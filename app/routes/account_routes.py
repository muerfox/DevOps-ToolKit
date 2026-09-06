from fastapi import APIRouter, Depends, Request, Form
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from .. import models, security

# require_login only (not require_operator) -- a developer account is
# locked out of everything else in the cockpit but must still be able to
# change its own password.
router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/account")
def account_get(request: Request):
    return templates.TemplateResponse("account/change_password.html", {"request": request, "error": None, "saved": False})


@router.post("/account/change-password")
def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    # Not request.state.user: that object belongs to the attach_current_user
    # middleware's own (already-closed) DB session, so mutating it and
    # committing on *this* route's session would silently do nothing --
    # the session never tracked that instance. Fetch it fresh here instead.
    user = db.query(models.User).filter(models.User.id == request.session["user_id"]).first()
    error = None
    if not security.verify_password(current_password, user.password_hash):
        error = "Current password is incorrect."
    elif len(new_password) < 8:
        error = "New password must be at least 8 characters."
    elif new_password != confirm_password:
        error = "New passwords do not match."

    if error:
        return templates.TemplateResponse(
            "account/change_password.html", {"request": request, "error": error, "saved": False}, status_code=400
        )

    user.password_hash = security.hash_password(new_password)
    db.commit()
    return templates.TemplateResponse("account/change_password.html", {"request": request, "error": None, "saved": True})
