from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.orm import Session

from cibud.db.session import session_factory
from cibud.settings import get_settings
from cibud.storage import LocalObjectStore, ObjectStore


def get_session() -> Iterator[Session]:
    """One transaction per request: committed on success, rolled back on error."""
    with session_factory()() as session, session.begin():
        yield session


@lru_cache
def get_store() -> ObjectStore:
    return LocalObjectStore(get_settings().object_store_dir)
