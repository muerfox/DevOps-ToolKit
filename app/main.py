from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import settings, BASE_DIR
from .database import init_db, SessionLocal
from .auth import NotAuthenticated
from . import models
from .routes import (
    auth_routes,
    docker_routes,
    k8s_routes,
    jenkins_routes,
    git_routes,
    ssh_routes,
    pipeline_routes,
    dashboard_routes,
)

app = FastAPI(title=settings.app_name)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


@app.middleware("http")
async def attach_current_user(request: Request, call_next):
    """Makes request.state.user available to every template without each
    route having to fetch + pass it explicitly."""
    request.state.user = None
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if user_id:
        db = SessionLocal()
        try:
            request.state.user = db.query(models.User).filter(models.User.id == user_id).first()
        finally:
            db.close()
    return await call_next(request)


# Registered after attach_current_user so it wraps it (Starlette runs
# later-added middleware first), guaranteeing request.session exists by the
# time attach_current_user reads it.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=303)


@app.on_event("startup")
def on_startup():
    init_db()


app.include_router(auth_routes.router)
app.include_router(dashboard_routes.router)
app.include_router(docker_routes.router)
app.include_router(k8s_routes.router)
app.include_router(jenkins_routes.router)
app.include_router(git_routes.router)
app.include_router(ssh_routes.router)
app.include_router(pipeline_routes.router)
