from collections.abc import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine() -> Engine:
    settings = get_settings()
    url = settings.resolved_database_url
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            # WAL lets readers proceed while a background job is writing.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401  (registers tables)

    Base.metadata.create_all(engine)
    # Open the pool's first connection and prime SQLAlchemy's statement cache now,
    # rather than on the first request.
    with SessionLocal() as session:
        session.execute(select(models.GeoFile.id).limit(1)).all()
        session.execute(select(models.Feature.feature_index).limit(1)).all()


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
