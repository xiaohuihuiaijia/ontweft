from alembic import context
from ontology.core.config import Settings
from ontology.core.registry import register_models
from sqlalchemy import create_engine, pool

target_metadata = register_models()
config = context.config
url = Settings().database_url


if context.is_offline_mode():
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
