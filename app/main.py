import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware

from .config import settings, BASE_DIR, INSECURE_SECRET_KEYS
from .database import init_db, SessionLocal
from .auth import NotAuthenticated, DeveloperRestricted
from .flash import flash
from . import models
from .modules import pipeline_engine, system_mgr
from .routes import (
    auth_routes,
    docker_routes,
    k8s_routes,
    jenkins_routes,
    git_routes,
    ssh_routes,
    pipeline_routes,
    dashboard_routes,
    users_routes,
    ansible_routes,
    deploy_routes,
    account_routes,
    diagnostics_routes,
    api_routes,
    settings_routes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cockpit")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    recovered = pipeline_engine.recover_interrupted_runs()
    if recovered:
        log.warning("marked %d pipeline run(s) interrupted by the last shutdown as failed", recovered)
    if settings.secret_key in INSECURE_SECRET_KEYS:
        log.warning("COCKPIT_SECRET_KEY is not set to a random value; run `./cockpit init` or edit .env")
    system_mgr.prime()
    pipeline_engine.scheduler.start()
    log.info("DevOps Cockpit v%s ready", settings.version)
    yield
    pipeline_engine.scheduler.stop()


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

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
# later-added middleware first), guaranteeing request.state.user exists by the
# time attach_current_user reads it.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, same_site="lax")


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(DeveloperRestricted)
async def developer_restricted_handler(request: Request, exc: DeveloperRestricted):
    return RedirectResponse(url="/deploy", status_code=303)


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    # Almost always a duplicate name on a create form.
    flash(request, "That name is already in use — pick a different one.", "error")
    referer = request.headers.get("referer") or "/"
    return RedirectResponse(url=referer, status_code=303)


@app.get("/healthz", include_in_schema=False)
def healthz():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "version": settings.version}
    finally:
        db.close()


app.include_router(auth_routes.router)
app.include_router(dashboard_routes.router)
app.include_router(api_routes.router)
app.include_router(settings_routes.router)
app.include_router(docker_routes.router)
app.include_router(docker_routes.ws_router)
app.include_router(k8s_routes.router)
app.include_router(k8s_routes.ws_router)
app.include_router(jenkins_routes.router)
app.include_router(git_routes.router)
app.include_router(ssh_routes.router)
app.include_router(ssh_routes.ws_router)
app.include_router(pipeline_routes.router)
app.include_router(pipeline_routes.runs_router)
app.include_router(pipeline_routes.webhook_router)
app.include_router(pipeline_routes.hooks_router)
app.include_router(users_routes.router)
app.include_router(ansible_routes.router)
app.include_router(deploy_routes.router)
app.include_router(account_routes.router)
app.include_router(diagnostics_routes.router)
