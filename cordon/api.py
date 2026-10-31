"""
api.py -- thin FastAPI wrapper around CaseService. All the actual
logic (budgets, telemetry, SLOs) lives in service.py/budget.py/
slo.py and is tested directly against those modules; this file's
only job is translating HTTP in and out, plus exposing the ops
endpoints (/ops/*) a human would actually watch.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from .auth import InvalidTenantToken, verify_tenant_token
from .budget import BudgetExceeded
from .metrics import render_metrics
from .service import CaseNotFound, CaseService
from .slo import (
    SLO,
    MultiWindowSLO,
    check_all_tenants,
    check_all_tenants_multi_window,
    compute_burn_rate,
    evaluate_multi_window,
    throttled_fraction,
)

app = FastAPI(
    title="cordon",
    description="Multi-tenant case-management API with per-tenant isolation.",
)
service = CaseService()

# The SLO this service promises every tenant: 99.5% of requests succeed,
# measured over a 5-minute rolling window. One SLO, one window -- the
# scope this project actually implements (see slo.py's own docstring).
DEFAULT_SLO = SLO(name="availability", target=0.995, window_seconds=300.0)

# The production paging decision: multi-window, multi-burn-rate rules
# (see slo.py) layered on top of the same single SLO target above.
DEFAULT_MULTI_WINDOW_SLO = MultiWindowSLO(slo=DEFAULT_SLO)


class CreateCaseRequest(BaseModel):
    subject: str
    description: str = ""


def tenant_id_from_token(x_tenant_token: str = Header(...)) -> str:
    """Every endpoint below depends on this instead of reading
    `X-Tenant-Id` directly. The id this returns is the one the
    signature actually proved, not a value the caller merely typed
    into a header -- see auth.py for why that distinction is the
    whole point. An unverifiable token is a 401, not a 500: a bad
    identity claim is the client's problem, not this service's."""
    try:
        return verify_tenant_token(x_tenant_token)
    except InvalidTenantToken as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/cases")
def create_case(body: CreateCaseRequest, x_tenant_id: str = Depends(tenant_id_from_token)):
    try:
        case = service.create_case(x_tenant_id, body.subject, body.description)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))
    return case.__dict__


@app.get("/cases/{case_id}")
def get_case(case_id: str, x_tenant_id: str = Depends(tenant_id_from_token)):
    try:
        case = service.get_case(x_tenant_id, case_id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))
    except CaseNotFound:
        raise HTTPException(status_code=404, detail="case not found")
    return case.__dict__


@app.get("/cases")
def list_cases(x_tenant_id: str = Depends(tenant_id_from_token)):
    try:
        cases = service.list_cases(x_tenant_id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))
    return [c.__dict__ for c in cases]


@app.post("/cases/{case_id}/close")
def close_case(case_id: str, x_tenant_id: str = Depends(tenant_id_from_token)):
    try:
        case = service.close_case(x_tenant_id, case_id)
    except BudgetExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))
    except CaseNotFound:
        raise HTTPException(status_code=404, detail="case not found")
    return case.__dict__


@app.get("/ops/headroom")
def ops_headroom(x_tenant_id: str = Depends(tenant_id_from_token)):
    """What an on-call engineer checks first: how close this tenant
    is to its own budget, before anything actually breaks."""
    return service.budgets.headroom(x_tenant_id)


@app.get("/ops/burn-rate")
def ops_burn_rate(x_tenant_id: str = Depends(tenant_id_from_token)):
    """Single-window SLO burn rate for one tenant -- the simple
    building block. /ops/burn-rate-multi-window is the actual paging
    decision; this stays for the single number on its own."""
    result = compute_burn_rate(service.telemetry, x_tenant_id, DEFAULT_SLO)
    return result.__dict__


@app.get("/ops/burn-rate-multi-window")
def ops_burn_rate_multi_window(x_tenant_id: str = Depends(tenant_id_from_token)):
    """The real paging decision: multi-window, multi-burn-rate rules
    (see slo.py) -- pages only when a fast-burn or slow-burn rule's
    long AND short windows both agree, so a single noisy window
    doesn't page on its own and a resolved incident stops paging as
    soon as the short window clears."""
    result = evaluate_multi_window(service.telemetry, x_tenant_id, DEFAULT_MULTI_WINDOW_SLO)
    return asdict(result)


@app.get("/ops/throttled-fraction")
def ops_throttled_fraction(x_tenant_id: str = Depends(tenant_id_from_token)):
    """Distinct from burn rate on purpose: how much of this tenant's
    own traffic is being contained by its own budget right now."""
    return {"tenant_id": x_tenant_id, "throttled_fraction": throttled_fraction(
        service.telemetry, x_tenant_id, DEFAULT_SLO.window_seconds
    )}


@app.get("/ops/sweep")
def ops_sweep():
    """Every known tenant's single-window burn rate in one call."""
    return [r.__dict__ for r in check_all_tenants(service.telemetry, DEFAULT_SLO)]


@app.get("/ops/sweep-multi-window")
def ops_sweep_multi_window():
    """Every known tenant's multi-window paging decision in one call
    -- the actual on-call sweep, so a genuinely paging tenant
    surfaces without having to be asked about by name."""
    return [
        asdict(r)
        for r in check_all_tenants_multi_window(service.telemetry, DEFAULT_MULTI_WINDOW_SLO)
    ]


@app.get("/metrics")
def metrics():
    """Prometheus scrape target -- the same underlying state as
    /ops/*, in the format a real monitoring stack actually consumes.
    See metrics.py for why this needs its own module."""
    body = render_metrics(service.telemetry, service.budgets, DEFAULT_SLO)
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")
