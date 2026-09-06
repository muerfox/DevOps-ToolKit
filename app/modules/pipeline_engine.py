"""CI/CD pipeline engine.

A Pipeline is a named, ordered list of steps (stored as JSON on the model).
Each step has a `type` (see STEP_TYPES) and a `params` dict. Running a
pipeline creates a PipelineRun row and executes steps sequentially in a
worker thread (kicked off via FastAPI BackgroundTasks so the HTTP request
returns immediately); each step's output is appended to the run's log and
committed as it happens so the run-detail page can poll for live output.

Steps are plain glue over the other modules (git_mgr, docker_mgr, k8s_mgr,
jenkins_mgr, ssh_mgr) -- this file has no infra logic of its own beyond
wiring parameters through and formatting log lines.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from ..config import REPOS_DIR, BASE_DIR
from ..database import SessionLocal
from .. import models
from . import docker_mgr, k8s_mgr, jenkins_mgr, git_mgr, ssh_mgr

LogFn = Callable[[str], None]


class StepError(Exception):
    pass


# --- step executors ------------------------------------------------------------

def _step_git_pull(db: Session, params: dict, log: LogFn) -> None:
    record = git_mgr.get_repo_record(db, params["repo"])
    log(f"$ git pull  ({record.name})\n")
    try:
        log(git_mgr.pull(record) + "\n")
    except git_mgr.GitError as exc:
        raise StepError(str(exc)) from exc


def _build_context_path(params: dict) -> str:
    if params.get("repo"):
        path = REPOS_DIR / params["repo"]
        sub = params.get("context_subdir", "").strip("/")
        if sub:
            path = path / sub
        return str(path)
    if params.get("context_path"):
        return params["context_path"]
    raise StepError("docker_build requires either 'repo' or 'context_path'")


def _step_docker_build(db: Session, params: dict, log: LogFn) -> None:
    client = docker_mgr.get_client(db, params.get("host"))
    context = _build_context_path(params)
    tag = params["tag"]
    dockerfile = params.get("dockerfile") or "Dockerfile"
    log(f"$ docker build -t {tag} -f {dockerfile} {context}\n")
    try:
        for chunk in client.api.build(path=context, tag=tag, dockerfile=dockerfile, rm=True, decode=True):
            if "stream" in chunk:
                log(chunk["stream"])
            elif "error" in chunk:
                raise StepError(chunk["error"])
    finally:
        client.close()


def _step_docker_push(db: Session, params: dict, log: LogFn) -> None:
    client = docker_mgr.get_client(db, params.get("host"))
    tag = params["tag"]
    log(f"$ docker push {tag}\n")
    try:
        for line in client.api.push(tag, stream=True, decode=True):
            status = line.get("status", "")
            progress = line.get("progress", "")
            if status:
                log(f"{status} {progress}\n")
            if "error" in line:
                raise StepError(line["error"])
    finally:
        client.close()


def _resolve_compose_text(params: dict, step_label: str) -> str:
    """Shared by stack_deploy and compose_up: the compose file either comes
    from a repo + path, or is pasted directly. Raises a clear StepError
    instead of a bare KeyError when neither was actually filled in."""
    if params.get("repo") and params.get("compose_path"):
        return (REPOS_DIR / params["repo"] / params["compose_path"]).read_text()
    if params.get("compose_text"):
        return params["compose_text"]
    raise StepError(
        f"{step_label} needs a compose file: either pick a git repo and set 'Compose file path', "
        f"or paste the compose YAML directly into 'Compose text'."
    )


def _step_stack_deploy(db: Session, params: dict, log: LogFn) -> None:
    base_url = docker_mgr.resolve_base_url(db, params.get("host"))
    compose_text = _resolve_compose_text(params, "Swarm stack deploy")
    if not params.get("stack_name"):
        raise StepError("Swarm stack deploy needs a stack name.")
    log(f"$ docker stack deploy -c ... {params['stack_name']}\n")
    try:
        log(docker_mgr.stack_deploy(base_url, params["stack_name"], compose_text) + "\n")
    except docker_mgr.DockerError as exc:
        raise StepError(str(exc)) from exc


def _step_compose_up(db: Session, params: dict, log: LogFn) -> None:
    base_url = docker_mgr.resolve_base_url(db, params.get("host"))
    compose_text = _resolve_compose_text(params, "Docker Compose up")
    project_name = params.get("project_name") or "app"
    log(f"$ docker compose -p {project_name} up -d --build\n")
    try:
        log(docker_mgr.compose_up(base_url, project_name, compose_text) + "\n")
    except docker_mgr.DockerError as exc:
        raise StepError(str(exc)) from exc


def _step_k8s_apply(db: Session, params: dict, log: LogFn) -> None:
    cluster = k8s_mgr.get_cluster(db, params["cluster"])
    if params.get("repo") and params.get("manifest_path"):
        manifest_text = (REPOS_DIR / params["repo"] / params["manifest_path"]).read_text()
    else:
        manifest_text = params["manifest_text"]
    namespace = params.get("namespace") or "default"
    log(f"$ kubectl apply -f ... -n {namespace}\n")
    try:
        log(k8s_mgr.apply_manifest(cluster, namespace, manifest_text) + "\n")
    except k8s_mgr.K8sError as exc:
        raise StepError(str(exc)) from exc


def _step_k8s_rollout_restart(db: Session, params: dict, log: LogFn) -> None:
    _, apps_v1, _ = k8s_mgr.get_apis(db, params["cluster"])
    namespace = params.get("namespace") or "default"
    log(f"$ kubectl rollout restart deployment/{params['deployment']} -n {namespace}\n")
    try:
        k8s_mgr.rollout_restart_deployment(apps_v1, namespace, params["deployment"])
        log("rollout restarted\n")
    except k8s_mgr.K8sError as exc:
        raise StepError(str(exc)) from exc


def _step_jenkins_trigger(db: Session, params: dict, log: LogFn) -> None:
    server = jenkins_mgr.get_client(db, params["instance"])
    job_name = params["job_name"]
    build_params = {}
    for line in (params.get("params_text") or "").splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            build_params[k.strip()] = v.strip()
    log(f"$ jenkins build {job_name} {build_params or ''}\n")
    try:
        queue_id = jenkins_mgr.trigger_build(server, job_name, build_params or None)
        number = jenkins_mgr.wait_for_build_number(server, queue_id, timeout=30)
        if not number:
            log("Build queued but not started within 30s; not waiting further.\n")
            return
        log(f"Build #{number} started, waiting for completion...\n")
        if params.get("wait", True):
            import time

            for _ in range(600):  # up to ~10 minutes
                info = jenkins_mgr.build_info(server, job_name, number)
                if not info["building"]:
                    log(f"Build #{number} finished: {info['result']}\n")
                    if info["result"] not in ("SUCCESS", None):
                        raise StepError(f"Jenkins build {job_name} #{number} result: {info['result']}")
                    return
                time.sleep(1)
            log("Timed out waiting for build to finish.\n")
    except jenkins_mgr.JenkinsError as exc:
        raise StepError(str(exc)) from exc


def _step_ssh_exec(db: Session, params: dict, log: LogFn) -> None:
    record = ssh_mgr.get_server(db, params["server"])
    log(f"$ ssh {record.username}@{record.host} '{params['command']}'\n")
    try:
        result = ssh_mgr.run_command(record, params["command"], timeout=int(params.get("timeout", 60)))
    except ssh_mgr.SSHError as exc:
        raise StepError(str(exc)) from exc
    log(result["stdout"])
    if result["stderr"]:
        log(result["stderr"])
    if result["exit_code"] != 0:
        raise StepError(f"Remote command exited {result['exit_code']}")


def _step_ssh_run_script(db: Session, params: dict, log: LogFn) -> None:
    script = ssh_mgr.get_script(db, int(params["script_id"]))
    record = script.server
    log(f"$ ssh {record.username}@{record.host} run saved script '{script.name}'\n")
    try:
        result = ssh_mgr.run_script(record, script.script_text, timeout=int(params.get("timeout", 300)))
    except ssh_mgr.SSHError as exc:
        raise StepError(str(exc)) from exc
    log(result["stdout"])
    if result["stderr"]:
        log(result["stderr"])
    if result["exit_code"] != 0:
        raise StepError(f"Script exited {result['exit_code']}")


def _step_repo_run_script(db: Session, params: dict, log: LogFn) -> None:
    script = git_mgr.get_script(db, int(params["repo_script_id"]))
    record = script.repo
    log(f"$ ({record.name}) run saved script '{script.name}'\n")
    try:
        result = git_mgr.run_script(record, script.script_text, timeout=int(params.get("timeout", 300)))
    except git_mgr.GitError as exc:
        raise StepError(str(exc)) from exc
    log(result["stdout"])
    if result["stderr"]:
        log(result["stderr"])
    if result["exit_code"] != 0:
        raise StepError(f"Script exited {result['exit_code']}")


def _step_shell(db: Session, params: dict, log: LogFn) -> None:
    cwd = params.get("cwd") or str(BASE_DIR)
    log(f"$ {params['command']}  (cwd={cwd})\n")
    proc = subprocess.run(
        params["command"], shell=True, cwd=cwd, capture_output=True, text=True, timeout=int(params.get("timeout", 300))
    )
    log(proc.stdout)
    if proc.stderr:
        log(proc.stderr)
    if proc.returncode != 0:
        raise StepError(f"Command exited {proc.returncode}")


STEP_TYPES: dict[str, dict] = {
    "git_pull": {"label": "Git pull", "fields": ["repo"], "run": _step_git_pull},
    "docker_build": {
        "label": "Docker build",
        "fields": ["host", "repo", "context_subdir", "context_path", "dockerfile", "tag"],
        "run": _step_docker_build,
    },
    "docker_push": {"label": "Docker push", "fields": ["host", "tag"], "run": _step_docker_push},
    "stack_deploy": {
        "label": "Swarm stack deploy",
        "fields": ["host", "stack_name", "repo", "compose_path", "compose_text"],
        "run": _step_stack_deploy,
    },
    "compose_up": {
        "label": "Docker Compose up (-d --build)",
        "fields": ["host", "project_name", "repo", "compose_path", "compose_text"],
        "run": _step_compose_up,
    },
    "k8s_apply": {
        "label": "Kubernetes apply",
        "fields": ["cluster", "namespace", "repo", "manifest_path", "manifest_text"],
        "run": _step_k8s_apply,
    },
    "k8s_rollout_restart": {
        "label": "Kubernetes rollout restart",
        "fields": ["cluster", "namespace", "deployment"],
        "run": _step_k8s_rollout_restart,
    },
    "jenkins_trigger": {
        "label": "Jenkins trigger build",
        "fields": ["instance", "job_name", "params_text"],
        "run": _step_jenkins_trigger,
    },
    "ssh_exec": {"label": "SSH run command", "fields": ["server", "command", "timeout"], "run": _step_ssh_exec},
    "ssh_run_script": {
        "label": "SSH run saved script",
        "fields": ["script_id", "timeout"],
        "run": _step_ssh_run_script,
    },
    "repo_run_script": {
        "label": "Repo run saved script (build/test/...)",
        "fields": ["repo_script_id", "timeout"],
        "run": _step_repo_run_script,
    },
    "shell": {"label": "Local shell command", "fields": ["command", "cwd", "timeout"], "run": _step_shell},
}


# --- pipeline CRUD ------------------------------------------------------------

def list_pipelines(db: Session) -> list[models.Pipeline]:
    return db.query(models.Pipeline).order_by(models.Pipeline.name).all()


def get_pipeline(db: Session, pipeline_id: int) -> models.Pipeline:
    pipeline = db.get(models.Pipeline, pipeline_id)
    if not pipeline:
        raise StepError(f"Unknown pipeline id {pipeline_id}")
    return pipeline


def create_pipeline(db: Session, name: str, description: str = "") -> models.Pipeline:
    pipeline = models.Pipeline(name=name, description=description, steps_json="[]")
    db.add(pipeline)
    db.commit()
    db.refresh(pipeline)
    return pipeline


def delete_pipeline(db: Session, pipeline_id: int) -> None:
    pipeline = db.get(models.Pipeline, pipeline_id)
    if pipeline:
        db.delete(pipeline)
        db.commit()


def get_steps(pipeline: models.Pipeline) -> list[dict]:
    return json.loads(pipeline.steps_json or "[]")


def set_steps(db: Session, pipeline: models.Pipeline, steps: list[dict]) -> None:
    pipeline.steps_json = json.dumps(steps)
    db.commit()


def add_step(db: Session, pipeline: models.Pipeline, step_type: str, params: dict) -> None:
    if step_type not in STEP_TYPES:
        raise StepError(f"Unknown step type '{step_type}'")
    steps = get_steps(pipeline)
    steps.append({"type": step_type, "params": params})
    set_steps(db, pipeline, steps)


def remove_step(db: Session, pipeline: models.Pipeline, index: int) -> None:
    steps = get_steps(pipeline)
    if 0 <= index < len(steps):
        steps.pop(index)
        set_steps(db, pipeline, steps)


def move_step(db: Session, pipeline: models.Pipeline, index: int, direction: int) -> None:
    steps = get_steps(pipeline)
    new_index = index + direction
    if 0 <= index < len(steps) and 0 <= new_index < len(steps):
        steps[index], steps[new_index] = steps[new_index], steps[index]
        set_steps(db, pipeline, steps)


# --- execution ------------------------------------------------------------

def run_pipeline_sync(pipeline_id: int, run_id: int) -> None:
    """Executed in a worker thread via BackgroundTasks. Owns its own DB session."""
    db = SessionLocal()
    try:
        run = db.get(models.PipelineRun, run_id)
        pipeline = db.get(models.Pipeline, pipeline_id)
        if not run or not pipeline:
            return

        buffer: list[str] = []

        def log(text: str) -> None:
            if not text:
                return
            buffer.append(text)
            run.log_text = "".join(buffer)
            db.commit()

        steps = get_steps(pipeline)
        log(f"=== Starting pipeline '{pipeline.name}' ({len(steps)} step(s)) ===\n\n")
        try:
            for i, step in enumerate(steps, start=1):
                step_def = STEP_TYPES.get(step["type"])
                if not step_def:
                    raise StepError(f"Unknown step type '{step['type']}'")
                log(f"--- Step {i}/{len(steps)}: {step_def['label']} ---\n")
                step_def["run"](db, step.get("params", {}), log)
                log("\n")
            run.status = "success"
            log("=== Pipeline succeeded ===\n")
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            log(f"\n=== Pipeline FAILED: {exc} ===\n")
        finally:
            import datetime as dt

            run.finished_at = dt.datetime.utcnow()
            db.commit()
    finally:
        db.close()


def start_run(db: Session, pipeline: models.Pipeline) -> models.PipelineRun:
    run = models.PipelineRun(pipeline_id=pipeline.id, status="running", log_text="")
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
