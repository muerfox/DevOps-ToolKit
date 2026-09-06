from fastapi import Request, Depends, HTTPException
from sqlalchemy.orm import Session

from .database import get_db
from . import models


class NotAuthenticated(Exception):
    """Raised by require_login; caught by an exception handler that redirects to /login."""


class DeveloperRestricted(Exception):
    """Raised by require_operator when a developer-role user hits a page
    outside their sandbox; caught by an exception handler that redirects to
    /deploy -- their entire cockpit, from their point of view."""


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(models.User).filter(models.User.id == user_id).first()


def require_login(request: Request, db: Session = Depends(get_db)) -> models.User:
    user = get_current_user(request, db)
    if not user:
        raise NotAuthenticated()
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> models.User:
    user = require_login(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


def require_operator(request: Request, db: Session = Depends(get_db)) -> models.User:
    """Full cockpit access (everything except user management, which is
    require_admin's job) -- i.e. anyone who ISN'T a restricted developer
    account. Use this instead of require_login on any router that manages
    infrastructure (Docker, Kubernetes, Jenkins, Git, Servers, Ansible, the
    pipeline builder) so a developer account can't reach it even by URL."""
    user = require_login(request, db)
    if user.is_developer:
        raise DeveloperRestricted()
    return user


def websocket_user_is_operator(db: Session, user_id: int) -> bool:
    """For the three no-router-dependency websocket handlers (docker/k8s
    container-log tail, SSH terminal): they check request.session by hand
    since a router-level Depends(require_operator) can't apply to a
    websocket route (see docker_routes.py's ws_router comment), so they need
    this to reject a developer account the same way require_operator would."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    return bool(user) and not user.is_developer


def can_deploy_pipeline(db: Session, user: models.User, pipeline_id: int) -> bool:
    """Admin/operator accounts can deploy (run) any pipeline; a developer
    account only ones explicitly granted via PipelineAccess."""
    if not user.is_developer:
        return True
    return (
        db.query(models.PipelineAccess)
        .filter(models.PipelineAccess.user_id == user.id, models.PipelineAccess.pipeline_id == pipeline_id)
        .first()
        is not None
    )
