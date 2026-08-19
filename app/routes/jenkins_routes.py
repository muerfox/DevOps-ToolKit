from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from .. import models
from ..modules import jenkins_mgr

router = APIRouter(dependencies=[Depends(require_login)])


def _parse_params(text: str) -> dict:
    params = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        params[key.strip()] = value.strip()
    return params


@router.get("/jenkins")
def jenkins_index(db: Session = Depends(get_db)):
    instances = jenkins_mgr.list_instances(db)
    if not instances:
        return RedirectResponse("/jenkins/instances", status_code=303)
    return RedirectResponse(f"/jenkins/jobs?instance={instances[0].name}", status_code=303)


@router.get("/jenkins/instances")
def jenkins_instances(request: Request, db: Session = Depends(get_db)):
    instances = jenkins_mgr.list_instances(db)
    pings = {i.name: jenkins_mgr.ping(db, i.name) for i in instances}
    return templates.TemplateResponse(
        "jenkins/instances.html", {"request": request, "instances": instances, "pings": pings}
    )


@router.post("/jenkins/instances/create")
def jenkins_instances_create(
    name: str = Form(...),
    url: str = Form(...),
    username: str = Form(...),
    token: str = Form(...),
    db: Session = Depends(get_db),
):
    jenkins_mgr.create_instance(db, name.strip(), url.strip(), username.strip(), token)
    return RedirectResponse("/jenkins/instances", status_code=303)


@router.post("/jenkins/instances/{instance_id}/delete")
def jenkins_instances_delete(instance_id: int, db: Session = Depends(get_db)):
    jenkins_mgr.delete_instance(db, instance_id)
    return RedirectResponse("/jenkins/instances", status_code=303)


@router.get("/jenkins/jobs")
def jenkins_jobs(request: Request, instance: str, db: Session = Depends(get_db)):
    jobs, error = [], None
    try:
        server = jenkins_mgr.get_client(db, instance)
        jobs = jenkins_mgr.list_jobs(server)
    except jenkins_mgr.JenkinsError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "jenkins/jobs.html",
        {
            "request": request,
            "instances": jenkins_mgr.list_instances(db),
            "instance": instance,
            "jobs": jobs,
            "error": error,
        },
    )


@router.get("/jenkins/jobs/{job_name:path}")
def jenkins_job_detail(request: Request, job_name: str, instance: str, db: Session = Depends(get_db)):
    info, error = None, None
    try:
        server = jenkins_mgr.get_client(db, instance)
        info = jenkins_mgr.job_info(server, job_name)
    except jenkins_mgr.JenkinsError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "jenkins/job_detail.html",
        {
            "request": request,
            "instances": jenkins_mgr.list_instances(db),
            "instance": instance,
            "job_name": job_name,
            "info": info,
            "error": error,
            "queued": request.query_params.get("queued"),
        },
    )


@router.post("/jenkins/jobs/{job_name:path}/trigger")
def jenkins_job_trigger(job_name: str, instance: str, params_text: str = Form(""), db: Session = Depends(get_db)):
    try:
        server = jenkins_mgr.get_client(db, instance)
        params = _parse_params(params_text)
        queue_id = jenkins_mgr.trigger_build(server, job_name, params or None)
        number = jenkins_mgr.wait_for_build_number(server, queue_id, timeout=6)
        if number:
            return RedirectResponse(
                f"/jenkins/jobs/{job_name}/builds/{number}/console?instance={instance}", status_code=303
            )
        return RedirectResponse(f"/jenkins/jobs/{job_name}?instance={instance}&queued=1", status_code=303)
    except jenkins_mgr.JenkinsError:
        return RedirectResponse(f"/jenkins/jobs/{job_name}?instance={instance}", status_code=303)


@router.get("/jenkins/jobs/{job_name:path}/builds/{number}/console")
def jenkins_build_console(request: Request, job_name: str, number: int, instance: str, db: Session = Depends(get_db)):
    output, build, error = "", None, None
    try:
        server = jenkins_mgr.get_client(db, instance)
        output = jenkins_mgr.console_output(server, job_name, number)
        build = jenkins_mgr.build_info(server, job_name, number)
    except jenkins_mgr.JenkinsError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "jenkins/console.html",
        {
            "request": request,
            "instance": instance,
            "job_name": job_name,
            "number": number,
            "output": output,
            "build": build,
            "error": error,
        },
    )
