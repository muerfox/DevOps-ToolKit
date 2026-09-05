from fastapi import APIRouter, Depends, Request, Form, BackgroundTasks
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from .. import models
from ..modules import ansible_mgr, ssh_mgr

router = APIRouter(dependencies=[Depends(require_login)])


def _picker_data(db: Session) -> dict:
    return {
        "servers": ssh_mgr.list_servers(db),
        "groups": ansible_mgr.list_groups(db),
    }


def _kick_off(db: Session, background_tasks: BackgroundTasks, label: str, target_type: str, target_value: str, playbook_yaml: str):
    run = ansible_mgr.start_run(db, label, target_type, target_value or None, playbook_yaml)
    background_tasks.add_task(ansible_mgr.execute_run, run.id)
    return run


@router.get("/ansible")
def ansible_index(request: Request, db: Session = Depends(get_db)):
    playbooks = ansible_mgr.list_playbooks(db)
    recent_runs = db.query(models.AnsibleRun).order_by(models.AnsibleRun.started_at.desc()).limit(10).all()
    ctx = {"request": request, "playbooks": playbooks, "recent_runs": recent_runs}
    ctx.update(_picker_data(db))
    return templates.TemplateResponse("ansible/index.html", ctx)


# --- saved playbooks ------------------------------------------------------

@router.post("/ansible/playbooks/create")
def ansible_playbooks_create(
    name: str = Form(...), description: str = Form(""), playbook_yaml: str = Form(...), db: Session = Depends(get_db)
):
    playbook = ansible_mgr.create_playbook(db, name.strip(), description.strip(), playbook_yaml)
    return RedirectResponse(f"/ansible/playbooks/{playbook.id}", status_code=303)


@router.post("/ansible/playbooks/{playbook_id}/delete")
def ansible_playbooks_delete(playbook_id: int, db: Session = Depends(get_db)):
    ansible_mgr.delete_playbook(db, playbook_id)
    return RedirectResponse("/ansible", status_code=303)


@router.get("/ansible/playbooks/{playbook_id}")
def ansible_playbook_detail(request: Request, playbook_id: int, db: Session = Depends(get_db)):
    playbook = ansible_mgr.get_playbook(db, playbook_id)
    runs = (
        db.query(models.AnsibleRun)
        .filter(models.AnsibleRun.label == playbook.name)
        .order_by(models.AnsibleRun.started_at.desc())
        .limit(15)
        .all()
    )
    ctx = {"request": request, "playbook": playbook, "runs": runs, "error": None, "saved": False}
    ctx.update(_picker_data(db))
    return templates.TemplateResponse("ansible/playbook_detail.html", ctx)


@router.post("/ansible/playbooks/{playbook_id}/update")
def ansible_playbook_update(
    request: Request,
    playbook_id: int,
    description: str = Form(""),
    playbook_yaml: str = Form(...),
    db: Session = Depends(get_db),
):
    playbook = ansible_mgr.get_playbook(db, playbook_id)
    ansible_mgr.update_playbook(db, playbook, description.strip(), playbook_yaml)
    runs = (
        db.query(models.AnsibleRun)
        .filter(models.AnsibleRun.label == playbook.name)
        .order_by(models.AnsibleRun.started_at.desc())
        .limit(15)
        .all()
    )
    ctx = {"request": request, "playbook": playbook, "runs": runs, "error": None, "saved": True}
    ctx.update(_picker_data(db))
    return templates.TemplateResponse("ansible/playbook_detail.html", ctx)


@router.post("/ansible/playbooks/{playbook_id}/run")
def ansible_playbook_run(
    playbook_id: int,
    background_tasks: BackgroundTasks,
    target_type: str = Form(...),
    target_value: str = Form(""),
    db: Session = Depends(get_db),
):
    playbook = ansible_mgr.get_playbook(db, playbook_id)
    run = _kick_off(db, background_tasks, playbook.name, target_type, target_value, playbook.playbook_yaml)
    return RedirectResponse(f"/ansible/runs/{run.id}", status_code=303)


# --- ad-hoc (unsaved) playbook ------------------------------------------------

@router.post("/ansible/run-adhoc")
def ansible_run_adhoc(
    background_tasks: BackgroundTasks,
    playbook_yaml: str = Form(...),
    target_type: str = Form(...),
    target_value: str = Form(""),
    db: Session = Depends(get_db),
):
    run = _kick_off(db, background_tasks, "(ad-hoc playbook)", target_type, target_value, playbook_yaml)
    return RedirectResponse(f"/ansible/runs/{run.id}", status_code=303)


# --- quick actions ------------------------------------------------------

@router.post("/ansible/quick/manage-user")
def ansible_quick_manage_user(
    background_tasks: BackgroundTasks,
    target_type: str = Form(...),
    target_value: str = Form(""),
    username: str = Form(...),
    state: str = Form("present"),
    password: str = Form(""),
    sudo: str = Form(""),
    ssh_pubkey: str = Form(""),
    db: Session = Depends(get_db),
):
    playbook_yaml = ansible_mgr.playbook_manage_user(
        username.strip(), state=state, password=password or None, sudo=bool(sudo), ssh_pubkey=ssh_pubkey.strip() or None
    )
    run = _kick_off(db, background_tasks, f"Manage user: {username.strip()}", target_type, target_value, playbook_yaml)
    return RedirectResponse(f"/ansible/runs/{run.id}", status_code=303)


@router.post("/ansible/quick/install-updates")
def ansible_quick_install_updates(
    background_tasks: BackgroundTasks,
    target_type: str = Form(...),
    target_value: str = Form(""),
    db: Session = Depends(get_db),
):
    playbook_yaml = ansible_mgr.playbook_install_updates()
    run = _kick_off(db, background_tasks, "Install OS updates", target_type, target_value, playbook_yaml)
    return RedirectResponse(f"/ansible/runs/{run.id}", status_code=303)


@router.post("/ansible/quick/upload-file")
def ansible_quick_upload_file(
    background_tasks: BackgroundTasks,
    target_type: str = Form(...),
    target_value: str = Form(""),
    dest_path: str = Form(...),
    content: str = Form(...),
    mode: str = Form(""),
    owner: str = Form(""),
    db: Session = Depends(get_db),
):
    playbook_yaml = ansible_mgr.playbook_upload_file(dest_path.strip(), content, mode.strip() or None, owner.strip() or None)
    run = _kick_off(db, background_tasks, f"Upload file: {dest_path.strip()}", target_type, target_value, playbook_yaml)
    return RedirectResponse(f"/ansible/runs/{run.id}", status_code=303)


# --- run history ------------------------------------------------------

@router.get("/ansible/runs/{run_id}")
def ansible_run_detail(request: Request, run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.AnsibleRun, run_id)
    return templates.TemplateResponse("ansible/run.html", {"request": request, "run": run})


@router.get("/ansible/runs/{run_id}/status")
def ansible_run_status(run_id: int, db: Session = Depends(get_db)):
    run = db.get(models.AnsibleRun, run_id)
    if not run:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"status": run.status, "log_text": run.log_text}
