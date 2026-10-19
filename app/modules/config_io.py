"""Export/import of the cockpit's configuration as a portable JSON document.

Covers Docker hosts, Kubernetes clusters, Jenkins instances, Git repos, SSH
servers (+ saved scripts) and pipelines. Users and pipeline run history are
not included.

Credentials (SSH passwords/keys, Jenkins tokens, Git PATs, kubeconfigs) are
only exported when a passphrase is given; they are then re-encrypted with a
key derived from that passphrase (PBKDF2-SHA256 -> Fernet), so the file can be
imported into a different cockpit instance with a different encryption key.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy.orm import Session

from ..config import settings
from .. import models, security
from . import git_mgr, k8s_mgr, pipeline_engine

FORMAT = "devops-cockpit-config"
FORMAT_VERSION = 1
KDF_ITERATIONS = 390_000


class ConfigError(Exception):
    pass


def _fernet_from_passphrase(passphrase: str, salt: bytes, iterations: int) -> Fernet:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(passphrase.encode())))


# --- export ------------------------------------------------------------

def export_config(db: Session, passphrase: str | None = None) -> dict:
    include_secrets = bool(passphrase)
    doc: dict = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": settings.version,
        "exported_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "includes_credentials": include_secrets,
    }
    seal = lambda value: None  # noqa: E731
    if include_secrets:
        salt = os.urandom(16)
        doc["kdf"] = {"name": "pbkdf2-sha256", "salt": base64.b64encode(salt).decode(), "iterations": KDF_ITERATIONS}
        f = _fernet_from_passphrase(passphrase, salt, KDF_ITERATIONS)
        seal = lambda value: f.encrypt(value.encode()).decode() if value else None  # noqa: E731

    doc["docker_hosts"] = [
        {"name": h.name, "base_url": h.base_url, "tls_verify": bool(h.tls_verify)}
        for h in db.query(models.DockerHost).order_by(models.DockerHost.name)
    ]

    clusters = []
    for c in db.query(models.K8sCluster).order_by(models.K8sCluster.name):
        kubeconfig = None
        if include_secrets:
            try:
                kubeconfig = Path(c.kubeconfig_path).read_text()
            except OSError:
                kubeconfig = None
        clusters.append({"name": c.name, "context_name": c.context_name, "kubeconfig": seal(kubeconfig)})
    doc["k8s_clusters"] = clusters

    doc["jenkins_instances"] = [
        {"name": i.name, "url": i.url, "username": i.username, "token": seal(security.decrypt(i.token_encrypted))}
        for i in db.query(models.JenkinsInstance).order_by(models.JenkinsInstance.name)
    ]

    doc["git_repos"] = [
        {
            "name": r.name,
            "url": r.url,
            "branch": r.branch,
            "username": r.username,
            "credential": seal(security.decrypt(r.credential_encrypted)),
        }
        for r in db.query(models.GitRepo).order_by(models.GitRepo.name)
    ]

    servers = []
    script_refs: dict[int, dict] = {}
    for s in db.query(models.SSHServer).order_by(models.SSHServer.name):
        scripts = []
        for sc in sorted(s.scripts, key=lambda x: x.name):
            scripts.append({"name": sc.name, "script_text": sc.script_text})
            script_refs[sc.id] = {"server": s.name, "script": sc.name}
        servers.append(
            {
                "name": s.name,
                "host": s.host,
                "port": s.port,
                "username": s.username,
                "auth_type": s.auth_type,
                "tags": s.tags,
                "secret": seal(security.decrypt(s.secret_encrypted)),
                "passphrase": seal(security.decrypt(s.passphrase_encrypted)),
                "scripts": scripts,
            }
        )
    doc["ssh_servers"] = servers

    pipelines = []
    for p in db.query(models.Pipeline).order_by(models.Pipeline.name):
        steps = pipeline_engine.get_steps(p)
        for step in steps:
            # Script ids are local to this database; export a name reference instead.
            sid = (step.get("params") or {}).get("script_id")
            if step.get("type") == "ssh_run_script" and sid:
                ref = script_refs.get(int(sid)) if str(sid).isdigit() else None
                step["params"] = {k: v for k, v in step["params"].items() if k != "script_id"}
                if ref:
                    step["params"]["script_ref"] = ref
        pipelines.append(
            {
                "name": p.name,
                "description": p.description,
                "steps": steps,
                "env_text": p.env_text or "",
                "schedule_cron": p.schedule_cron,
                "schedule_enabled": bool(p.schedule_enabled),
                "webhook_enabled": bool(p.webhook_token),
                "webhook_branch": p.webhook_branch,
            }
        )
    doc["pipelines"] = pipelines
    return doc


# --- import ------------------------------------------------------------

class ImportReport:
    def __init__(self):
        self.created: list[str] = []
        self.updated: list[str] = []
        self.skipped: list[str] = []
        self.warnings: list[str] = []

    def summary(self) -> str:
        parts = [f"{len(self.created)} created", f"{len(self.updated)} updated", f"{len(self.skipped)} skipped"]
        return "Import finished: " + ", ".join(parts) + "."

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "warnings": self.warnings}


def import_config(db: Session, doc: dict, passphrase: str | None = None, overwrite: bool = False) -> ImportReport:
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ConfigError("Not a DevOps Cockpit config export (missing 'format' marker).")
    if int(doc.get("format_version", 0)) > FORMAT_VERSION:
        raise ConfigError("This export was made by a newer cockpit version; upgrade before importing.")

    fernet: Fernet | None = None
    if doc.get("includes_credentials"):
        if not passphrase:
            raise ConfigError("This export contains credentials; enter the passphrase it was exported with.")
        kdf = doc.get("kdf") or {}
        fernet = _fernet_from_passphrase(passphrase, base64.b64decode(kdf["salt"]), int(kdf["iterations"]))
        probe = next(_iter_sealed(doc), None)
        if probe:
            try:
                fernet.decrypt(probe.encode())
            except InvalidToken as exc:
                raise ConfigError("Wrong passphrase for this export.") from exc

    def unseal(value):
        if not value or fernet is None:
            return None
        return fernet.decrypt(value.encode()).decode()

    report = ImportReport()

    def existing(model, name):
        return db.query(model).filter(model.name == name).first()

    def decide(label: str, row) -> bool:
        """True if we should write this item."""
        if row and not overwrite:
            report.skipped.append(f"{label} (already exists)")
            return False
        return True

    for h in doc.get("docker_hosts") or []:
        label = f"docker host '{h['name']}'"
        if h["name"] == "local":
            continue
        row = existing(models.DockerHost, h["name"])
        if not decide(label, row):
            continue
        row = row or models.DockerHost(name=h["name"])
        (report.updated if row.id else report.created).append(label)
        row.base_url, row.tls_verify = h["base_url"], bool(h.get("tls_verify"))
        db.add(row)
    db.commit()

    for c in doc.get("k8s_clusters") or []:
        label = f"cluster '{c['name']}'"
        row = existing(models.K8sCluster, c["name"])
        if not decide(label, row):
            continue
        kubeconfig = unseal(c.get("kubeconfig"))
        if not kubeconfig:
            report.skipped.append(f"{label} (no kubeconfig in export — re-export with a passphrase)")
            continue
        if row:
            Path(row.kubeconfig_path).write_text(kubeconfig)
            row.context_name = c.get("context_name")
            report.updated.append(label)
        else:
            k8s_mgr.save_cluster(db, c["name"], kubeconfig, c.get("context_name"))
            report.created.append(label)
    db.commit()

    for i in doc.get("jenkins_instances") or []:
        label = f"jenkins '{i['name']}'"
        row = existing(models.JenkinsInstance, i["name"])
        if not decide(label, row):
            continue
        token = unseal(i.get("token"))
        if not token and not row:
            report.skipped.append(f"{label} (no API token in export)")
            continue
        row = row or models.JenkinsInstance(name=i["name"])
        (report.updated if row.id else report.created).append(label)
        row.url, row.username = i["url"], i["username"]
        if token:
            row.token_encrypted = security.encrypt(token)
        db.add(row)
    db.commit()

    for s in doc.get("ssh_servers") or []:
        label = f"ssh server '{s['name']}'"
        row = existing(models.SSHServer, s["name"])
        if decide(label, row):
            secret = unseal(s.get("secret"))
            row = row or models.SSHServer(name=s["name"])
            (report.updated if row.id else report.created).append(label)
            row.host, row.port, row.username = s["host"], int(s.get("port") or 22), s["username"]
            row.auth_type, row.tags = s.get("auth_type") or "password", s.get("tags")
            if secret:
                row.secret_encrypted = security.encrypt(secret)
                row.passphrase_encrypted = security.encrypt(unseal(s.get("passphrase")))
            elif not row.secret_encrypted:
                report.warnings.append(f"{label}: imported without credentials — edit it before connecting")
            db.add(row)
            db.commit()
        if not row:
            continue
        for sc in s.get("scripts") or []:
            match = next((x for x in row.scripts if x.name == sc["name"]), None)
            slabel = f"script '{s['name']}/{sc['name']}'"
            if match and not overwrite:
                report.skipped.append(f"{slabel} (already exists)")
                continue
            if match:
                match.script_text = sc["script_text"]
                report.updated.append(slabel)
            else:
                db.add(models.SSHScript(server_id=row.id, name=sc["name"], script_text=sc["script_text"]))
                report.created.append(slabel)
        db.commit()

    for r in doc.get("git_repos") or []:
        label = f"repo '{r['name']}'"
        row = existing(models.GitRepo, r["name"])
        if not decide(label, row):
            continue
        credential = unseal(r.get("credential"))
        if row:
            row.url, row.branch, row.username = r["url"], r.get("branch") or "main", r.get("username")
            if credential:
                row.credential_encrypted = security.encrypt(credential)
            db.commit()
            report.updated.append(label)
            continue
        try:
            git_mgr.register_and_clone(db, r["name"], r["url"], r.get("branch") or "main", r.get("username"), credential)
            report.created.append(f"{label} (cloned)")
        except git_mgr.GitError as exc:
            db.rollback()
            report.warnings.append(f"{label}: clone failed, skipped — {exc}")

    script_ids = {
        (sc.server.name, sc.name): sc.id for sc in db.query(models.SSHScript).all() if sc.server is not None
    }
    for p in doc.get("pipelines") or []:
        label = f"pipeline '{p['name']}'"
        row = existing(models.Pipeline, p["name"])
        if not decide(label, row):
            continue
        steps = []
        for step in p.get("steps") or []:
            params = dict(step.get("params") or {})
            ref = params.pop("script_ref", None)
            if step.get("type") == "ssh_run_script":
                ref = ref or {}
                sid = script_ids.get((ref.get("server"), ref.get("script")))
                if sid:
                    params["script_id"] = str(sid)
                else:
                    report.warnings.append(f"{label}: saved script {ref} not found; fix that step")
            steps.append({"type": step.get("type"), "params": params})
        row = row or models.Pipeline(name=p["name"])
        (report.updated if row.id else report.created).append(label)
        row.description = p.get("description")
        row.steps_json = json.dumps(steps)
        row.env_text = p.get("env_text") or ""
        row.schedule_cron = p.get("schedule_cron")
        row.schedule_enabled = bool(p.get("schedule_enabled")) and pipeline_engine.validate_cron(p.get("schedule_cron") or "")
        row.last_scheduled_at = models.utcnow()
        row.webhook_branch = p.get("webhook_branch")
        if p.get("webhook_enabled") and not row.webhook_token:
            row.webhook_token = secrets.token_urlsafe(24)
            report.warnings.append(f"{label}: has a new webhook URL — update it in your git host")
        db.add(row)
        db.commit()

    return report


def _iter_sealed(doc: dict):
    for c in doc.get("k8s_clusters") or []:
        if c.get("kubeconfig"):
            yield c["kubeconfig"]
    for i in doc.get("jenkins_instances") or []:
        if i.get("token"):
            yield i["token"]
    for r in doc.get("git_repos") or []:
        if r.get("credential"):
            yield r["credential"]
    for s in doc.get("ssh_servers") or []:
        if s.get("secret"):
            yield s["secret"]
