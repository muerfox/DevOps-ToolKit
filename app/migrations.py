"""Lightweight, automatic schema upgrades.

`Base.metadata.create_all()` creates missing tables but never touches
existing ones, so a column added to a model would break an existing
database. On every startup this compares each model table against the live
schema and issues `ALTER TABLE ... ADD COLUMN` for anything missing. It only
ever adds columns (never drops or rewrites), so it is safe to run repeatedly.

New columns must be nullable or have a scalar default; anything more
involved (renames, type changes, data backfills) needs a hand-written step.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .database import Base

log = logging.getLogger("cockpit.migrations")


def _default_sql(column) -> str:
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return ""
    value = default.arg
    if isinstance(value, bool):
        return f" DEFAULT {1 if value else 0}"
    if isinstance(value, (int, float)):
        return f" DEFAULT {value}"
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f" DEFAULT '{escaped}'"
    return ""


def upgrade(engine: Engine) -> list[str]:
    """Create missing tables and add missing columns. Returns the DDL statements applied."""
    from . import models  # noqa: F401  (register models on Base)

    Base.metadata.create_all(bind=engine)

    applied: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                # SQLite can't ADD COLUMN with a UNIQUE constraint; uniqueness for
                # added columns is enforced by an index instead.
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}{_default_sql(column)}'
                conn.execute(text(ddl))
                applied.append(ddl)
                if column.unique:
                    idx = f'CREATE UNIQUE INDEX IF NOT EXISTS "uq_{table.name}_{column.name}" ON "{table.name}" ("{column.name}")'
                    conn.execute(text(idx))
                    applied.append(idx)

    for ddl in applied:
        log.warning("schema upgrade: %s", ddl)
    relocate_data_paths()
    return applied


def relocate_data_paths() -> int:
    """Kubeconfig and clone paths are stored absolute. After restoring a backup
    somewhere else (e.g. from a venv install into Docker, /home/.../data -> /app/data),
    repoint them at the same files under the current data directory."""
    from pathlib import Path

    from .config import KUBECONFIGS_DIR, REPOS_DIR
    from .database import SessionLocal
    from . import models

    fixed = 0
    db = SessionLocal()
    try:
        for c in db.query(models.K8sCluster):
            candidate = KUBECONFIGS_DIR / Path(c.kubeconfig_path).name
            if not Path(c.kubeconfig_path).exists() and candidate.exists():
                c.kubeconfig_path = str(candidate)
                fixed += 1
        for r in db.query(models.GitRepo):
            candidate = REPOS_DIR / Path(r.local_path).name
            if not Path(r.local_path).exists() and candidate.exists():
                r.local_path = str(candidate)
                fixed += 1
        db.commit()
    finally:
        db.close()
    if fixed:
        log.warning("repointed %d kubeconfig/repo path(s) to %s", fixed, KUBECONFIGS_DIR.parent)
    return fixed
