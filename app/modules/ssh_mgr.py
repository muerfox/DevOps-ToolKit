"""SSH server registry + connections, built on paramiko.

Credentials (password or private key + passphrase) are encrypted at rest.
Supports one-shot command execution (used by the server list "run command"
action and by the pipeline engine's ssh_exec step) and interactive PTY
shells (used by the web terminal).
"""

from __future__ import annotations

import io

import paramiko
from sqlalchemy.orm import Session

from .. import models, security


class SSHError(Exception):
    pass


def list_servers(db: Session) -> list[models.SSHServer]:
    return db.query(models.SSHServer).order_by(models.SSHServer.name).all()


def get_server(db: Session, name: str) -> models.SSHServer:
    server = db.query(models.SSHServer).filter(models.SSHServer.name == name).first()
    if not server:
        raise SSHError(f"Unknown server '{name}'")
    return server


def create_server(
    db: Session,
    name: str,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    secret: str,
    passphrase: str | None = None,
    tags: str | None = None,
) -> models.SSHServer:
    record = models.SSHServer(
        name=name,
        host=host,
        port=port,
        username=username,
        auth_type=auth_type,
        secret_encrypted=security.encrypt(secret),
        passphrase_encrypted=security.encrypt(passphrase),
        tags=tags,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def delete_server(db: Session, server_id: int) -> None:
    server = db.get(models.SSHServer, server_id)
    if server:
        db.delete(server)
        db.commit()


_KEY_CLASSES = (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey)


def _load_private_key(text: str, passphrase: str | None) -> paramiko.PKey:
    last_exc = None
    for cls in _KEY_CLASSES:
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    raise SSHError(f"Could not parse private key: {last_exc}")


def connect(record: models.SSHServer) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    secret = security.decrypt(record.secret_encrypted)
    passphrase = security.decrypt(record.passphrase_encrypted)
    try:
        if record.auth_type == "key":
            pkey = _load_private_key(secret, passphrase)
            client.connect(record.host, port=record.port, username=record.username, pkey=pkey, timeout=10)
        else:
            client.connect(record.host, port=record.port, username=record.username, password=secret, timeout=10)
    except SSHError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SSHError(f"Could not connect to {record.host}:{record.port}: {exc}") from exc
    return client


def ping(record: models.SSHServer) -> dict:
    try:
        client = connect(record)
        client.close()
        return {"ok": True}
    except SSHError as exc:
        return {"ok": False, "error": str(exc)}


def run_command(record: models.SSHServer, command: str, timeout: int = 30) -> dict:
    client = connect(record)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return {"exit_code": exit_code, "stdout": out, "stderr": err}
    finally:
        client.close()


def open_shell(record: models.SSHServer, term: str = "xterm", width: int = 120, height: int = 32):
    client = connect(record)
    channel = client.invoke_shell(term=term, width=width, height=height)
    return client, channel
