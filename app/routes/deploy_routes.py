from fastapi import APIRouter, Depends, Request, BackgroundTasks, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login, can_deploy_pipeline
from .. import models
from ..modules import pipeline_engine
from .pipeline_routes import start_and_queue

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/deploy")
def deploy_index(request: Request, db: Session = Depends(get_db)):
    """The whole cockpit, from a developer account's point of view: pick a
    pipeline you've been granted, click Deploy. No steps, servers, or other
    infrastructure details are shown here -- an admin/operator manages all
    of that; this page just runs what's already configured."""
    user = request.state.user
    all_pipelines = pipeline_engine.list_pipelines(db)
    if user.is_developer:
        granted_ids = {
            pa.pipeline_id
            for pa in db.query(models.PipelineAccess).filter(models.PipelineAccess.user_id == user.id).all()
        }
        pipelines = [p for p in all_pipelines if p.id in granted_ids]
    else:
        # Admin/operator accounts can deploy anything -- shown here too as a
        # quick one-click shortcut, same list they'd otherwise dig for under
        # Pipelines.
        pipelines = all_pipelines

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
        "deploy/index.html",
        {"request": request, "pipelines": pipelines, "latest_runs": latest_runs, "is_developer": user.is_developer},
    )


@router.post("/deploy/{pipeline_id}/run")
def deploy_run(pipeline_id: int, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    user = request.state.user
    if not can_deploy_pipeline(db, user, pipeline_id):
        raise HTTPException(status_code=404, detail="Not found")
    pipeline = pipeline_engine.get_pipeline(db, pipeline_id)
    run = start_and_queue(db, background_tasks, pipeline)
    return RedirectResponse(f"/pipelines/runs/{run.id}", status_code=303)
