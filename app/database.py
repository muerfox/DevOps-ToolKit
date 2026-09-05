import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _auto_migrate() -> None:
    """Best-effort dev migration: add any model column that's missing from
    an already-existing table.

    Base.metadata.create_all() only creates tables that don't exist yet --
    it never alters an existing table's columns, so a fresh column added to
    a model (this project has picked up several across its history: is_admin,
    webhook_token, tags, ...) silently doesn't show up on a database created
    before that change. There's no Alembic here (deliberately, for a project
    this size), so this covers the gap: additive only, never drops or alters
    an existing column, and is a no-op for a brand-new database where
    create_all() already created every table with every column.
    """
    inspector = sa.inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            pk_cols = [c.name for c in table.primary_key.columns]
            for column in table.columns:
                if column.name in existing_cols:
                    continue
                col_type = column.type.compile(engine.dialect)
                conn.execute(sa.text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))

                if column.default is None or not pk_cols:
                    continue
                # ADD COLUMN only ever applies a constant to existing rows (NULL,
                # here, since we didn't give it one) -- it can't run a Python-side
                # default like generate_webhook_token, so without this, every
                # pre-existing row would sit at NULL. For a column like
                # webhook_token that's a real bug, not just a cosmetic gap: the
                # trigger route's hmac.compare_digest(token, None) raises a
                # TypeError instead of cleanly rejecting the request.
                where = " AND ".join(f'"{c}" = :{c}' for c in pk_cols)
                if getattr(column.default, "is_callable", False):
                    fn = column.default.arg
                    rows = conn.execute(sa.text(f'SELECT {", ".join(pk_cols)} FROM "{table.name}"')).fetchall()
                    for row in rows:
                        try:
                            value = fn()
                        except TypeError:
                            value = fn(None)  # SQLAlchemy passes an ExecutionContext to 1-arg defaults
                        params = dict(zip(pk_cols, row))
                        params["val"] = value
                        conn.execute(sa.text(f'UPDATE "{table.name}" SET "{column.name}" = :val WHERE {where}'), params)
                else:
                    conn.execute(sa.text(f'UPDATE "{table.name}" SET "{column.name}" = :val'), {"val": column.default.arg})


def init_db() -> None:
    from . import models  # noqa: F401  (register models on Base before create_all)

    Base.metadata.create_all(bind=engine)
    _auto_migrate()
