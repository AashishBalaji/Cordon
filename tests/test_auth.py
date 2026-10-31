"""Tests for cordon.auth -- the tenant-identity verification this
project previously didn't have (every endpoint trusted a bare
X-Tenant-Id header). These test the signing/verification logic
directly; tests/test_api.py tests it wired into the actual HTTP
layer."""

import base64

import pytest

from cordon.auth import InvalidTenantToken, issue_tenant_token, verify_tenant_token

SECRET = b"a fixed test secret, not the process-random fallback"


def test_a_token_round_trips_to_the_same_tenant_id():
    token = issue_tenant_token("acme", secret=SECRET)
    assert verify_tenant_token(token, secret=SECRET) == "acme"


def test_tenant_ids_with_unusual_characters_round_trip_too():
    for tenant_id in ["tenant.with.dots", "tenant/with/slashes", "tenant with spaces", "日本語"]:
        token = issue_tenant_token(tenant_id, secret=SECRET)
        assert verify_tenant_token(token, secret=SECRET) == tenant_id


def test_tampering_with_the_signature_is_rejected():
    token = issue_tenant_token("acme", secret=SECRET)
    encoded_id, signature = token.split(".", 1)
    flipped_char = "0" if signature[-1] != "0" else "1"
    tampered = f"{encoded_id}.{signature[:-1]}{flipped_char}"
    with pytest.raises(InvalidTenantToken):
        verify_tenant_token(tampered, secret=SECRET)


def test_forging_a_different_tenant_id_onto_a_valid_signature_is_rejected():
    # The attack this whole module exists to stop: taking a token
    # that legitimately verifies for "acme" and swapping in a
    # different tenant id without re-signing. The signature covers
    # the encoded id itself, so this must not verify as "globex".
    acme_token = issue_tenant_token("acme", secret=SECRET)
    _, acme_signature = acme_token.split(".", 1)
    forged_encoded_id = base64.urlsafe_b64encode(b"globex").decode("ascii").rstrip("=")
    forged_token = f"{forged_encoded_id}.{acme_signature}"
    with pytest.raises(InvalidTenantToken):
        verify_tenant_token(forged_token, secret=SECRET)


def test_a_malformed_token_with_no_separator_is_rejected():
    with pytest.raises(InvalidTenantToken):
        verify_tenant_token("not-a-valid-token-at-all", secret=SECRET)


def test_a_token_signed_with_a_different_secret_is_rejected():
    token = issue_tenant_token("acme", secret=SECRET)
    with pytest.raises(InvalidTenantToken):
        verify_tenant_token(token, secret=b"a completely different secret")


def test_corrupt_base64_in_the_id_segment_is_rejected_not_crashed():
    # A signature can validly cover garbage bytes if it was computed
    # over them -- verify_tenant_token must still fail cleanly (an
    # InvalidTenantToken), not raise an unrelated decoding exception,
    # when asked to decode something that isn't valid base64.
    import hashlib
    import hmac

    garbage_encoded_id = "%%%not-base64%%%"
    signature = hmac.new(SECRET, garbage_encoded_id.encode("ascii"), hashlib.sha256).hexdigest()
    token = f"{garbage_encoded_id}.{signature}"
    with pytest.raises(InvalidTenantToken):
        verify_tenant_token(token, secret=SECRET)


def test_default_secret_falls_back_consistently_within_one_process():
    # No explicit secret passed on either side -- both calls must
    # still agree, via the same env-var-or-process-fallback secret.
    token = issue_tenant_token("acme")
    assert verify_tenant_token(token) == "acme"


def test_env_var_secret_is_used_when_set(monkeypatch):
    from cordon.auth import SECRET_ENV_VAR

    monkeypatch.setenv(SECRET_ENV_VAR, "a secret from the environment")
    token = issue_tenant_token("acme")  # no explicit secret -- must read the env var
    assert verify_tenant_token(token) == "acme"
    # A token signed under a *different* env secret must not verify.
    monkeypatch.setenv(SECRET_ENV_VAR, "a different secret from the environment")
    with pytest.raises(InvalidTenantToken):
        verify_tenant_token(token)
