"""Functional tests through the actual HTTP layer, via FastAPI's
TestClient (in-process, no real server) -- confirms the budget/
telemetry wiring survives being called over HTTP, not just as
direct Python calls."""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    # Fresh module import per test so each test gets its own
    # CaseService/BudgetRegistry/TelemetryStore -- otherwise tenant
    # budgets would leak state between tests via the module-level
    # singleton in api.py.
    import cordon.api as api_module

    importlib.reload(api_module)
    return TestClient(api_module.app)


def test_create_case_requires_tenant_header(client):
    resp = client.post("/cases", json={"subject": "no tenant header"})
    assert resp.status_code == 422  # FastAPI's own missing-required-header error


def test_create_and_fetch_case_over_http(client):
    resp = client.post("/cases", json={"subject": "Billing issue"}, headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 200
    case_id = resp.json()["case_id"]

    resp = client.get(f"/cases/{case_id}", headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 200
    assert resp.json()["subject"] == "Billing issue"


def test_cross_tenant_fetch_returns_404_not_someone_elses_case(client):
    resp = client.post("/cases", json={"subject": "secret"}, headers={"X-Tenant-Id": "acme"})
    case_id = resp.json()["case_id"]

    resp = client.get(f"/cases/{case_id}", headers={"X-Tenant-Id": "globex"})
    assert resp.status_code == 404


def test_budget_exceeded_returns_429_over_http(client, monkeypatch):
    # Collapse this tenant's rate budget to 1 so the second request
    # over HTTP actually trips BudgetExceeded -- the thing we're
    # testing is that it surfaces as a 429, not a 500.
    import cordon.api as api_module

    tenant_budget = api_module.service.budgets.get("heavy")
    tenant_budget.rate_bucket.capacity = 1.0
    tenant_budget.rate_bucket.tokens = 1.0
    tenant_budget.rate_bucket.refill_rate = 0.0

    r1 = client.post("/cases", json={"subject": "first"}, headers={"X-Tenant-Id": "heavy"})
    assert r1.status_code == 200
    r2 = client.post("/cases", json={"subject": "second"}, headers={"X-Tenant-Id": "heavy"})
    assert r2.status_code == 429


def test_ops_headroom_endpoint(client):
    client.post("/cases", json={"subject": "x"}, headers={"X-Tenant-Id": "acme"})
    resp = client.get("/ops/headroom", headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 200
    body = resp.json()
    assert "request_rate" in body and "resource_cost" in body


def test_ops_burn_rate_endpoint_shape(client):
    resp = client.get("/ops/burn-rate", headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) >= {"tenant_id", "burn_rate", "should_page", "request_count"}


def test_list_cases_over_http(client):
    client.post("/cases", json={"subject": "a"}, headers={"X-Tenant-Id": "acme"})
    client.post("/cases", json={"subject": "b"}, headers={"X-Tenant-Id": "acme"})
    resp = client.get("/cases", headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_close_case_over_http(client):
    created = client.post("/cases", json={"subject": "a"}, headers={"X-Tenant-Id": "acme"})
    case_id = created.json()["case_id"]
    resp = client.post(f"/cases/{case_id}/close", headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"


def test_close_missing_case_returns_404(client):
    resp = client.post("/cases/does-not-exist/close", headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 404


def test_list_cases_budget_exceeded_returns_429(client):
    import cordon.api as api_module

    tenant_budget = api_module.service.budgets.get("heavy")
    tenant_budget.rate_bucket.capacity = 0.0
    tenant_budget.rate_bucket.tokens = 0.0
    tenant_budget.rate_bucket.refill_rate = 0.0

    resp = client.get("/cases", headers={"X-Tenant-Id": "heavy"})
    assert resp.status_code == 429


def test_close_case_budget_exceeded_returns_429(client):
    import cordon.api as api_module

    created = client.post("/cases", json={"subject": "a"}, headers={"X-Tenant-Id": "acme"})
    case_id = created.json()["case_id"]

    tenant_budget = api_module.service.budgets.get("acme")
    tenant_budget.rate_bucket.tokens = 0.0
    tenant_budget.rate_bucket.refill_rate = 0.0

    resp = client.post(f"/cases/{case_id}/close", headers={"X-Tenant-Id": "acme"})
    assert resp.status_code == 429


def test_ops_throttled_fraction_endpoint(client):
    import cordon.api as api_module

    tenant_budget = api_module.service.budgets.get("noisy")
    tenant_budget.rate_bucket.capacity = 1.0
    tenant_budget.rate_bucket.tokens = 1.0
    tenant_budget.rate_bucket.refill_rate = 0.0

    client.post("/cases", json={"subject": "a"}, headers={"X-Tenant-Id": "noisy"})
    client.post("/cases", json={"subject": "b"}, headers={"X-Tenant-Id": "noisy"})  # throttled

    resp = client.get("/ops/throttled-fraction", headers={"X-Tenant-Id": "noisy"})
    assert resp.status_code == 200
    assert resp.json()["throttled_fraction"] > 0.0


def test_ops_sweep_includes_every_known_tenant(client):
    client.post("/cases", json={"subject": "a"}, headers={"X-Tenant-Id": "acme"})
    client.post("/cases", json={"subject": "b"}, headers={"X-Tenant-Id": "globex"})
    resp = client.get("/ops/sweep")
    assert resp.status_code == 200
    tenant_ids = {row["tenant_id"] for row in resp.json()}
    assert {"acme", "globex"} <= tenant_ids


def test_metrics_endpoint_returns_prometheus_text_format(client):
    client.post("/cases", json={"subject": "a"}, headers={"X-Tenant-Id": "acme"})
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "cordon_requests_total" in resp.text
    assert 'tenant_id="acme"' in resp.text


def test_a_noisy_tenant_does_not_affect_another_tenants_headroom(client):
    import cordon.api as api_module

    noisy_budget = api_module.service.budgets.get("noisy")
    noisy_budget.rate_bucket.capacity = 1.0
    noisy_budget.rate_bucket.tokens = 1.0
    noisy_budget.rate_bucket.refill_rate = 0.0

    client.post("/cases", json={"subject": "a"}, headers={"X-Tenant-Id": "noisy"})
    client.post("/cases", json={"subject": "b"}, headers={"X-Tenant-Id": "noisy"})  # throttled

    quiet_resp = client.post("/cases", json={"subject": "c"}, headers={"X-Tenant-Id": "quiet"})
    assert quiet_resp.status_code == 200

    quiet_headroom = client.get("/ops/headroom", headers={"X-Tenant-Id": "quiet"}).json()
    assert quiet_headroom["request_rate"] > 0.9  # quiet's budget untouched by noisy's exhaustion
