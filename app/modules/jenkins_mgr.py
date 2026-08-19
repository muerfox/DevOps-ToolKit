"""Jenkins control plane, built on python-jenkins.

Each registered instance stores a base URL + username + API token (token is
encrypted at rest, see app.security). Job names are treated as flat; nested
folder jobs are flattened one level using Jenkins' `fullname` field, which
covers the common case without pulling in full folder-tree navigation.
"""

from __future__ import annotations

import jenkins
from sqlalchemy.orm import Session

from .. import models, security


class JenkinsError(Exception):
    pass


def list_instances(db: Session) -> list[models.JenkinsInstance]:
    return db.query(models.JenkinsInstance).order_by(models.JenkinsInstance.name).all()


def get_instance(db: Session, name: str) -> models.JenkinsInstance:
    inst = db.query(models.JenkinsInstance).filter(models.JenkinsInstance.name == name).first()
    if not inst:
        raise JenkinsError(f"Unknown Jenkins instance '{name}'")
    return inst


def create_instance(db: Session, name: str, url: str, username: str, token: str) -> models.JenkinsInstance:
    inst = models.JenkinsInstance(
        name=name, url=url.rstrip("/"), username=username, token_encrypted=security.encrypt(token)
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return inst


def delete_instance(db: Session, instance_id: int) -> None:
    inst = db.get(models.JenkinsInstance, instance_id)
    if inst:
        db.delete(inst)
        db.commit()


def get_client(db: Session, instance_name: str) -> jenkins.Jenkins:
    inst = get_instance(db, instance_name)
    try:
        return jenkins.Jenkins(inst.url, username=inst.username, password=security.decrypt(inst.token_encrypted))
    except Exception as exc:  # noqa: BLE001
        raise JenkinsError(f"Could not connect to Jenkins at {inst.url}: {exc}") from exc


def ping(db: Session, instance_name: str) -> dict:
    try:
        server = get_client(db, instance_name)
        version = server.get_version()
        return {"ok": True, "version": version}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def list_jobs(server: jenkins.Jenkins) -> list[dict]:
    out = []
    try:
        jobs = server.get_all_jobs()
    except Exception:
        jobs = server.get_jobs()
    for j in jobs:
        out.append(
            {
                "name": j.get("fullname") or j.get("name"),
                "color": j.get("color", "notbuilt"),
                "url": j.get("url"),
            }
        )
    return out


def job_info(server: jenkins.Jenkins, job_name: str) -> dict:
    try:
        info = server.get_job_info(job_name)
    except jenkins.JenkinsException as exc:
        raise JenkinsError(str(exc)) from exc
    last_build = info.get("lastBuild") or {}
    last_number = last_build.get("number")
    builds = []
    for b in info.get("builds", [])[:15]:
        builds.append({"number": b.get("number")})
    return {
        "name": info.get("fullName") or job_name,
        "buildable": info.get("buildable"),
        "in_queue": info.get("inQueue"),
        "last_build_number": last_number,
        "builds": builds,
        "parameters": _extract_param_defs(info),
    }


def _extract_param_defs(info: dict) -> list[dict]:
    params = []
    for action in info.get("actions", []):
        for pd in action.get("parameterDefinitions", []) or []:
            params.append({"name": pd.get("name"), "default": (pd.get("defaultParameterValue") or {}).get("value")})
    return params


def build_info(server: jenkins.Jenkins, job_name: str, number: int) -> dict:
    try:
        info = server.get_build_info(job_name, number)
    except jenkins.JenkinsException as exc:
        raise JenkinsError(str(exc)) from exc
    return {
        "number": info.get("number"),
        "result": info.get("result"),
        "building": info.get("building"),
        "duration_ms": info.get("duration"),
        "timestamp": info.get("timestamp"),
    }


def console_output(server: jenkins.Jenkins, job_name: str, number: int) -> str:
    try:
        return server.get_build_console_output(job_name, number)
    except jenkins.JenkinsException as exc:
        raise JenkinsError(str(exc)) from exc


def trigger_build(server: jenkins.Jenkins, job_name: str, params: dict | None = None) -> int:
    try:
        queue_id = server.build_job(job_name, parameters=params or None)
    except jenkins.JenkinsException as exc:
        raise JenkinsError(str(exc)) from exc
    return queue_id


def resolve_queue_item(server: jenkins.Jenkins, queue_id: int) -> dict | None:
    try:
        item = server.get_queue_item(queue_id)
    except jenkins.JenkinsException:
        return None
    executable = item.get("executable")
    if executable:
        return {"job_name": item.get("task", {}).get("name"), "number": executable.get("number")}
    return None


def wait_for_build_number(server: jenkins.Jenkins, queue_id: int, timeout: int = 30) -> int | None:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        resolved = resolve_queue_item(server, queue_id)
        if resolved and resolved.get("number") is not None:
            return resolved["number"]
        time.sleep(1)
    return None
