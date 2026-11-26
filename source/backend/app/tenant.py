"""Per-request dashboard scope for database connections."""
from __future__ import annotations

from contextvars import ContextVar, Token

_dashboard_id: ContextVar[str | None] = ContextVar("dashboard_id", default=None)


def set_dashboard(dashboard_id: str) -> Token[str | None]:
    return _dashboard_id.set(str(dashboard_id))


def reset_dashboard(token: Token[str | None]) -> None:
    _dashboard_id.reset(token)


def current_dashboard() -> str | None:
    return _dashboard_id.get()
