"""JSON endpoints used by the UI for async loading (dashboard, metrics, search)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..auth import require_login
from .. import models
from ..modules import docker_mgr, k8s_mgr, jenkins_mgr, ssh_mgr, git_mgr, system_mgr

router = APIRouter(prefix="/api", dependencies=[Depends(require_login)])

STATUS_TIMEOUT = 8

_PAGES = [
    ("Dashboard", "/", "overview"),
    ("Docker containers", "/docker", "docker"),
    ("Docker images", "/docker/images", "docker"),
    ("Docker networks", "/docker/networks", "docker"),
    ("Docker volumes", "/docker/volumes", "docker"),
    ("Docker hosts", "/docker/hosts", "docker"),
    ("Swarm", "/docker/swarm", "swarm services stacks nodes"),
    ("Kubernetes", "/k8s", "k8s pods"),
    ("Kubernetes clusters", "/k8s/clusters", "k8s kubeconfig"),
    ("SSH servers", "/ssh", "ssh terminal"),
    ("Git repos", "/git", "git"),
    ("Jenkins", "/jenkins", "jenkins jobs"),
    ("Jenkins instances", "/jenkins/instances", "jenkins"),
    ("Pipelines", "/pipelines", "ci cd"),
    ("Settings", "/settings", "export import backup config about"),
]


@router.get("/search-index")
def search_index(db: Session = Depends(get_db)):
    items = [{"kind": "page", "title": t, "url": u, "keywords": k} for t, u, k in _PAGES]
    for h in docker_mgr.list_hosts(db):
        items.append({"kind": "docker", "title": h["name"], "url": f"/docker?host={h['name']}", "subtitle": h["base_url"]})
    for c in k8s_mgr.list_clusters(db):
        items.append({"kind": "cluster", "title": c.name, "url": f"/k8s/pods?cluster={c.name}", "subtitle": c.context_name or ""})
    for s in ssh_mgr.list_servers(db):
        items.append(
            {"kind": "ssh", "title": s.name, "url": f"/ssh/servers/{s.name}", "subtitle": f"{s.username}@{s.host} {s.tags or ''}"}
        )
        items.append({"kind": "terminal", "title": f"Terminal: {s.name}", "url": f"/ssh/servers/{s.name}/terminal", "subtitle": s.host})
    for r in git_mgr.list_repos(db):
        items.append({"kind": "repo", "title": r.name, "url": f"/git/repos/{r.name}", "subtitle": r.url})
    for i in jenkins_mgr.list_instances(db):
        items.append({"kind": "jenkins", "title": i.name, "url": f"/jenkins/jobs?instance={i.name}", "subtitle": i.url})
    for p in db.query(models.Pipeline).order_by(models.Pipeline.name).all():
        items.append({"kind": "pipeline", "title": p.name, "url": f"/pipelines/{p.id}", "subtitle": p.description or ""})
    return {"items": items}


def _docker_status(host_name: str) -> dict:
    db = SessionLocal()
    try:
        client = docker_mgr.get_client(db, host_name)
        try:
            return {"ok": True, **docker_mgr.engine_summary(client)}
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def _with_session(fn, name):
    db = SessionLocal()
    try:
        return fn(db, name)
    finally:
        db.close()


@router.get("/dashboard/status")
def dashboard_status(db: Session = Depends(get_db)):
    """Pings every registered integration in parallel; slow/unreachable ones time out individually."""
    tasks: dict[tuple[str, str], tuple] = {}
    for h in docker_mgr.list_hosts(db):
        tasks[("docker", h["name"])] = (_docker_status, h["name"])
    for c in k8s_mgr.list_clusters(db):
        tasks[("k8s", c.name)] = (_with_session, k8s_mgr.ping, c.name)
    for i in jenkins_mgr.list_instances(db):
        tasks[("jenkins", i.name)] = (_with_session, jenkins_mgr.ping, i.name)
    for s in ssh_mgr.list_servers(db):
        tasks[("ssh", s.name)] = (system_mgr.tcp_reachable, s.host, s.port or 22)

    result: dict[str, dict] = {"docker": {}, "k8s": {}, "jenkins": {}, "ssh": {}}
    if tasks:
        pool = ThreadPoolExecutor(max_workers=min(16, len(tasks)))
        futures = {pool.submit(*spec): key for key, spec in tasks.items()}
        done, _ = wait(futures, timeout=STATUS_TIMEOUT)
        for fut, (kind, name) in futures.items():
            if fut in done:
                try:
                    result[kind][name] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    result[kind][name] = {"ok": False, "error": str(exc)}
            else:
                result[kind][name] = {"ok": False, "error": f"timed out after {STATUS_TIMEOUT}s"}
        pool.shutdown(wait=False, cancel_futures=True)

    runs = db.query(models.PipelineRun).filter(models.PipelineRun.status.in_(("running", "queued"))).count()
    result["pipelines"] = {"active_runs": runs}
    return result


@router.get("/ssh/status")
def ssh_status(db: Session = Depends(get_db)):
    """Full login check (not just TCP) for every SSH server, in parallel."""
    servers = ssh_mgr.list_servers(db)
    if not servers:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(16, len(servers)))
    futures = {pool.submit(ssh_mgr.ping, s): s.name for s in servers}
    done, _ = wait(futures, timeout=15)
    out = {}
    for fut, name in futures.items():
        out[name] = fut.result() if fut in done else {"ok": False, "error": "timed out after 15s"}
    pool.shutdown(wait=False, cancel_futures=True)
    return out


@router.get("/system")
def system_metrics():
    return system_mgr.host_metrics()


@router.get("/docker/stats")
def docker_stats(host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    try:
        client = docker_mgr.get_client(db, host)
    except docker_mgr.DockerError as exc:
        return {"ok": False, "error": str(exc), "stats": {}}
    try:
        return {"ok": True, "stats": docker_mgr.container_stats(client)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "stats": {}}
    finally:
        client.close()
