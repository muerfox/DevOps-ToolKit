import subprocess

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_operator
from ..modules import git_mgr, ssh_mgr

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/git")
def git_index(request: Request, db: Session = Depends(get_db)):
    repos = git_mgr.list_repos(db)
    return templates.TemplateResponse(
        "git/index.html", {"request": request, "repos": repos, "ssh_servers": ssh_mgr.list_servers(db)}
    )


@router.post("/git/repos/create")
def git_repos_create(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    branch: str = Form("main"),
    auth_type: str = Form("https"),
    username: str = Form(""),
    credential: str = Form(""),
    ssh_key: str = Form(""),
    ssh_key_passphrase: str = Form(""),
    target: str = Form("local"),  # local | server
    deploy_server_id: str = Form(""),
    remote_path: str = Form(""),
    db: Session = Depends(get_db),
):
    error = None
    try:
        git_mgr.register_and_clone(
            db,
            name.strip(),
            url.strip(),
            branch.strip() or "main",
            auth_type=auth_type,
            username=username.strip() or None,
            credential=credential or None,
            ssh_key=ssh_key or None,
            ssh_key_passphrase=ssh_key_passphrase or None,
            deploy_server_id=int(deploy_server_id) if target == "server" and deploy_server_id else None,
            remote_path=remote_path.strip() if target == "server" else None,
        )
    except git_mgr.GitError as exc:
        error = str(exc)
    if error:
        repos = git_mgr.list_repos(db)
        return templates.TemplateResponse(
            "git/index.html",
            {"request": request, "repos": repos, "ssh_servers": ssh_mgr.list_servers(db), "error": error},
            status_code=400,
        )
    return RedirectResponse("/git", status_code=303)


@router.post("/git/repos/{repo_id}/delete")
def git_repos_delete(repo_id: int, db: Session = Depends(get_db)):
    git_mgr.delete_repo(db, repo_id)
    return RedirectResponse("/git", status_code=303)


@router.get("/git/repos/{name}")
def git_repo_detail(request: Request, name: str, db: Session = Depends(get_db)):
    record = git_mgr.get_repo_record(db, name)
    error = None
    repo_status, commits, branch_info, diff_text = None, [], None, ""
    try:
        repo_status = git_mgr.status(record)
        commits = git_mgr.log(record)
        branch_info = git_mgr.branches(record)
        diff_text = git_mgr.diff(record)
    except git_mgr.GitError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "git/detail.html",
        {
            "request": request,
            "record": record,
            "status": repo_status,
            "commits": commits,
            "branch_info": branch_info,
            "diff_text": diff_text,
            "scripts": git_mgr.list_scripts(db, record.id),
            "error": error,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/git/repos/{name}/pull")
def git_repo_pull(name: str, db: Session = Depends(get_db)):
    record = git_mgr.get_repo_record(db, name)
    message = None
    try:
        message = git_mgr.pull(record)
    except git_mgr.GitError as exc:
        message = str(exc)
    return RedirectResponse(f"/git/repos/{name}?message={message}", status_code=303)


@router.post("/git/repos/{name}/checkout")
def git_repo_checkout(name: str, branch: str = Form(...), db: Session = Depends(get_db)):
    record = git_mgr.get_repo_record(db, name)
    message = None
    try:
        git_mgr.checkout(record, branch)
        message = f"Checked out {branch}"
    except git_mgr.GitError as exc:
        message = str(exc)
    return RedirectResponse(f"/git/repos/{name}?message={message}", status_code=303)


@router.post("/git/repos/{name}/commit")
def git_repo_commit(name: str, message: str = Form(...), db: Session = Depends(get_db)):
    record = git_mgr.get_repo_record(db, name)
    result = None
    try:
        result = git_mgr.commit_and_push(record, message)
    except git_mgr.GitError as exc:
        result = str(exc)
    return RedirectResponse(f"/git/repos/{name}?message={result}", status_code=303)


# --- custom scripts (build/test/lint/... run locally against the checkout) --

@router.post("/git/repos/{name}/scripts/create")
def git_repo_scripts_create(name: str, script_name: str = Form(...), script_text: str = Form(...), db: Session = Depends(get_db)):
    record = git_mgr.get_repo_record(db, name)
    git_mgr.create_script(db, record.id, script_name, script_text)
    return RedirectResponse(f"/git/repos/{name}", status_code=303)


@router.post("/git/repos/{name}/scripts/{script_id}/delete")
def git_repo_scripts_delete(name: str, script_id: int, db: Session = Depends(get_db)):
    git_mgr.delete_script(db, script_id)
    return RedirectResponse(f"/git/repos/{name}", status_code=303)


@router.post("/git/repos/{name}/scripts/{script_id}/run")
def git_repo_scripts_run(request: Request, name: str, script_id: int, db: Session = Depends(get_db)):
    record = git_mgr.get_repo_record(db, name)
    script = git_mgr.get_script(db, script_id)
    result, error = None, None
    try:
        result = git_mgr.run_script(record, script.script_text)
    except subprocess.TimeoutExpired:
        error = "Script timed out after 300 seconds."
    return templates.TemplateResponse(
        "git/script_result.html",
        {"request": request, "record": record, "script": script, "result": result, "error": error},
    )
