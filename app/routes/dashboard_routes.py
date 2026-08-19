from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from .. import models
from ..modules import docker_mgr, k8s_mgr, jenkins_mgr, git_mgr, ssh_mgr

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    local_docker = docker_mgr.ping(db, docker_mgr.LOCAL_HOST_NAME)
    docker_hosts = docker_mgr.list_hosts(db)

    clusters = k8s_mgr.list_clusters(db)
    cluster_status = {c.name: k8s_mgr.ping(db, c.name) for c in clusters}

    jenkins_instances = jenkins_mgr.list_instances(db)
    jenkins_status = {i.name: jenkins_mgr.ping(db, i.name) for i in jenkins_instances}

    ssh_servers = ssh_mgr.list_servers(db)
    repos = git_mgr.list_repos(db)

    pipelines = db.query(models.Pipeline).count()
    recent_runs = (
        db.query(models.PipelineRun).order_by(models.PipelineRun.started_at.desc()).limit(6).all()
    )

    swarm_active = False
    if local_docker.get("ok"):
        client = docker_mgr.get_client(db)
        try:
            swarm_active = docker_mgr.swarm_info(client).get("active", False)
        except docker_mgr.DockerError:
            pass
        finally:
            client.close()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "local_docker": local_docker,
            "docker_hosts": docker_hosts,
            "swarm_active": swarm_active,
            "clusters": clusters,
            "cluster_status": cluster_status,
            "jenkins_instances": jenkins_instances,
            "jenkins_status": jenkins_status,
            "ssh_servers": ssh_servers,
            "repos": repos,
            "pipelines_count": pipelines,
            "recent_runs": recent_runs,
        },
    )
