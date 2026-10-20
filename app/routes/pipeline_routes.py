import hashlib
import hmac
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..templating import templates
from ..auth import require_login, require_operator, can_deploy_pipeline
from ..flash import flash
from .. import models
from ..modules import pipeline_engine, docker_mgr, k8s_mgr, jenkins_mgr, git_mgr, ssh_mgr

# Pipeline builder / settings: operators only (developers use /deploy).
router = APIRouter(dependencies=[Depends(require_operator)])

# Run detail / cancel / status: any logged-in user who can see the run
# (developers land here after Deploy). Each mutating handler still checks
# can_deploy_pipeline() where needed.
runs_router = APIRouter(dependencies=[Depends(require_login)])

# Webhook triggers authenticate with the pipeline's own token (URL path,
# bearer header, or GitHub HMAC secret) -- no login session.
webhook_router = APIRouter()
hooks_router = APIRouter(tags=["webhooks"])


def start_and_queue(db: Session, background_tasks: BackgroundTasks, pipeline: models.Pipeline) -> models.PipelineRun:
    """Compatibility wrapper used by /deploy. `background_tasks` is unused:
    launch_run already starts a daemon thread."""
    del background_tasks  # kept for call-site compatibility
    run_id = pipeline_engine.launch_run(pipeline.id, trigger="manual", trigger_info="deploy")
    run = db.get(models.PipelineRun, run_id)
    if not run:
        # Fresh session may not see the row until expire; re-query.
        db.expire_all()
        run = db.get(models.PipelineRun, run_id)
    return run


def _picker_data(db: Session) -> dict:
    return {
        "docker_hosts": docker_mgr.list_hosts(db),
        "repos": git_mgr.list_repos(db),
        "clusters": k8s_mgr.list_clusters(db),
        "jenkins_instances": jenkins_mgr.list_instances(db),
        "ssh_servers": ssh_mgr.list_servers(db),
        "ssh_scripts": ssh_mgr.list_all_scripts(db),
        "repo_scripts": git_mgr.list_all_scripts(db),
        "step_types": pipeline_engine.STEP_TYPES,
    }


def _describe_step_params(db: Session, step: dict) -> dict:
    """Human-readable version of a step's params for the builder's step list."""
    params = {k: v for k, v in step.get("params", {}).items() if k not in pipeline_engine.COMMON_FIELDS}
    if step.get("type") == "ssh_run_script" and params.get("script_id"):
        try:
            script = ssh_mgr.get_script(db, int(params["script_id"]))
            rest = {k: v for k, v in params.items() if k != "script_id"}
            return {"script": f"{script.server.name} / {script.name}", **rest}
        except (ssh_mgr.SSHError, ValueError):
            return params
    if step.get("type") == "repo_run_script" and params.get("repo_script_id"):
        try:
            script = git_mgr.get_script(db, int(params["repo_script_id"]))
            rest = {k: v for k, v in params.items() if k != "repo_script_id"}
            return {"script": f"{script.repo.name} / {script.name}", **rest}
        except (git_mgr.GitError, ValueError):
            return params
    return params


def _step_params_from_form(form, step_def: dict) -> dict:
    fields = step_def["fields"] + pipeline_engine.COMMON_FIELDS
    return {f: form.get(f) for f in fields if form.get(f)}


def _webhook_url(request: Request, pipeline: models.Pipeline) -> str | None:
    if not pipeline.webhook_token:
        return None
    base = settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/hooks/pipelines/{pipeline.webhook_token}"


def _get_pipeline_or_404(db: Session, pipeline_id: int) -> models.Pipeline:
    pipeline = db.get(models.Pipeline, pipeline_id)
    if not pipeline:
        raise HTTPException(status_code=404, detail="Unknown pipeline")
    return pipeline


@router.get("/pipelines")
def pipelines_index(request: Request, db: Session = Depends(get_db)):
    pipelines = pipeline_engine.list_pipelines(db)
    latest_runs = {}
    next_runs = {}
    now = models.utcnow()
    for p in pipelines:
        latest_runs[p.id] = (
            db.query(models.PipelineRun)
            .filter(models.PipelineRun.pipeline_id == p.id)
            .order_by(models.PipelineRun.id.desc())
            .first()
        )
        if p.schedule_enabled and pipeline_engine.validate_cron(p.schedule_cron or ""):
            next_runs[p.id] = pipeline_engine.next_fire_time(p.schedule_cron, p.last_scheduled_at or now)
    return templates.TemplateResponse(
        "pipelines/index.html",
        {
            "request": request,
            "pipelines": pipelines,
            "latest_runs": latest_runs,
            "next_runs": next_runs,
            "step_counts": {p.id: len(pipeline_engine.get_steps(p)) for p in pipelines},
        },
    )


@router.post("/pipelines/create")
def pipelines_create(
    request: Request, name: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)
):
    pipeline = pipeline_engine.create_pipeline(db, name.strip(), description.strip())
    flash(request, f"Pipeline '{pipeline.name}' created — add some steps.", "success")
    return RedirectResponse(f"/pipelines/{pipeline.id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/delete")
def pipelines_delete(request: Request, pipeline_id: int, db: Session = Depends(get_db)):
    pipeline_engine.delete_pipeline(db, pipeline_id)
    flash(request, "Pipeline deleted.", "success")
    return RedirectResponse("/pipelines", status_code=303)


@router.post("/pipelines/{pipeline_id}/duplicate")
def pipelines_duplicate(request: Request, pipeline_id: int, db: Session = Depends(get_db)):
    copy = pipeline_engine.duplicate_pipeline(db, pipeline_engine.get_pipeline(db, pipeline_id))
    flash(request, f"Created '{copy.name}' (schedule disabled on the copy).", "success")
    return RedirectResponse(f"/pipelines/{copy.id}", status_code=303)


@router.get("/pipelines/{pipeline_id}")
def pipeline_detail(request: Request, pipeline_id: int, db: Session = Depends(get_db)):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    steps = pipeline_engine.get_steps(pipeline)
    for step in steps:
        step["display_params"] = _describe_step_params(db, step)
    runs = (
        db.query(models.PipelineRun)
        .filter(models.PipelineRun.pipeline_id == pipeline_id)
        .order_by(models.PipelineRun.id.desc())
        .limit(15)
        .all()
    )
    next_run = None
    if pipeline.schedule_enabled and pipeline_engine.validate_cron(pipeline.schedule_cron or ""):
        next_run = pipeline_engine.next_fire_time(pipeline.schedule_cron, pipeline.last_scheduled_at or models.utcnow())
    developer_users = db.query(models.User).filter(models.User.is_developer.is_(True)).order_by(models.User.username).all()
    granted_user_ids = {
        pa.user_id
        for pa in db.query(models.PipelineAccess).filter(models.PipelineAccess.pipeline_id == pipeline_id).all()
    }
    ctx = {
        "request": request,
        "pipeline": pipeline,
        "steps": steps,
        "steps_json": json.dumps(steps),
        "runs": runs,
        "webhook_url": _webhook_url(request, pipeline),
        "next_run": next_run,
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


@router.post("/pipelines/{pipeline_id}/settings")
def pipeline_settings(
    request: Request,
    pipeline_id: int,
    name: str = Form(...),
    description: str = Form(""),
    env_text: str = Form(""),
    schedule_cron: str = Form(""),
    schedule_enabled: bool = Form(False),
    webhook_enabled: bool = Form(False),
    webhook_branch: str = Form(""),
    db: Session = Depends(get_db),
):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    try:
        pipeline_engine.update_settings(
            db,
            pipeline,
            name=name,
            description=description,
            env_text=env_text,
            schedule_cron=schedule_cron,
            schedule_enabled=schedule_enabled,
            webhook_enabled=webhook_enabled,
            webhook_branch=webhook_branch,
        )
        flash(request, "Pipeline settings saved.", "success")
    except pipeline_engine.StepError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse(f"/pipelines/{pipeline_id}#settings", status_code=303)


@router.post("/pipelines/{pipeline_id}/webhook/regenerate")
def pipeline_webhook_regenerate(request: Request, pipeline_id: int, db: Session = Depends(get_db)):
    pipeline_engine.regenerate_webhook_token(db, pipeline_engine.get_pipeline(db, pipeline_id))
    flash(request, "New webhook URL generated — the old one no longer works.", "success")
    return RedirectResponse(f"/pipelines/{pipeline_id}#settings", status_code=303)


# Legacy aliases used by older builder templates on origin/main.
@router.post("/pipelines/{pipeline_id}/regenerate-webhook-token")
def pipeline_regenerate_webhook_token_legacy(request: Request, pipeline_id: int, db: Session = Depends(get_db)):
    return pipeline_webhook_regenerate(request, pipeline_id, db)


@router.post("/pipelines/{pipeline_id}/webhook-branch")
def pipeline_set_webhook_branch(pipeline_id: int, webhook_branch: str = Form(""), db: Session = Depends(get_db)):
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    pipeline.webhook_branch = webhook_branch.strip() or None
    db.commit()
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/steps/add")
async def pipeline_add_step(pipeline_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    step_type = form.get("step_type")
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    step_def = pipeline_engine.STEP_TYPES.get(step_type)
    if step_def:
        pipeline_engine.add_step(db, pipeline, step_type, _step_params_from_form(form, step_def))
        flash(request, f"Added step: {step_def['label']}", "success")
    return RedirectResponse(f"/pipelines/{pipeline_id}", status_code=303)


@router.post("/pipelines/{pipeline_id}/steps/{index}/update")
async def pipeline_update_step(pipeline_id: int, index: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    step_type = form.get("step_type")
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    step_def = pipeline_engine.STEP_TYPES.get(step_type)
    try:
        if not step_def:
            raise pipeline_engine.StepError(f"Unknown step type '{step_type}'")
        pipeline_engine.update_step(db, pipeline, index, step_type, _step_params_from_form(form, step_def))
        flash(request, f"Updated step {index + 1}.", "success")
    except pipeline_engine.StepError as exc:
        flash(request, str(exc), "error")
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
def pipeline_run(
    request: Request,
    pipeline_id: int,
    vars_text: str = Form(""),
    db: Session = Depends(get_db),
):
    user = request.state.user
    if not can_deploy_pipeline(db, user, pipeline_id):
        raise HTTPException(status_code=403, detail="Not allowed to run this pipeline")
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    overrides = pipeline_engine.parse_kv_text(vars_text)
    info = ("overrides: " + ", ".join(sorted(overrides))) if overrides else None
    run_id = pipeline_engine.launch_run(pipeline.id, trigger="manual", trigger_info=info, variables=overrides)
    return RedirectResponse(f"/pipelines/runs/{run_id}", status_code=303)


@runs_router.get("/pipelines/runs/{run_id}")
def pipeline_run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.PipelineRun, run_id)
    return templates.TemplateResponse("pipelines/run.html", {"request": request, "run": run})


@runs_router.post("/pipelines/runs/{run_id}/cancel")
def pipeline_run_cancel(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.PipelineRun, run_id)
    if run and not can_deploy_pipeline(db, request.state.user, run.pipeline_id):
        raise HTTPException(status_code=403, detail="Not allowed to cancel this run")
    if pipeline_engine.cancel_run(db, run_id):
        flash(request, f"Cancelling run #{run_id}…", "info")
    else:
        flash(request, f"Run #{run_id} is not running.", "error")
    return RedirectResponse(f"/pipelines/runs/{run_id}", status_code=303)


@runs_router.post("/pipelines/runs/{run_id}/rerun")
def pipeline_run_rerun(request: Request, run_id: int, db: Session = Depends(get_db)):
    old = db.get(models.PipelineRun, run_id)
    if not old:
        return RedirectResponse("/pipelines", status_code=303)
    if not can_deploy_pipeline(db, request.state.user, old.pipeline_id):
        raise HTTPException(status_code=403, detail="Not allowed to run this pipeline")
    overrides = {}
    if old.trigger == "manual" and old.trigger_info and old.trigger_info.startswith("overrides: "):
        final = json.loads(old.vars_json or "{}")
        overrides = {k: final[k] for k in old.trigger_info[len("overrides: "):].split(", ") if k in final}
    new_id = pipeline_engine.launch_run(
        old.pipeline_id, trigger="manual", trigger_info=f"re-run of #{run_id}", variables=overrides
    )
    return RedirectResponse(f"/pipelines/runs/{new_id}", status_code=303)


@runs_router.get("/pipelines/runs/{run_id}/status")
def pipeline_run_status(run_id: int, offset: int = 0, db: Session = Depends(get_db)):
    run = db.get(models.PipelineRun, run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    text = run.log_text or ""
    return {
        "status": run.status,
        "log_text": text[offset:] if 0 <= offset <= len(text) else text,
        "offset": len(text),
        "reset": not (0 <= offset <= len(text)),
        "duration": run.duration_seconds,
    }


# --- token-in-URL hooks (docs/github-webhooks.md) ----------------------------


def _pipeline_for_hook_token(db: Session, token: str) -> models.Pipeline | None:
    if len(token) < 16:
        return None
    return db.query(models.Pipeline).filter(models.Pipeline.webhook_token == token).first()


@hooks_router.get("/hooks/pipelines/{token}")
def pipeline_webhook_probe(token: str, db: Session = Depends(get_db)):
    """Cheap reachability check (browsers, curl, uptime monitors). Does not
    start a run. GitHub only POSTs; if GET works but GitHub deliveries time
    out, the path is almost always a firewall/WAF blocking GitHub's IPs."""
    pipeline = _pipeline_for_hook_token(db, token)
    if not pipeline:
        return JSONResponse({"error": "unknown webhook"}, status_code=404)
    return {"ok": True, "pipeline": pipeline.name, "hint": "POST to this URL to trigger a run"}


@hooks_router.post("/hooks/pipelines/{token}")
async def pipeline_webhook(token: str, request: Request, db: Session = Depends(get_db)):
    pipeline = _pipeline_for_hook_token(db, token)
    if not pipeline:
        return JSONResponse({"error": "unknown webhook"}, status_code=404)

    headers = {k.lower(): v for k, v in request.headers.items()}
    if headers.get("x-github-event") == "ping":
        return {"ok": True, "pong": True, "pipeline": pipeline.name}

    payload = None
    body = await request.body()
    if body:
        try:
            payload = json.loads(body)
        except ValueError:
            form = await request.form()
            if form.get("payload"):  # GitHub "application/x-www-form-urlencoded" deliveries
                try:
                    payload = json.loads(form["payload"])
                except ValueError:
                    payload = None
    if payload is not None and not isinstance(payload, dict):
        payload = None

    parsed = pipeline_engine.parse_webhook(headers, payload)
    if parsed["skip"]:
        return {"ok": True, "skipped": parsed["skip"]}
    if not pipeline_engine.branch_matches(pipeline.webhook_branch, parsed["branch"], parsed["tag"]):
        name = parsed["branch"] or parsed["tag"]
        return {"ok": True, "skipped": f"'{name}' does not match '{pipeline.webhook_branch}'"}

    run_id = pipeline_engine.launch_run(
        pipeline.id, trigger="webhook", trigger_info=parsed["info"], variables=parsed["variables"]
    )
    return {"ok": True, "run_id": run_id, "pipeline": pipeline.name}


# --- legacy /pipelines/{id}/trigger + native GitHub HMAC routes -------------


@webhook_router.post("/pipelines/{pipeline_id}/trigger")
def pipeline_webhook_trigger(
    pipeline_id: int,
    token: str | None = None,
    x_webhook_token: str | None = Header(None),
    db: Session = Depends(get_db),
):
    """Generic trigger for any CI system that can POST with ?token= or
    X-Webhook-Token. Kept for callers already pointed at origin/main."""
    pipeline = _get_pipeline_or_404(db, pipeline_id)
    provided = x_webhook_token or token or ""
    if not pipeline.webhook_token or not hmac.compare_digest(provided, pipeline.webhook_token):
        raise HTTPException(status_code=403, detail="Invalid or missing webhook token")
    run_id = pipeline_engine.launch_run(pipeline.id, trigger="webhook", trigger_info="trigger endpoint")
    return {"run_id": run_id, "status_url": f"/pipelines/runs/{run_id}/status"}


@webhook_router.post("/pipelines/{pipeline_id}/webhook/github")
async def pipeline_webhook_github(
    pipeline_id: int,
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
    db: Session = Depends(get_db),
):
    """Native GitHub webhook: Secret = pipeline webhook_token, HMAC-SHA256."""
    pipeline = _get_pipeline_or_404(db, pipeline_id)
    if not pipeline.webhook_token:
        raise HTTPException(status_code=403, detail="No webhook token configured for this pipeline")

    body = await request.body()
    expected = "sha256=" + hmac.new(pipeline.webhook_token.encode(), body, hashlib.sha256).hexdigest()
    if not x_hub_signature_256 or not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid signature")

    if x_github_event == "ping":
        return {"pong": True}

    if x_github_event not in (None, "push"):
        return {"skipped": True, "reason": f"ignoring event '{x_github_event}'"}

    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        payload = None
    parsed = pipeline_engine.parse_webhook(headers, payload if isinstance(payload, dict) else None)
    if parsed["skip"]:
        return {"ok": True, "skipped": parsed["skip"]}
    if not pipeline_engine.branch_matches(pipeline.webhook_branch, parsed["branch"], parsed["tag"]):
        name = parsed["branch"] or parsed["tag"]
        return {"skipped": True, "reason": f"'{name}' does not match '{pipeline.webhook_branch}'"}

    run_id = pipeline_engine.launch_run(
        pipeline.id, trigger="webhook", trigger_info=parsed["info"], variables=parsed["variables"]
    )
    return {"run_id": run_id, "status_url": f"/pipelines/runs/{run_id}/status"}
