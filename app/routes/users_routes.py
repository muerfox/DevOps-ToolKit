from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_admin
from .. import models, security

router = APIRouter(dependencies=[Depends(require_admin)])


def _users_page(request: Request, db: Session, error: str | None = None, status_code: int = 200):
    users = db.query(models.User).order_by(models.User.username).all()
    return templates.TemplateResponse(
        "users/index.html", {"request": request, "users": users, "error": error}, status_code=status_code
    )


@router.get("/users")
def users_index(request: Request, db: Session = Depends(get_db)):
    return _users_page(request, db)


@router.post("/users/create")
def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    role: str = Form("operator"),
    db: Session = Depends(get_db),
):
    username = username.strip()
    error = None
    if len(username) < 3:
        error = "Username must be at least 3 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm:
        error = "Passwords do not match."
    elif role not in ("admin", "operator", "developer"):
        error = "Invalid role."
    elif db.query(models.User).filter(models.User.username == username).first():
        error = "That username is already taken."

    if error:
        return _users_page(request, db, error=error, status_code=400)

    user = models.User(
        username=username,
        password_hash=security.hash_password(password),
        is_admin=(role == "admin"),
        is_developer=(role == "developer"),
    )
    db.add(user)
    db.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete")
def users_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
    target = db.get(models.User, user_id)
    if not target:
        return RedirectResponse("/users", status_code=303)

    # Self-delete is blocked unconditionally (not just "if you're the last
    # admin"), which as a side effect makes zero-admins unreachable through
    # this UI: deleting *someone else* always leaves the current admin in
    # place, so admin count can never drop below 1.
    current = request.state.user
    if current and target.id == current.id:
        return _users_page(
            request, db, error="You can't remove the account you're currently logged in as.", status_code=400
        )

    db.delete(target)
    db.commit()
    return RedirectResponse("/users", status_code=303)
