"""Admin token authentication (ADR-012: admin tokens and team API keys are
two fully independent opaque-token systems -- this module only implements
the former, mirroring gateway/auth/team_auth.py's shape exactly). Every
lookup hits Postgres directly; no in-process cache, so a revoked token stops
working on the very next request (ADR-007).
"""

from __future__ import annotations

from fastapi import Header, HTTPException
from pydantic import BaseModel

from gateway.db import get_pool

_INVALID_TOKEN_DETAIL = "invalid or missing admin token"

_ADMIN_LOOKUP_QUERY = """
    SELECT admin_name
    FROM admin_tokens
    WHERE token = $1 AND revoked_at IS NULL
"""


class Admin(BaseModel):
    name: str


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ")
    return authorization


async def get_current_admin(authorization: str | None = Header(default=None)) -> Admin:
    token = _extract_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail=_INVALID_TOKEN_DETAIL)

    row = await get_pool().fetchrow(_ADMIN_LOOKUP_QUERY, token)
    if row is None:
        raise HTTPException(status_code=401, detail=_INVALID_TOKEN_DETAIL)

    return Admin(name=row["admin_name"])
