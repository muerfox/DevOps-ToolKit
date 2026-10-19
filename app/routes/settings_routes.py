import datetime as dt
import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from ..flash import flash
from .. import models
from ..modules import config_io, system_mgr

router = APIRouter(dependencies=[Depends(require_login)])

MAX_IMPORT_BYTES = 20 * 1024 * 1024


def _render(request: Request, db: Session, report: dict | None = None):
    counts = {
        "Docker hosts": db.query(models.DockerHost).count(),
        "Kubernetes clusters": db.query(models.K8sCluster).count(),
        "Jenkins instances": db.query(models.JenkinsInstance).count(),
        "Git repos": db.query(models.GitRepo).count(),
        "SSH servers": db.query(models.SSHServer).count(),
        "SSH scripts": db.query(models.SSHScript).count(),
        "Pipelines": db.query(models.Pipeline).count(),
        "Pipeline runs": db.query(models.PipelineRun).count(),
        "Users": db.query(models.User).count(),
    }
    return templates.TemplateResponse(
        "settings.html", {"request": request, "about": system_mgr.about(), "counts": counts, "report": report}
    )


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    return _render(request, db)


@router.post("/settings/export")
def settings_export(
    request: Request,
    passphrase: str = Form(""),
    passphrase_confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    if passphrase and passphrase != passphrase_confirm:
        flash(request, "Passphrases do not match.", "error")
        return RedirectResponse("/settings", status_code=303)
    if passphrase and len(passphrase) < 8:
        flash(request, "Use a passphrase of at least 8 characters.", "error")
        return RedirectResponse("/settings", status_code=303)
    doc = config_io.export_config(db, passphrase or None)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "-with-credentials" if passphrase else ""
    return Response(
        content=json.dumps(doc, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="cockpit-config-{stamp}{suffix}.json"'},
    )


@router.post("/settings/import")
async def settings_import(
    request: Request,
    file: UploadFile = File(...),
    passphrase: str = Form(""),
    overwrite: bool = Form(False),
    db: Session = Depends(get_db),
):
    raw = await file.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        flash(request, "File too large.", "error")
        return RedirectResponse("/settings", status_code=303)
    try:
        doc = json.loads(raw)
        report = config_io.import_config(db, doc, passphrase or None, overwrite=overwrite)
    except (ValueError, KeyError, TypeError) as exc:
        db.rollback()
        flash(request, f"Import failed: invalid file ({exc})", "error")
        return RedirectResponse("/settings", status_code=303)
    except config_io.ConfigError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/settings", status_code=303)

    flash(request, report.summary(), "success")
    # Rendered directly (not redirected): the full report can be too big for the session cookie.
    return _render(request, db, report=report.as_dict())
