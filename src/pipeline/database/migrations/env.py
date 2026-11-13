"""Run migrations using the caller's existing transactional connection."""

from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Asset migrations require an explicit SQLAlchemy connection")

context.configure(connection=connection, target_metadata=None)
with context.begin_transaction():
    context.run_migrations()
