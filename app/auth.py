from fastapi import Request, Depends
from sqlalchemy.orm import Session

from .database import get_db
from . import models


class NotAuthenticated(Exception):
    """Raised by require_login; caught by an exception handler that redirects to /login."""


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
