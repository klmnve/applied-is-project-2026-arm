"""Use the application's configured SQLite file with per-request ORM sessions."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from sqlalchemy import Engine, event
from sqlmodel import Session, SQLModel, create_engine

from app.database import models  # noqa: F401 -- register tables before create_all


def create_db_engine(path: Path | str, *, echo: bool = False) -> Engine:
    database_path = Path(path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        "sqlite:///" + database_path.as_posix(),
        connect_args={"check_same_thread": False, "timeout": 10},
        echo=echo,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection: sqlite3.Connection, _record: Any) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.create_function(
            "casefold", 1, lambda value: str(value).casefold(), deterministic=True
        )

    return engine


def init_db(engine: Engine) -> None:
    """Create only missing SQLModel tables; leave existing ARM data unchanged."""
    SQLModel.metadata.create_all(engine)


def get_session(request: Request) -> Iterator[Session]:
    engine = cast(Engine, request.app.state.orm_engine)
    with Session(engine) as session:
        yield session
