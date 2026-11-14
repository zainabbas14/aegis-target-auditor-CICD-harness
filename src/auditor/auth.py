"""Authentication and role-based authorisation.

Identity comes from the credential, never from the request body: the person
recorded as keying, mapping, or approving an invoice is whoever authenticated.
That is what makes the four-eyes rule mean something.

Mechanism: static API keys, one per principal, configured as a secret
(``AUDITOR_API_KEYS``). Keys are hashed at load and requests are matched by hash,
so plaintext keys are never held in memory beyond startup. This is deliberately
simple - the roles and the dependency are the contract; swapping the key lookup
for OIDC/JWT validation changes this module only.

    AUDITOR_API_KEYS="clerk.bea:clerk:<key>,ana:analyst:<key>,ap.manager:approver:<key>,ian:auditor:<key>"

A principal may hold several roles: ``ana:analyst+clerk:<key>``.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

MIN_KEY_LENGTH = 16
MIN_PROD_KEY_LENGTH = 24


class Role(StrEnum):
    CLERK = "clerk"  # uploads invoices, keys them in when parsing fails
    ANALYST = "analyst"  # confirms line-item mappings, reconciles contracts
    APPROVER = "approver"  # approves or rejects payment
    AUDITOR = "auditor"  # reads the audit log


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    roles: frozenset[Role]


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_api_keys(raw: str, *, production: bool = False) -> dict[str, Principal]:
    """``id:role[+role]:key`` entries, comma-separated -> {sha256(key): Principal}. Raises ValueError."""
    principals: dict[str, Principal] = {}
    seen_ids: set[str] = set()
    for i, entry in enumerate(e.strip() for e in raw.split(",") if e.strip()):
        parts = entry.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"API key entry {i + 1}: expected id:role[+role]:key")
        pid, roles_raw, key = (p.strip() for p in parts)
        if not pid or pid.casefold() in seen_ids:
            raise ValueError(f"API key entry {i + 1}: empty or duplicate principal id")
        try:
            roles = frozenset(Role(r.strip()) for r in roles_raw.split("+") if r.strip())
        except ValueError as exc:
            raise ValueError(f"API key entry {i + 1} ({pid}): unknown role; use {', '.join(Role)}") from exc
        if not roles:
            raise ValueError(f"API key entry {i + 1} ({pid}): no roles")
        min_len = MIN_PROD_KEY_LENGTH if production else MIN_KEY_LENGTH
        if len(key) < min_len or (production and "change-me" in key):
            raise ValueError(f"API key for {pid}: must be at least {min_len} characters and not a placeholder")
        digest = _digest(key)
        if digest in principals:
            raise ValueError(f"API key for {pid}: key reused by another principal")
        principals[digest] = Principal(id=pid, roles=roles)
        seen_ids.add(pid.casefold())
    return principals


_bearer = HTTPBearer(auto_error=False)


def authenticate(
    request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]
) -> Principal:
    principals: dict[str, Principal] = request.app.state.principals
    principal = principals.get(_digest(creds.credentials)) if creds else None
    if principal is None:
        logger.warning("Authentication failed", extra={"path": request.url.path, "credential_present": bool(creds)})
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or missing API key", headers={"WWW-Authenticate": "Bearer"}
        )
    return principal


def require(*roles: Role) -> Callable[[Principal], Principal]:
    """Dependency: authenticated AND holding at least one of ``roles`` (any role if none given)."""

    def dependency(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
        if roles and not (principal.roles & set(roles)):
            logger.warning("Authorisation denied", extra={"principal": principal.id, "needs": [r.value for r in roles]})
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"requires role: {' or '.join(r.value for r in roles)}")
        return principal

    return dependency
