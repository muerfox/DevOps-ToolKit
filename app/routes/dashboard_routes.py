from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_operator
from .. import models
from ..modules import jenkins_mgr, git_mgr, ssh_mgr

router = APIRouter(dependencies=[Depends(require_operator)])


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

    pipelines = db.query(models.Pipeline).count()
    recent_runs = (
        db.query(models.PipelineRun).order_by(models.PipelineRun.started_at.desc()).limit(6).all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "jenkins_instances": jenkins_instances,
            "jenkins_status": jenkins_status,
            "ssh_servers": ssh_servers,
            "repos": repos,
            "pipelines_count": pipelines,
            "recent_runs": recent_runs,
        },
    )
