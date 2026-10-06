import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from cibud.db.tables import Base

TEST_DATABASE_URL = os.environ.get(
    "CIBUD_TEST_DATABASE_URL", "postgresql+psycopg://cibud:cibud@localhost:5433/cibud_test"
)
BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Test database, rebuilt from migrations once per run (so migrations are tested too)."""
    eng = create_engine(TEST_DATABASE_URL)
    try:
        eng.connect().close()
    except OperationalError:
        if os.environ.get("CI"):
            raise
        pytest.skip(
            "test database unavailable; run: docker compose -f infra/docker-compose.yml up -d"
        )

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, expire_on_commit=False)
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def session(factory: sessionmaker[Session]) -> Iterator[Session]:
    with factory() as s, s.begin():
        yield s
