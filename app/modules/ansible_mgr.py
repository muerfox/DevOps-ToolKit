"""Ansible integration: run playbooks against registered SSH servers.

Targets are chosen at run time -- a single server, a "group" (servers that
share a tag on the Servers page), or all registered servers -- and a dynamic
inventory is generated on the fly from the SSHServer table for exactly that
set of hosts. Ansible's implicit `all` group covers whatever hosts we put in
the inventory, so every generated/custom playbook here just targets
`hosts: all`; there's no need for our own custom group names in the
inventory file.

Everything (inventory, decrypted private key copies) is written to a
private temp directory for the duration of one run and removed afterwards,
the same pattern used elsewhere in this app for one-shot secrets (e.g. the
docker stack-deploy compose file).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

import yaml
from cryptography.hazmat.primitives import serialization
from sqlalchemy.orm import Session

from ..config import BASE_DIR
from .. import models, security

LogFn = Callable[[str], None]


class AnsibleError(Exception):
    pass


# --- targets: single server / group-by-tag / all ---------------------------

def list_groups(db: Session) -> list[str]:
    """Distinct tags across all registered servers -- these double as the
    "groups" a playbook can be run against."""
    tags: set[str] = set()
    for (raw,) in db.query(models.SSHServer.tags).all():
        for t in (raw or "").split(","):
            t = t.strip()
            if t:
                tags.add(t)
    return sorted(tags)


def resolve_targets(db: Session, target_type: str, target_value: str | None) -> list[models.SSHServer]:
    if target_type == "all":
        servers = db.query(models.SSHServer).order_by(models.SSHServer.name).all()
    elif target_type == "group":
        if not target_value:
            raise AnsibleError("No group selected")
        servers = [
            s
            for s in db.query(models.SSHServer).order_by(models.SSHServer.name).all()
            if target_value in [t.strip() for t in (s.tags or "").split(",") if t.strip()]
        ]
    elif target_type == "server":
        if not target_value:
            raise AnsibleError("No server selected")
        server = db.query(models.SSHServer).filter(models.SSHServer.name == target_value).first()
        if not server:
            raise AnsibleError(f"Unknown server '{target_value}'")
        servers = [server]
    else:
        raise AnsibleError(f"Unknown target type '{target_type}'")

    if not servers:
        raise AnsibleError("No servers matched this target")
    return servers


def describe_target(target_type: str, target_value: str | None) -> str:
    if target_type == "all":
        return "all servers"
    if target_type == "group":
        return f"group '{target_value}'"
    return f"server '{target_value}'"


# --- inventory + execution ------------------------------------------------

def _sanitize_alias(server: models.SSHServer) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", server.name)
    return f"srv{server.id}_{safe}"


def _strip_key_passphrase(key_text: str, passphrase: str) -> str:
    try:
        key_obj = serialization.load_ssh_private_key(key_text.encode(), password=passphrase.encode())
    except (ValueError, TypeError) as exc:
        raise AnsibleError(f"Could not decrypt private key: {exc}") from exc
    return key_obj.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _write_inventory(servers: list[models.SSHServer], run_dir: Path) -> Path:
    lines = []
    for server in servers:
        alias = _sanitize_alias(server)
        secret = security.decrypt(server.secret_encrypted)
        vars_ = [
            f"ansible_host={server.host}",
            f"ansible_port={server.port}",
            f"ansible_user={server.username}",
        ]
        if server.auth_type == "key":
            passphrase = security.decrypt(server.passphrase_encrypted)
            key_path = run_dir / f"{alias}.pem"
            if passphrase:
                # Strip the passphrase so ssh/ansible never has to prompt for
                # one interactively. Re-serializing via paramiko's own
                # write_private_key() would be an option, but it's simply
                # unimplemented for Ed25519Key in this paramiko version (the
                # most common modern key type) -- the `cryptography` library
                # (already a dependency) handles every key type OpenSSH does.
                key_path.write_text(_strip_key_passphrase(secret, passphrase))
            else:
                # Already validated parseable at server-registration time
                # (ssh_mgr) and unencrypted -- use it as-is.
                key_path.write_text(secret)
            key_path.chmod(0o600)
            vars_ += [
                "ansible_connection=ssh",
                f"ansible_ssh_private_key_file={key_path}",
            ]
        else:
            # paramiko connection plugin supports password auth natively,
            # no sshpass binary required.
            vars_ += [
                "ansible_connection=paramiko",
                f"ansible_password={secret}",
            ]
        lines.append(f"{alias} " + " ".join(vars_))

    inventory_path = run_dir / "inventory.ini"
    inventory_path.write_text("\n".join(lines) + "\n")
    inventory_path.chmod(0o600)
    return inventory_path


def _env() -> dict:
    env = os.environ.copy()
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    env["ANSIBLE_RETRY_FILES_ENABLED"] = "False"
    env["ANSIBLE_NOCOWS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_playbook(
    db: Session,
    target_type: str,
    target_value: str | None,
    playbook_yaml: str,
    log: LogFn,
    extra_vars: dict | None = None,
) -> None:
    servers = resolve_targets(db, target_type, target_value)
    log(f"Target: {describe_target(target_type, target_value)} ({len(servers)} host(s))\n")
    for s in servers:
        log(f"  - {s.name} ({s.username}@{s.host}:{s.port})\n")
    log("\n")

    run_dir = Path(tempfile.mkdtemp(prefix="ansible-run-", dir=str(BASE_DIR / "data")))
    try:
        run_dir.chmod(0o700)
        inventory_path = _write_inventory(servers, run_dir)
        playbook_path = run_dir / "playbook.yml"
        playbook_path.write_text(playbook_yaml)

        cmd = ["ansible-playbook", "-i", str(inventory_path), str(playbook_path)]
        if extra_vars:
            import json

            cmd += ["--extra-vars", json.dumps(extra_vars)]

        log(f"$ ansible-playbook {playbook_path.name}\n\n")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=_env()
            )
        except FileNotFoundError as exc:
            raise AnsibleError(
                "ansible-playbook is not installed in this environment (pip install ansible-core)"
            ) from exc

        for line in proc.stdout:
            log(line)
        proc.wait()
        if proc.returncode != 0:
            raise AnsibleError(f"ansible-playbook exited {proc.returncode}")
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


# --- saved playbooks CRUD ------------------------------------------------

def list_playbooks(db: Session) -> list[models.AnsiblePlaybook]:
    return db.query(models.AnsiblePlaybook).order_by(models.AnsiblePlaybook.name).all()


def get_playbook(db: Session, playbook_id: int) -> models.AnsiblePlaybook:
    playbook = db.get(models.AnsiblePlaybook, playbook_id)
    if not playbook:
        raise AnsibleError(f"Unknown playbook id {playbook_id}")
    return playbook


def create_playbook(db: Session, name: str, description: str, playbook_yaml: str) -> models.AnsiblePlaybook:
    playbook = models.AnsiblePlaybook(name=name, description=description, playbook_yaml=playbook_yaml)
    db.add(playbook)
    db.commit()
    db.refresh(playbook)
    return playbook


def update_playbook(db: Session, playbook: models.AnsiblePlaybook, description: str, playbook_yaml: str) -> None:
    playbook.description = description
    playbook.playbook_yaml = playbook_yaml
    db.commit()


def delete_playbook(db: Session, playbook_id: int) -> None:
    playbook = db.get(models.AnsiblePlaybook, playbook_id)
    if playbook:
        db.delete(playbook)
        db.commit()


# --- run history ------------------------------------------------------

def start_run(db: Session, label: str, target_type: str, target_value: str | None, playbook_yaml: str) -> models.AnsibleRun:
    run = models.AnsibleRun(
        label=label,
        target_type=target_type,
        target_value=target_value,
        playbook_yaml=playbook_yaml,
        status="running",
        log_text="",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def execute_run(run_id: int) -> None:
    """Executed in a worker thread via BackgroundTasks. Owns its own DB session."""
    from ..database import SessionLocal
    import datetime as dt

    db = SessionLocal()
    try:
        run = db.get(models.AnsibleRun, run_id)
        if not run:
            return

        buffer: list[str] = []

        def log(text: str) -> None:
            if not text:
                return
            buffer.append(text)
            run.log_text = "".join(buffer)
            db.commit()

        try:
            run_playbook(db, run.target_type, run.target_value, run.playbook_yaml, log)
            run.status = "success"
            log("\n=== Playbook run succeeded ===\n")
        except AnsibleError as exc:
            run.status = "failed"
            log(f"\n=== Playbook run FAILED: {exc} ===\n")
        finally:
            run.finished_at = dt.datetime.utcnow()
            db.commit()
    finally:
        db.close()


# --- quick-action playbook generators ------------------------------------
#
# These build a plain Python data structure and hand it to yaml.safe_dump()
# rather than splicing user input into hand-written YAML text with f-strings.
# Usernames/passwords/file content/paths can contain quotes, colons, newlines,
# etc. that would otherwise corrupt the YAML or -- worse -- let user input
# inject extra playbook structure; a real YAML serializer escapes all of that
# correctly no matter what's in the string.

def _dump_playbook(play: dict) -> str:
    return yaml.safe_dump([play], default_flow_style=False, sort_keys=False, width=4096)


def playbook_manage_user(
    username: str,
    state: str = "present",
    password: str | None = None,
    sudo: bool = False,
    ssh_pubkey: str | None = None,
) -> str:
    if state == "absent":
        tasks = [
            {
                "name": f"Remove user {username}",
                "ansible.builtin.user": {"name": username, "state": "absent", "remove": True},
            }
        ]
        play_vars = {}
    else:
        user_module: dict = {"name": username, "state": "present", "shell": "/bin/bash", "append": True}
        if sudo:
            user_module["groups"] = "sudo"
        play_vars = {}
        if password:
            user_module["password"] = "{{ new_password | password_hash('sha512') }}"
            play_vars["new_password"] = password
        tasks = [
            {"name": f"Create/update user {username}", "ansible.builtin.user": user_module, "register": "user_result"}
        ]
        if ssh_pubkey:
            # ansible.posix.authorized_key would be the one-liner here, but
            # that's a separate collection this app doesn't install/require;
            # ansible.builtin.file + lineinfile do the same job using only
            # modules that ship with ansible-core.
            play_vars["ssh_pubkey"] = ssh_pubkey
            tasks.append(
                {
                    "name": f"Ensure .ssh directory exists for {username}",
                    "ansible.builtin.file": {
                        "path": "{{ user_result.home }}/.ssh",
                        "state": "directory",
                        "owner": username,
                        "mode": "0700",
                    },
                }
            )
            tasks.append(
                {
                    "name": f"Authorize SSH key for {username}",
                    "ansible.builtin.lineinfile": {
                        "path": "{{ user_result.home }}/.ssh/authorized_keys",
                        "line": "{{ ssh_pubkey }}",
                        "create": True,
                        "owner": username,
                        "mode": "0600",
                    },
                }
            )

    play = {"name": f"Manage user {username}", "hosts": "all", "become": True, "gather_facts": False, "tasks": tasks}
    if play_vars:
        play["vars"] = play_vars
    return _dump_playbook(play)


def playbook_install_updates() -> str:
    play = {
        "name": "Install OS updates",
        "hosts": "all",
        "become": True,
        "gather_facts": True,
        "tasks": [
            {
                "name": "Update apt packages (Debian/Ubuntu)",
                "ansible.builtin.apt": {"update_cache": True, "upgrade": "dist", "autoremove": True},
                "when": "ansible_facts['pkg_mgr'] == 'apt'",
            },
            {
                "name": "Update dnf/yum packages (RHEL/Fedora/CentOS)",
                "ansible.builtin.dnf": {"name": "*", "state": "latest", "update_cache": True},
                "when": "ansible_facts['pkg_mgr'] in ['dnf', 'yum']",
            },
        ],
    }
    return _dump_playbook(play)


def playbook_upload_file(dest_path: str, content: str, mode: str | None = None, owner: str | None = None) -> str:
    copy_args = {"dest": dest_path, "content": "{{ file_content }}"}
    if mode:
        copy_args["mode"] = mode
    if owner:
        copy_args["owner"] = owner
    play = {
        "name": f"Upload file to {dest_path}",
        "hosts": "all",
        "become": True,
        "gather_facts": False,
        "vars": {"file_content": content},
        "tasks": [{"name": f"Write {dest_path}", "ansible.builtin.copy": copy_args}],
    }
    return _dump_playbook(play)
