from alembic import context
from app.database import get_engine


def run_migrations_online():
    with get_engine().connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
