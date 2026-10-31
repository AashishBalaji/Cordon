"""
auth.py -- verifies tenant identity instead of trusting it.

The gap this closes, stated plainly: every endpoint in api.py used to
read `X-Tenant-Id` as a bare, client-supplied header and trust it
outright. That's fine for the isolation and telemetry layers this
project is actually about (a tenant never sees another tenant's
budget or cases *if the header is honest*), but it means any caller
could put any value in that header and act as any tenant -- the
isolation guarantee this whole project is built around is worthless
against a client that simply lies about who it is.

The fix: tenants present a signed token (`X-Tenant-Token`), not a
bare id. This service verifies the signature with `verify_tenant_token`
and only trusts the tenant id that comes back from a token that
actually checks out -- never the raw header value.

Honest scope: in a real deployment, signing tokens is an upstream
identity provider's or API gateway's job (this service would only
ever verify, with a securely managed/rotated key -- a KMS secret,
not a module-level fallback). `issue_tenant_token` below exists only
so tests and the demo scripts in this repo can produce valid tokens
without a real IdP; it is not exposed over HTTP, because an
unauthenticated "give me a token for any tenant" endpoint would just
move the same trust problem one hop over, not fix it. The
HMAC-SHA256 signature check uses `hmac.compare_digest` specifically
to avoid a timing side-channel on the comparison, not plain `==`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

# Real deployment: set this to a securely managed, rotated secret
# (a KMS-backed value, not a repo constant). Demo fallback below
# exists so this project runs out of the box without one.
SECRET_ENV_VAR = "CORDON_TENANT_SIGNING_SECRET"

# Generated once per process as a fallback when SECRET_ENV_VAR isn't
# set, so every verify_tenant_token call in this process agrees with
# every issue_tenant_token call in the same process without any setup
# -- explicitly NOT how a real deployment should get its signing key.
_PROCESS_FALLBACK_SECRET = os.urandom(32)


class InvalidTenantToken(Exception):
    """Raised when a presented token doesn't verify -- wrong
    signature, malformed shape, or corrupt encoding. Callers (api.py)
    turn this into an HTTP 401, never a 500: an unverifiable identity
    claim is a client error, not a server fault."""


def _secret(secret: bytes | None) -> bytes:
    if secret is not None:
        return secret
    from_env = os.environ.get(SECRET_ENV_VAR)
    if from_env:
        return from_env.encode("utf-8")
    return _PROCESS_FALLBACK_SECRET


def issue_tenant_token(tenant_id: str, secret: bytes | None = None) -> str:
    """Build a signed `<encoded-id>.<signature>` token for one
    tenant. Not exposed over HTTP -- see module docstring for why --
    used only by tests and the demo scripts in this repo."""
    secret_bytes = _secret(secret)
    encoded_id = base64.urlsafe_b64encode(tenant_id.encode("utf-8")).decode("ascii").rstrip("=")
    signature = hmac.new(secret_bytes, encoded_id.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded_id}.{signature}"


def verify_tenant_token(token: str, secret: bytes | None = None) -> str:
    """Verify a token and return the tenant id it actually proves,
    or raise InvalidTenantToken. The returned tenant id -- never the
    raw header -- is what every endpoint in api.py now uses."""
    secret_bytes = _secret(secret)
    try:
        encoded_id, signature = token.split(".", 1)
    except ValueError:
        raise InvalidTenantToken("malformed token: expected '<encoded-id>.<signature>'")

    expected_signature = hmac.new(
        secret_bytes, encoded_id.encode("ascii"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise InvalidTenantToken(
            "signature does not match -- token was tampered with, expired under a "
            "rotated secret, or issued for a different service"
        )

    try:
        padding = "=" * (-len(encoded_id) % 4)
        tenant_id = base64.urlsafe_b64decode(encoded_id + padding).decode("utf-8")
    except Exception as e:
        raise InvalidTenantToken(f"corrupt tenant id encoding: {e}")

    return tenant_id
