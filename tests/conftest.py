"""Every test gets a session bound to a single connection wrapped in an outer
transaction that's rolled back at the end — so tests can freely insert/commit
without polluting the real dev/seed data, and without needing a second
database. Requires the same Postgres the migrations were applied to (models
use Postgres-specific UUID/JSONB column types, so SQLite isn't an option)."""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

# A dedicated NullPool engine for tests: every checkout is a brand-new
# physical connection, so a previous test's transaction/savepoint state can
# never leak into the next test via a pooled, reused asyncpg connection.
_test_engine = create_async_engine(get_settings().database_url, poolclass=NullPool)


@pytest_asyncio.fixture
async def db_session():
    async with _test_engine.connect() as conn:
        trans = await conn.begin()
        session_factory = async_sessionmaker(
            bind=conn, expire_on_commit=False, class_=AsyncSession, join_transaction_mode="create_savepoint"
        )
        async with session_factory() as session:
            yield session
        await trans.rollback()
