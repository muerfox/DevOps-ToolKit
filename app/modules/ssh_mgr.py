"""SSH server registry + connections, built on paramiko.

Credentials (password or private key + passphrase) are encrypted at rest.
Supports one-shot command execution (used by the server list "run command"
action and by the pipeline engine's ssh_exec step) and interactive PTY
shells (used by the web terminal).
"""

from __future__ import annotations

import io
import json

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
    if auth_type == "key":
        secret = security.normalize_key_text(secret)
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


def update_server(
    db: Session,
    server: models.SSHServer,
    host: str,
    port: int,
    username: str,
    auth_type: str,
    tags: str | None = None,
    secret: str | None = None,
    passphrase: str | None = None,
) -> None:
    """Update connection settings for an existing server.

    The server's `name` is intentionally not editable here -- pipeline steps
    and saved scripts reference servers by name, and silently breaking those
    references on a rename isn't worth the convenience. `secret`/`passphrase`
    follow the usual "leave blank to keep the current value" convention so
    editing the hostname doesn't force re-entering a password/key you didn't
    mean to change.
    """
    server.host = host
    server.port = port
    server.username = username
    server.auth_type = auth_type
    server.tags = tags
    if secret:
        if auth_type == "key":
            secret = security.normalize_key_text(secret)
        server.secret_encrypted = security.encrypt(secret)
    if passphrase:
        server.passphrase_encrypted = security.encrypt(passphrase)
    db.commit()


def delete_server(db: Session, server_id: int) -> None:
    server = db.get(models.SSHServer, server_id)
    if server:
        db.delete(server)
        db.commit()


_KEY_CLASSES = (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey, paramiko.DSSKey)


def load_private_key(text: str, passphrase: str | None) -> paramiko.PKey:
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
            pkey = load_private_key(security.normalize_key_text(secret), passphrase)
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
    # exec_command hands this straight to the remote shell -- CRLF line
    # endings (e.g. from a browser textarea or a script pasted from Windows)
    # would otherwise show up as a literal '\r' character in the middle of
    # paths/arguments on the remote end (bash doesn't treat it as a newline).
    command = command.replace("\r\n", "\n").replace("\r", "\n")
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


# --- remote docker (via SSH -- no Docker API/socket exposure needed) -------

_CONTAINER_ACTIONS = {"start": "start", "stop": "stop", "restart": "restart", "remove": "rm -f"}


def list_docker_containers(record: models.SSHServer, show_all: bool = True) -> list[dict]:
    flag = "-a " if show_all else ""
    command = "docker ps " + flag + "--format '{{json .}}' 2>&1"
    result = run_command(record, command, timeout=20)
    if result["exit_code"] != 0:
        raise SSHError(result["stdout"].strip() or result["stderr"].strip() or "docker ps failed")
    containers = []
    for line in result["stdout"].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return containers


def docker_container_action(record: models.SSHServer, container_id: str, action: str) -> str:
    action_cmd = _CONTAINER_ACTIONS.get(action)
    if not action_cmd:
        raise SSHError(f"Unsupported action '{action}'")
    result = run_command(record, f"docker {action_cmd} {container_id}", timeout=30)
    if result["exit_code"] != 0:
        raise SSHError(result["stderr"].strip() or result["stdout"].strip() or f"docker {action_cmd} failed")
    return result["stdout"]


def docker_container_logs(record: models.SSHServer, container_id: str, tail: int = 300) -> str:
    result = run_command(record, f"docker logs --tail {tail} --timestamps {container_id} 2>&1", timeout=20)
    return result["stdout"]


# --- automation scripts ------------------------------------------------------------

def list_scripts(db: Session, server_id: int) -> list[models.SSHScript]:
    return (
        db.query(models.SSHScript)
        .filter(models.SSHScript.server_id == server_id)
        .order_by(models.SSHScript.name)
        .all()
    )


def list_all_scripts(db: Session) -> list[models.SSHScript]:
    """All saved scripts across every server, for pickers like the pipeline builder."""
    return (
        db.query(models.SSHScript)
        .join(models.SSHServer)
        .order_by(models.SSHServer.name, models.SSHScript.name)
        .all()
    )


def get_script(db: Session, script_id: int) -> models.SSHScript:
    script = db.get(models.SSHScript, script_id)
    if not script:
        raise SSHError(f"Unknown script id {script_id}")
    return script


def create_script(db: Session, server_id: int, name: str, script_text: str) -> models.SSHScript:
    script_text = script_text.replace("\r\n", "\n").replace("\r", "\n")
    script = models.SSHScript(server_id=server_id, name=name.strip(), script_text=script_text)
    db.add(script)
    db.commit()
    db.refresh(script)
    return script


def delete_script(db: Session, script_id: int) -> None:
    script = db.get(models.SSHScript, script_id)
    if script:
        db.delete(script)
        db.commit()


def run_script(record: models.SSHServer, script_text: str, timeout: int = 300) -> dict:
    # exec_command hands the whole string to the remote login shell, so
    # multi-line scripts ("cd ... && git pull && docker compose up -d")
    # run exactly as if pasted into an interactive shell.
    return run_command(record, script_text, timeout=timeout)
