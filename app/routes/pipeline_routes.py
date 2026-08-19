from fastapi import APIRouter, Depends, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from .. import models
from ..modules import pipeline_engine, docker_mgr, k8s_mgr, jenkins_mgr, git_mgr, ssh_mgr

router = APIRouter(dependencies=[Depends(require_login)])

FORM_FIELDS = [
    "host", "repo", "context_subdir", "context_path", "dockerfile", "tag",
    "stack_name", "compose_path", "compose_text",
    "cluster", "namespace", "manifest_path", "manifest_text", "deployment",
    "instance", "job_name", "params_text",
    "server", "command", "timeout", "cwd",
]


def _picker_data(db: Session) -> dict:
    return {
        "docker_hosts": docker_mgr.list_hosts(db),
        "repos": git_mgr.list_repos(db),
        "clusters": k8s_mgr.list_clusters(db),
        "jenkins_instances": jenkins_mgr.list_instances(db),
        "ssh_servers": ssh_mgr.list_servers(db),
        "step_types": pipeline_engine.STEP_TYPES,
    }


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
    runs = (
        db.query(models.PipelineRun)
        .filter(models.PipelineRun.pipeline_id == pipeline_id)
        .order_by(models.PipelineRun.started_at.desc())
        .limit(15)
        .all()
    )
    ctx = {"request": request, "pipeline": pipeline, "steps": steps, "runs": runs}
    ctx.update(_picker_data(db))
    return templates.TemplateResponse("pipelines/builder.html", ctx)


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
    run = pipeline_engine.start_run(db, pipeline)
    background_tasks.add_task(pipeline_engine.run_pipeline_sync, pipeline.id, run.id)
    return RedirectResponse(f"/pipelines/runs/{run.id}", status_code=303)


@router.get("/pipelines/runs/{run_id}")
def pipeline_run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.PipelineRun, run_id)
    return templates.TemplateResponse("pipelines/run.html", {"request": request, "run": run})


@router.get("/pipelines/runs/{run_id}/status")
def pipeline_run_status(run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.PipelineRun, run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"status": run.status, "log_text": run.log_text}
