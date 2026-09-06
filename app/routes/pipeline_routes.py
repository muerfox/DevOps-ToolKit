import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, Request, Form, BackgroundTasks, Header, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login, require_operator, can_deploy_pipeline
from .. import models
from ..modules import pipeline_engine, docker_mgr, k8s_mgr, jenkins_mgr, git_mgr, ssh_mgr

# Building/editing pipelines is operator+ territory (blocked for a developer
# account, same as Docker/K8s/Servers/etc).
router = APIRouter(dependencies=[Depends(require_operator)])

# Watching a run's output is different: a developer account needs to see the
# outcome of a deploy it just kicked off from /deploy. Login-only at the
# router level; each handler below checks can_deploy_pipeline() itself so a
# developer can only ever see runs of pipelines it's actually been granted.
runs_router = APIRouter(dependencies=[Depends(require_login)])

# Webhook trigger endpoints live on their own router with NO login dependency
# -- GitHub (or any CI) can't do an interactive session login, so these
# authenticate with the pipeline's own webhook_token instead (as a bearer
# token/query param, or as a GitHub HMAC signature). See docker_routes.py's
# ws_router comment for why login-gated routers can't just be reused here.
webhook_router = APIRouter()


def start_and_queue(db: Session, background_tasks: BackgroundTasks, pipeline: models.Pipeline) -> models.PipelineRun:
    run = pipeline_engine.start_run(db, pipeline)
    background_tasks.add_task(pipeline_engine.run_pipeline_sync, pipeline.id, run.id)
    return run

FORM_FIELDS = [
    "host", "repo", "context_subdir", "context_path", "dockerfile", "tag",
    "stack_name", "project_name", "compose_path", "compose_text",
    "cluster", "namespace", "manifest_path", "manifest_text", "deployment",
    "instance", "job_name", "params_text",
    "server", "command", "timeout", "cwd", "script_id",
]


def _picker_data(db: Session) -> dict:
    return {
        "docker_hosts": docker_mgr.list_hosts(db),
        "repos": git_mgr.list_repos(db),
        "clusters": k8s_mgr.list_clusters(db),
        "jenkins_instances": jenkins_mgr.list_instances(db),
        "ssh_servers": ssh_mgr.list_servers(db),
        "ssh_scripts": ssh_mgr.list_all_scripts(db),
        "step_types": pipeline_engine.STEP_TYPES,
    }


def _describe_step_params(db: Session, step: dict) -> dict:
    """Human-readable version of a step's params for the builder's step list."""
    params = step.get("params", {})
    if step.get("type") == "ssh_run_script" and params.get("script_id"):
        try:
            script = ssh_mgr.get_script(db, int(params["script_id"]))
            rest = {k: v for k, v in params.items() if k != "script_id"}
            return {"script": f"{script.server.name} / {script.name}", **rest}
        except (ssh_mgr.SSHError, ValueError):
            return params
    return params


@router.get("/pipelines")
def pipelines_index(request: Request, db: Session = Depends(get_db)):
    pipelines = pipeline_engine.list_pipelines(db)
    latest_runs = {}
    for p in pipelines:
        run = (
            db.query(models.PipelineRun)
            .filter(models.PipelineRun.pipeline_id == p.id)
            .order_by(models.PipelineRun.started_at.desc())
            .first()
        )
        latest_runs[p.id] = run
    return templates.TemplateResponse(
        "pipelines/index.html", {"request": request, "pipelines": pipelines, "latest_runs": latest_runs}
    )


@router.post("/pipelines/create")
def pipelines_create(name: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)):
    pipeline = pipeline_engine.create_pipeline(db, name.strip(), description.strip())
    return RedirectResponse(f"/pipelines/{pipeline.id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/delete")
def pipelines_delete(pipeline_id: int, db: Session = Depends(get_db)):
    pipeline_engine.delete_pipeline(db, pipeline_id)
    return RedirectResponse("/pipelines", status_code=303)


@router.get("/pipelines/{pipeline_id}")
def pipeline_detail(request: Request, pipeline_id: int, db: Session = Depends(get_db)):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    steps = pipeline_engine.get_steps(pipeline)
    for step in steps:
        step["display_params"] = _describe_step_params(db, step)
    runs = (
        db.query(models.PipelineRun)
        .filter(models.PipelineRun.pipeline_id == pipeline_id)
        .order_by(models.PipelineRun.started_at.desc())
        .limit(15)
        .all()
    )
    developer_users = db.query(models.User).filter(models.User.is_developer.is_(True)).order_by(models.User.username).all()
    granted_user_ids = {
        pa.user_id
        for pa in db.query(models.PipelineAccess).filter(models.PipelineAccess.pipeline_id == pipeline_id).all()
    }
    ctx = {
        "request": request,
        "pipeline": pipeline,
        "steps": steps,
        "runs": runs,
        "developer_users": developer_users,
        "granted_user_ids": granted_user_ids,
    }
    ctx.update(_picker_data(db))
    return templates.TemplateResponse("pipelines/builder.html", ctx)


@router.post("/pipelines/{pipeline_id}/access/grant")
def pipeline_access_grant(pipeline_id: int, user_id: int = Form(...), db: Session = Depends(get_db)):
    exists = (
        db.query(models.PipelineAccess)
        .filter(models.PipelineAccess.pipeline_id == pipeline_id, models.PipelineAccess.user_id == user_id)
        .first()
    )
    if not exists:
        db.add(models.PipelineAccess(pipeline_id=pipeline_id, user_id=user_id))
        db.commit()
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/access/revoke")
def pipeline_access_revoke(pipeline_id: int, user_id: int = Form(...), db: Session = Depends(get_db)):
    db.query(models.PipelineAccess).filter(
        models.PipelineAccess.pipeline_id == pipeline_id, models.PipelineAccess.user_id == user_id
    ).delete()
    db.commit()
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/steps/add")
async def pipeline_add_step(pipeline_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    step_type = form.get("step_type")
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    step_def = pipeline_engine.STEP_TYPES.get(step_type)
    if step_def:
        params = {f: form.get(f) for f in step_def["fields"] if form.get(f)}
        pipeline_engine.add_step(db, pipeline, step_type, params)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/steps/{index}/remove")
def pipeline_remove_step(pipeline_id: int, index: int, db: Session = Depends(get_db)):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    pipeline_engine.remove_step(db, pipeline, index)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/steps/{index}/move")
def pipeline_move_step(pipeline_id: int, index: int, direction: int = Form(...), db: Session = Depends(get_db)):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    pipeline_engine.move_step(db, pipeline, index, direction)
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/run")
def pipeline_run(pipeline_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    run = start_and_queue(db, background_tasks, pipeline)
    return RedirectResponse(f"/pipelines/runs/{run.id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/regenerate-webhook-token")
def pipeline_regenerate_webhook_token(pipeline_id: int, db: Session = Depends(get_db)):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    pipeline.webhook_token = models.generate_webhook_token()
    db.commit()
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/webhook-branch")
def pipeline_set_webhook_branch(pipeline_id: int, webhook_branch: str = Form(""), db: Session = Depends(get_db)):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    pipeline.webhook_branch = webhook_branch.strip() or None
    db.commit()
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@runs_router.get("/pipelines/runs/{run_id}")
def pipeline_run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.PipelineRun, run_id)
    if run and not can_deploy_pipeline(db, request.state.user, run.pipeline_id):
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse("pipelines/run.html", {"request": request, "run": run})


@runs_router.get("/pipelines/runs/{run_id}/status")
def pipeline_run_status(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.PipelineRun, run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not can_deploy_pipeline(db, request.state.user, run.pipeline_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"status": run.status, "log_text": run.log_text}


# --- webhook triggers (no login -- authenticated by the pipeline's own token) --

def _get_pipeline_or_404(db: Session, pipeline_id: int) -> models.Pipeline:
    pipeline = db.get(models.Pipeline, pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Unknown pipeline")
    return pipeline


@webhook_router.post("/pipelines/{pipeline_id}/trigger")
def pipeline_webhook_trigger(
    pipeline_id: int,
    background_tasks: BackgroundTasks,
    token: str | None = None,
    x_webhook_token: str | None = Header(None),
    db: Session = Depends(get_db),
):
    """Generic trigger for any CI system that can do a plain authenticated
    POST -- a GitHub Actions workflow step, GitLab CI, Bitbucket Pipelines,
    a Jenkins post-build step, or a one-line curl. Accepts the token either
    as ?token=... or an X-Webhook-Token header."""
    pipeline = _get_pipeline_or_404(db, pipeline_id)
    provided = x_webhook_token or token or ""
    if not pipeline.webhook_token or not hmac.compare_digest(provided, pipeline.webhook_token):
        raise HTTPException(status_code=403, detail="Invalid or missing webhook token")
    run = start_and_queue(db, background_tasks, pipeline)
    return {"run_id": run.id, "status_url": f"/pipelines/runs/{run.id}/status"}


@webhook_router.post("/pipelines/{pipeline_id}/webhook/github")
async def pipeline_webhook_github(
    pipeline_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
    db: Session = Depends(get_db),
):
    """Point a native GitHub repo webhook (Settings -> Webhooks) straight at
    this URL, with the pipeline's webhook token as the webhook's "Secret" --
    no GitHub Actions workflow file needed at all. Verifies the payload via
    GitHub's HMAC-SHA256 signature instead of a bearer token, since that's
    how GitHub itself authenticates a webhook delivery."""
    pipeline = _get_pipeline_or_404(db, pipeline_id)
    if not pipeline.webhook_token:
        raise HTTPException(status_code=403, detail="No webhook token configured for this pipeline")

    body = await request.body()
    expected = "sha256=" + hmac.new(pipeline.webhook_token.encode(), body, hashlib.sha256).hexdigest()
    if not x_hub_signature_256 or not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

    if x_github_event == "ping":
        # GitHub sends this once, immediately after you save the webhook --
        # acknowledge it without actually kicking off a run.
        return {"pong": True}

    if x_github_event not in (None, "push"):
        # If the webhook was configured to send "all events" rather than
        # GitHub's default "just the push event", don't deploy on a
        # pull_request/issue_comment/whatever just because it happened to
        # arrive with a valid signature.
        return {"skipped": True, "reason": f"ignoring event '{x_github_event}'"}

    if pipeline.webhook_branch:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        ref = payload.get("ref", "")
        expected_ref = f"refs/heads/{pipeline.webhook_branch}"
        if ref and ref != expected_ref:
            return {"skipped": True, "reason": f"ref '{ref}' does not match configured branch '{pipeline.webhook_branch}'"}

    run = start_and_queue(db, background_tasks, pipeline)
    return {"run_id": run.id, "status_url": f"/pipelines/runs/{run.id}/status"}
