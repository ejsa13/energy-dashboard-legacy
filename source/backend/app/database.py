import os
from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from app.tenant import current_dashboard

EXPECTED_REVISION = "0018_axle_credential_provider"


@lru_cache
def get_engine() -> Engine:
    engine = create_engine(
        os.environ["DATABASE_URL"],
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=2,
        pool_timeout=3,
        connect_args={"connect_timeout": 3},
        hide_parameters=True,
    )

    @event.listens_for(engine, "checkout")
    def apply_dashboard_scope(dbapi_connection, _record, _proxy) -> None:
        # RLS evaluates this setting for every statement. Reset it on every
        # pooled checkout so one request can never inherit another user's id.
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.dashboard_id', %s, false)", (current_dashboard() or "",))

    return engine
