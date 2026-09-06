import concurrent.futures

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_operator
from .. import models
from ..modules import jenkins_mgr, git_mgr, ssh_mgr, pipeline_engine

router = APIRouter(dependencies=[Depends(require_operator)])


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ping_servers(servers: list[models.SSHServer]) -> dict[int, dict]:
    """Pings every server concurrently (not one-by-one) so the dashboard stays
    fast no matter how many servers -- or how many *unreachable* ones, which
    would otherwise each cost the full 10s connect timeout -- are registered."""
    if not servers:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(servers), 16)) as pool:
        futures = {pool.submit(ssh_mgr.ping, s): s.id for s in servers}
        return {futures[f]: f.result() for f in concurrent.futures.as_completed(futures)}


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    # Docker/Swarm/Kubernetes are hidden from the dashboard for now (see
    # base.html) -- skipped here too so the page isn't pinging every
    # registered docker host / k8s cluster on every load for cards nobody
    # can navigate to.
    jenkins_instances = jenkins_mgr.list_instances(db)
    jenkins_status = {i.name: jenkins_mgr.ping(db, i.name) for i in jenkins_instances}

    ssh_servers = ssh_mgr.list_servers(db)
    repos = git_mgr.list_repos(db)
    pipelines = pipeline_engine.list_pipelines(db)

    server_ping = _ping_servers(ssh_servers)

    # id -> repo_id / server_id for every saved script, so a repo_run_script
    # or ssh_run_script step can be traced back to what it actually runs
    # against without a query per step.
    script_repo_id = {s.id: s.repo_id for s in git_mgr.list_all_scripts(db)}
    ssh_script_server_id = {s.id: s.server_id for s in ssh_mgr.list_all_scripts(db)}
    repo_by_name = {r.name: r for r in repos}
    server_by_name = {s.name: s for s in ssh_servers}

    pipeline_steps = {p.id: pipeline_engine.get_steps(p) for p in pipelines}

    def repo_ids_touched(steps: list[dict]) -> set[int]:
        ids: set[int] = set()
        for step in steps:
            params = step.get("params", {})
            if step.get("type") == "git_pull" and params.get("repo") in repo_by_name:
                ids.add(repo_by_name[params["repo"]].id)
            elif step.get("type") == "repo_run_script":
                rid = script_repo_id.get(_to_int(params.get("repo_script_id")))
                if rid:
                    ids.add(rid)
        return ids

    def server_ids_touched(steps: list[dict]) -> set[int]:
        ids: set[int] = set()
        for step in steps:
            params = step.get("params", {})
            if step.get("type") == "ssh_exec" and params.get("server") in server_by_name:
                ids.add(server_by_name[params["server"]].id)
            elif step.get("type") == "ssh_run_script":
                sid = ssh_script_server_id.get(_to_int(params.get("script_id")))
                if sid:
                    ids.add(sid)
        return ids

    pipeline_repo_ids = {p.id: repo_ids_touched(pipeline_steps[p.id]) for p in pipelines}
    pipeline_server_ids = {p.id: server_ids_touched(pipeline_steps[p.id]) for p in pipelines}

    latest_runs = {}
    for p in pipelines:
        latest_runs[p.id] = (
            db.query(models.PipelineRun)
            .filter(models.PipelineRun.pipeline_id == p.id)
            .order_by(models.PipelineRun.started_at.desc())
            .first()
        )

    def pipeline_view(p: models.Pipeline) -> dict:
        return {"pipeline": p, "latest_run": latest_runs[p.id]}

    def repo_card(repo: models.GitRepo) -> dict:
        matching = sorted(
            (p for p in pipelines if repo.id in pipeline_repo_ids[p.id]), key=lambda p: p.name
        )
        return {"repo": repo, "pipelines": [pipeline_view(p) for p in matching]}

    covered_pipeline_ids: set[int] = set()
    server_cards = []
    for s in ssh_servers:
        repo_cards = [repo_card(r) for r in repos if r.deploy_server_id == s.id]
        for rc in repo_cards:
            covered_pipeline_ids.update(pv["pipeline"].id for pv in rc["pipelines"])
        other = sorted(
            (
                p
                for p in pipelines
                if s.id in pipeline_server_ids[p.id] and p.id not in covered_pipeline_ids
            ),
            key=lambda p: p.name,
        )
        covered_pipeline_ids.update(p.id for p in other)
        ping = server_ping.get(s.id, {})
        server_cards.append(
            {
                "server": s,
                "online": ping.get("ok"),
                "ping_error": ping.get("error"),
                "repos": repo_cards,
                "other_pipelines": [pipeline_view(p) for p in other],
            }
        )

    local_repo_cards = [repo_card(r) for r in repos if r.deploy_server_id is None]
    for rc in local_repo_cards:
        covered_pipeline_ids.update(pv["pipeline"].id for pv in rc["pipelines"])

    unassigned_pipelines = [pipeline_view(p) for p in pipelines if p.id not in covered_pipeline_ids]

    recent_runs = (
        db.query(models.PipelineRun).order_by(models.PipelineRun.started_at.desc()).limit(8).all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "jenkins_instances": jenkins_instances,
            "jenkins_status": jenkins_status,
            "server_cards": server_cards,
            "local_repo_cards": local_repo_cards,
            "unassigned_pipelines": unassigned_pipelines,
            "servers_count": len(ssh_servers),
            "repos_count": len(repos),
            "pipelines_count": len(pipelines),
            "recent_runs": recent_runs,
        },
    )
