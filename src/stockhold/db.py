"""Engine, session and write-lock helpers.

PostgreSQL serialises competing writers with row locks (``SELECT ... FOR UPDATE``).
SQLite ignores ``FOR UPDATE``, so for SQLite we take a process-wide lock around every
write transaction instead. That is enough for tests and single-process demos; production
deployments are expected to run on PostgreSQL.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


_sqlite_write_lock = threading.RLock()


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):
            # One shared connection so every session sees the same in-memory database.
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def is_sqlite(session: Session) -> bool:
    return session.get_bind().dialect.name == "sqlite"


@contextmanager
def write_transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Open a session, run one transaction and commit or roll back.

    On SQLite the whole transaction runs under a process-wide lock, which stands in
    for the row locks PostgreSQL gives us.
    """
    session = factory()
    sqlite = session.get_bind().dialect.name == "sqlite"
    if sqlite:
        _sqlite_write_lock.acquire()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
        if sqlite:
            _sqlite_write_lock.release()
