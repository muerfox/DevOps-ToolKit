from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from .. import models, security, login_throttle

router = APIRouter(tags=["auth"])


@router.get("/setup")
def setup_get(request: Request, db: Session = Depends(get_db)):
    if db.query(models.User).count() > 0:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("setup.html", {"request": request, "error": None})


@router.post("/setup")
def setup_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if db.query(models.User).count() > 0:
        return RedirectResponse("/login", status_code=303)

    error = None
    if len(username.strip()) < 3:
        error = "Username must be at least 3 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm:
        error = "Passwords do not match."

    if error:
        return templates.TemplateResponse("setup.html", {"request": request, "error": error}, status_code=400)

    user = models.User(username=username.strip(), password_hash=security.hash_password(password), is_admin=True)
    db.add(user)
    db.commit()
    db.refresh(user)

    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/login")
def login_get(request: Request, db: Session = Depends(get_db)):
    if db.query(models.User).count() == 0:
        return RedirectResponse("/setup", status_code=303)
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)

    ip = login_throttle.client_ip(request)
    status = login_throttle.check(db, ip)
    error = None
    if status["banned"]:
        error = (
            f"Too many failed login attempts from your IP. "
            f"Try again in {login_throttle.format_duration(status['retry_after_seconds'])}."
        )
    return templates.TemplateResponse("login.html", {"request": request, "error": error, "banned": status["banned"]})


@router.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    ip = login_throttle.client_ip(request)

    # Check the ban *before* touching the password/DB at all -- a banned IP
    # shouldn't get a free bcrypt verify (or username-enumeration timing) out
    # of every retry.
    status = login_throttle.check(db, ip)
    if status["banned"]:
        error = (
            f"Too many failed login attempts from your IP. "
            f"Try again in {login_throttle.format_duration(status['retry_after_seconds'])}."
        )
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": error, "banned": True}, status_code=429
        )

    user = db.query(models.User).filter(models.User.username == username.strip()).first()
    if not user or not security.verify_password(password, user.password_hash):
        result = login_throttle.record_failure(db, ip)
        if result["banned"]:
            error = (
                f"Too many failed login attempts from your IP. "
                f"Blocked for {login_throttle.format_duration(result['retry_after_seconds'])}."
            )
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": error, "banned": True}, status_code=429
            )
        error = "Invalid username or password."
        if result["attempts_remaining"] <= 1:
            error += f" {result['attempts_remaining']} attempt left before a temporary block."
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": error, "banned": False}, status_code=401
        )

    login_throttle.record_success(db, ip)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
