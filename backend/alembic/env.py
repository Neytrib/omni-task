import os

from alembic import context
from app.models import Base
from sqlalchemy import create_engine, pool

config = context.config
target_metadata = Base.metadata


def run_with_connection(connection):
    # Protect against two separate migration invocations racing on the same DB.
    connection.exec_driver_sql("SELECT pg_advisory_lock(739142810)")
    connection.commit()
    try:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.exec_driver_sql("SELECT pg_advisory_unlock(739142810)")
        connection.commit()


if context.is_offline_mode():
    context.configure(
        url=os.environ["DATABASE_URL"],
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    run_with_connection(config.attributes["connection"])
else:
    engine = create_engine(
        os.environ["DATABASE_URL"], poolclass=pool.NullPool, hide_parameters=True
    )
    with engine.connect() as connection:
        run_with_connection(connection)
