from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from cibud.settings import get_settings


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any exception."""
    with factory() as session, session.begin():
        yield session
