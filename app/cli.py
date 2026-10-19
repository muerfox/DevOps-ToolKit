"""Maintenance commands, run inside the cockpit's environment (venv or container).

    python -m app.cli users
    python -m app.cli reset-password USER [--password-stdin]
    python -m app.cli create-admin USER [--password-stdin]
    python -m app.cli backup -o FILE|- [--env-stdin] [--no-repos]
    python -m app.cli restore -i FILE|- --yes
    python -m app.cli migrate
    python -m app.cli check

Usually invoked through the ./cockpit wrapper script rather than directly.
"""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from .config import DATA_DIR, INSECURE_SECRET_KEYS, settings

BACKUP_MARKER = "cockpit-backup.json"
_SKIP_IN_BACKUP = {"cockpit.db", "cockpit.db-wal", "cockpit.db-shm", "cockpit.db-journal"}


def _die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _read_password(args) -> str:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != password:
            _die("passwords do not match")
    if len(password) < 8:
        _die("password must be at least 8 characters")
    return password


def _sqlite_path() -> Path:
    if not settings.database_url.startswith("sqlite:///"):
        _die("backup/restore only supports the default SQLite database")
    return Path(settings.database_url[len("sqlite:///"):])


# --- users ------------------------------------------------------------

def cmd_users(_args) -> None:
    from .database import SessionLocal, init_db
    from . import models

    init_db()
    db = SessionLocal()
    try:
        users = db.query(models.User).order_by(models.User.id).all()
        if not users:
            print("(no users yet — open the web UI to create the first admin)")
        for u in users:
            print(f"{u.id:>3}  {u.username:<24} {'admin' if u.is_admin else 'user':<6} created {u.created_at:%Y-%m-%d}")
    finally:
        db.close()


def cmd_reset_password(args) -> None:
    from .database import SessionLocal, init_db
    from . import models, security

    init_db()
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == args.username).first()
        if not user:
            names = ", ".join(u.username for u in db.query(models.User)) or "none"
            _die(f"no user '{args.username}' (existing: {names}); use create-admin to add one")
        user.password_hash = security.hash_password(_read_password(args))
        db.commit()
        print(f"Password for '{user.username}' updated.")
    finally:
        db.close()


def cmd_create_admin(args) -> None:
    from .database import SessionLocal, init_db
    from . import models, security

    init_db()
    db = SessionLocal()
    try:
        if db.query(models.User).filter(models.User.username == args.username).first():
            _die(f"user '{args.username}' already exists; use reset-password")
        if len(args.username) < 3:
            _die("username must be at least 3 characters")
        db.add(models.User(username=args.username, password_hash=security.hash_password(_read_password(args)), is_admin=True))
        db.commit()
        print(f"Admin '{args.username}' created.")
    finally:
        db.close()


# --- backup / restore ------------------------------------------------------------

def cmd_backup(args) -> None:
    db_path = _sqlite_path()
    out_stream = sys.stdout.buffer if args.output == "-" else open(args.output, "wb")
    env_text = sys.stdin.read() if args.env_stdin else None

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "cockpit.db"
        if db_path.exists():
            # SQLite online backup: consistent even while the cockpit is writing (WAL mode).
            src = sqlite3.connect(str(db_path))
            dst = sqlite3.connect(str(snapshot))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()

        meta = {
            "format": "devops-cockpit-backup",
            "version": settings.version,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "includes_repos": not args.no_repos,
            "includes_env": env_text is not None,
        }
        with tarfile.open(fileobj=out_stream, mode="w|gz") as tar:
            def add_bytes(name: str, data: bytes, mode: int = 0o644):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), mode, int(time.time())
                tar.addfile(info, io.BytesIO(data))

            add_bytes(BACKUP_MARKER, json.dumps(meta, indent=2).encode())
            if snapshot.exists():
                tar.add(str(snapshot), arcname="data/cockpit.db")
            for entry in sorted(DATA_DIR.iterdir()):
                if entry.name in _SKIP_IN_BACKUP or entry.name in ("backups",):
                    continue
                if entry.name == "repos" and args.no_repos:
                    continue
                tar.add(str(entry), arcname=f"data/{entry.name}")
            if env_text is not None:
                add_bytes(".env", env_text.encode(), 0o600)

    if out_stream is not sys.stdout.buffer:
        out_stream.close()
        print(f"Backup written to {args.output}", file=sys.stderr)


def _safe_members(tar: tarfile.TarFile):
    for m in tar:
        name = m.name.lstrip("./")
        if not name.startswith("data/") or ".." in Path(name).parts or m.issym() or m.islnk():
            continue
        if not (m.isfile() or m.isdir()):
            continue
        m.name = name[len("data/"):]
        if m.name:
            yield m


def cmd_restore(args) -> None:
    if not args.yes:
        _die("restore replaces everything in the data directory; re-run with --yes to confirm")
    with tempfile.TemporaryDirectory() as tmp:
        if args.input == "-":
            archive = Path(tmp) / "restore.tar.gz"
            with open(archive, "wb") as f:
                shutil.copyfileobj(sys.stdin.buffer, f)
        else:
            archive = Path(args.input)
        _restore_from(archive)


def _restore_from(archive: Path) -> None:
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            names = tar.getnames()
    except (tarfile.TarError, OSError) as exc:
        _die(f"cannot read backup: {exc}")
    if BACKUP_MARKER not in names or "data/cockpit.db" not in names:
        _die("not a DevOps Cockpit backup (missing marker or database)")

    # Clear current contents (the data dir itself may be a mount point, so keep it).
    for entry in DATA_DIR.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()

    with tarfile.open(archive, mode="r:gz") as tar:
        members = list(_safe_members(tar))
        extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
        tar.extractall(path=str(DATA_DIR), members=members, **extra)
    for p in [DATA_DIR / "secret.key", *(DATA_DIR / "kubeconfigs").glob("*")]:
        if p.is_file():
            p.chmod(0o600)
    print(f"Restored {len(members)} entries into {DATA_DIR}", file=sys.stderr)


# --- misc ------------------------------------------------------------

def cmd_migrate(_args) -> None:
    from .database import init_db

    applied = init_db()
    print(f"{len(applied)} schema change(s) applied." if applied else "Database schema is up to date.")


def cmd_check(_args) -> None:
    """Machine-readable config check, used by `./cockpit doctor`."""
    key_file = DATA_DIR / "secret.key"
    out = {
        "version": settings.version,
        "secret_key_ok": settings.secret_key not in INSECURE_SECRET_KEYS and len(settings.secret_key) >= 32,
        "encryption_key_source": "env" if settings.encryption_key_env else ("file" if key_file.exists() else "will-generate"),
        "data_dir": str(DATA_DIR),
        "data_dir_writable": os.access(DATA_DIR, os.W_OK),
        "docker_host": settings.default_docker_host,
        "docker_cli": shutil.which("docker") is not None,
        "kubectl": shutil.which("kubectl") is not None,
        "git": shutil.which("git") is not None,
        "timezone": time.strftime("%Z"),
    }
    try:
        import docker

        client = docker.DockerClient(base_url=settings.default_docker_host, timeout=5)
        out["docker_engine"] = client.version().get("Version")
        client.close()
    except Exception as exc:  # noqa: BLE001
        out["docker_engine"] = None
        out["docker_error"] = str(exc)[:200]
    print(json.dumps(out))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="DevOps Cockpit maintenance")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("users", help="list users").set_defaults(fn=cmd_users)

    for name, fn in (("reset-password", cmd_reset_password), ("create-admin", cmd_create_admin)):
        p = sub.add_parser(name)
        p.add_argument("username")
        p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
        p.set_defaults(fn=fn)

    p = sub.add_parser("backup", help="write a .tar.gz backup of the data directory")
    p.add_argument("-o", "--output", required=True, help="file path, or - for stdout")
    p.add_argument("--env-stdin", action="store_true", help="include .env contents read from stdin")
    p.add_argument("--no-repos", action="store_true", help="skip cloned git repos (they can be re-cloned)")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("restore", help="replace the data directory with a backup")
    p.add_argument("-i", "--input", required=True, help="file path, or - for stdin")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_restore)

    sub.add_parser("migrate", help="apply pending schema upgrades").set_defaults(fn=cmd_migrate)
    sub.add_parser("check", help="print a JSON configuration check").set_defaults(fn=cmd_check)

    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
