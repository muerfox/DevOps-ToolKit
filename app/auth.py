from fastapi import Request, Depends, HTTPException
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


def require_admin(request: Request, db: Session = Depends(get_db)) -> models.User:
    user = require_login(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user
