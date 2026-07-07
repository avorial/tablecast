import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config

log = logging.getLogger("tablecast.db")


class Base(DeclarativeBase):
    pass


connect_args = {}
if config.DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(config.DATABASE_URL, connect_args=connect_args)

if config.DATABASE_URL.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# Columns added after the initial release: create_all creates missing
# TABLES but never alters existing ones, so patch these in by hand.
# (table, column, DDL type + default)
_MIGRATIONS = [
    ("game_sessions", "podcast_status", "VARCHAR(16) NOT NULL DEFAULT ''"),
]


def _ensure_columns() -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in _MIGRATIONS:
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                log.info("migrating: adding %s.%s", table, column)
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def init_db() -> None:
    from . import models  # noqa: F401  (register mappings)

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _ensure_columns()
