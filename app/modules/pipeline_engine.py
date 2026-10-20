"""CI/CD pipeline engine.

A Pipeline is a named, ordered list of steps (stored as JSON on the model).
Each step has a `type` (see STEP_TYPES) and a `params` dict. Running a
pipeline creates a PipelineRun row and executes steps sequentially in a
daemon worker thread; each step's output is appended to the run's log and
committed (throttled) as it happens so the run-detail page can poll for live
output.

Runs of the same pipeline are serialised: a second run waits as `queued`
until the first finishes, so two webhook pushes in a row don't fight over
the same checkout.

Variables: a pipeline has KEY=value variables (`env_text`), a run can add
overrides (manual "run with variables", webhook payloads), and built-ins
like COCKPIT_RUN_ID are always present. `${NAME}` in any step parameter is
substituted before the step runs. Local shell steps and remote SSH / repo
script steps also receive them as environment variables (so e.g.
`docker compose` on a server sees `MAIL_DOMAIN=...`). Any shell/SSH step
can export a new value for later steps by printing `::set NAME=value`.

Steps are plain glue over the other modules (git_mgr, docker_mgr, k8s_mgr,
jenkins_mgr, ssh_mgr) -- this file has no infra logic of its own beyond
wiring parameters through and formatting log lines.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import logging
import os
import re
import secrets
import shlex
import signal
import subprocess
import threading
import time

from croniter import croniter
from sqlalchemy.orm import Session

from ..config import REPOS_DIR, BASE_DIR, settings
from ..database import SessionLocal
from .. import models
from . import docker_mgr, k8s_mgr, jenkins_mgr, git_mgr, ssh_mgr

log = logging.getLogger("cockpit.pipelines")

ACTIVE_STATUSES = ("queued", "running")

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SET_RE = re.compile(r"^::set\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class StepError(Exception):
    pass


class RunCancelled(Exception):
    pass


# --- run context ------------------------------------------------------------

class RunContext:
    """Passed to every step: logging, cancellation and variables for one run."""

    FLUSH_INTERVAL = 0.5

    def __init__(self, db: Session, run: models.PipelineRun, cancel_event: threading.Event, variables: dict):
        self.db = db
        self.run = run
        self.cancel_event = cancel_event
        self.vars: dict[str, str] = variables
        self._buffer: list[str] = [run.log_text or ""]
        self._last_flush = 0.0

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise RunCancelled()

    def write(self, text: str) -> None:
        """Append to the log without a cancellation check (used for framing lines)."""
        if not text:
            return
        self._buffer.append(text)
        if time.monotonic() - self._last_flush >= self.FLUSH_INTERVAL:
            self.flush()

    def log(self, text: str) -> None:
        self.check()
        self.write(text)

    def output(self, text: str, check: bool = True) -> None:
        """Log command output, picking up any `::set NAME=value` lines."""
        if not text:
            return
        for line in text.splitlines(keepends=True):
            m = _SET_RE.match(line.strip())
            if m:
                self.vars[m.group(1)] = m.group(2).strip()
                self.write(f"  [set ${{{m.group(1)}}}]\n")
            else:
                self.write(line)
        if check:
            self.check()

    def flush(self) -> None:
        self.run.log_text = "".join(self._buffer)
        self.db.commit()
        self._last_flush = time.monotonic()


def expand_vars(value, variables: dict):
    if isinstance(value, str):
        return _VAR_RE.sub(lambda m: variables.get(m.group(1), m.group(0)), value)
    return value


def parse_kv_text(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


# --- step executors ------------------------------------------------------------

def _step_git_pull(db: Session, params: dict, ctx: RunContext) -> None:
    record = git_mgr.get_repo_record(db, params["repo"])
    branch = (params.get("branch") or "").strip() or None
    if branch and "${" in branch:
        # e.g. ${GIT_BRANCH} on a manual run, where no webhook supplied it.
        ctx.write(f"  [branch '{branch}' is not set for this run; using the clone's current branch]\n")
        branch = None
    ctx.log(f"$ git pull  ({record.name}{' @ ' + branch if branch else ''})\n")
    try:
        ctx.output(git_mgr.pull(record, branch) + "\n")
    except git_mgr.GitError as exc:
        raise StepError(str(exc)) from exc
    try:
        repo = git_mgr.open_repo(record)
        sha = repo.head.commit.hexsha
        ctx.vars["GIT_COMMIT"] = sha
        ctx.vars["GIT_SHORT_SHA"] = sha[:8]
        ctx.write(f"  [set ${{GIT_COMMIT}} = {sha[:8]}]\n")
    except Exception:  # noqa: BLE001 - informational only
        pass


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


def _step_docker_build(db: Session, params: dict, ctx: RunContext) -> None:
    client = docker_mgr.get_client(db, params.get("host"))
    context = _build_context_path(params)
    tag = params["tag"]
    dockerfile = params.get("dockerfile") or "Dockerfile"
    ctx.log(f"$ docker build -t {tag} -f {dockerfile} {context}\n")
    try:
        for chunk in client.api.build(path=context, tag=tag, dockerfile=dockerfile, rm=True, decode=True):
            if "stream" in chunk:
                ctx.log(chunk["stream"])
            elif "error" in chunk:
                raise StepError(chunk["error"])
    finally:
        client.close()


def _step_docker_push(db: Session, params: dict, ctx: RunContext) -> None:
    client = docker_mgr.get_client(db, params.get("host"))
    tag = params["tag"]
    ctx.log(f"$ docker push {tag}\n")
    try:
        for line in client.api.push(tag, stream=True, decode=True):
            status = line.get("status", "")
            progress = line.get("progress", "")
            if status:
                ctx.log(f"{status} {progress}\n")
            if "error" in line:
                raise StepError(line["error"])
    finally:
        client.close()


def _resolve_compose_text(params: dict, step_label: str, variables: dict) -> str:
    """Shared by stack_deploy and compose_up: the compose file either comes
    from a repo + path, or is pasted directly."""
    if params.get("repo") and params.get("compose_path"):
        text = (REPOS_DIR / params["repo"] / params["compose_path"]).read_text()
    elif params.get("compose_text"):
        text = params["compose_text"]
    else:
        raise StepError(
            f"{step_label} needs a compose file: either pick a git repo and set 'Compose file path', "
            f"or paste the compose YAML directly into 'Compose text'."
        )
    return expand_vars(text, variables)


def _step_stack_deploy(db: Session, params: dict, ctx: RunContext) -> None:
    base_url = docker_mgr.resolve_base_url(db, params.get("host"))
    compose_text = _resolve_compose_text(params, "Swarm stack deploy", ctx.vars)
    if not params.get("stack_name"):
        raise StepError("Swarm stack deploy needs a stack name.")
    ctx.log(f"$ docker stack deploy -c ... {params['stack_name']}\n")
    try:
        ctx.output(docker_mgr.stack_deploy(base_url, params["stack_name"], compose_text) + "\n")
    except docker_mgr.DockerError as exc:
        raise StepError(str(exc)) from exc


def _step_compose_up(db: Session, params: dict, ctx: RunContext) -> None:
    base_url = docker_mgr.resolve_base_url(db, params.get("host"))
    compose_text = _resolve_compose_text(params, "Docker Compose up", ctx.vars)
    project_name = expand_vars(params.get("project_name") or "app", ctx.vars)
    ctx.log(f"$ docker compose -p {project_name} up -d --build\n")
    try:
        ctx.output(docker_mgr.compose_up(base_url, project_name, compose_text) + "\n")
    except docker_mgr.DockerError as exc:
        raise StepError(str(exc)) from exc



def _step_k8s_apply(db: Session, params: dict, ctx: RunContext) -> None:
    cluster = k8s_mgr.get_cluster(db, params["cluster"])
    if params.get("repo") and params.get("manifest_path"):
        manifest_text = (REPOS_DIR / params["repo"] / params["manifest_path"]).read_text()
    else:
        manifest_text = params["manifest_text"]
    manifest_text = expand_vars(manifest_text, ctx.vars)
    namespace = params.get("namespace") or "default"
    ctx.log(f"$ kubectl apply -f ... -n {namespace}\n")
    try:
        ctx.output(k8s_mgr.apply_manifest(cluster, namespace, manifest_text) + "\n")
    except k8s_mgr.K8sError as exc:
        raise StepError(str(exc)) from exc


def _step_k8s_rollout_restart(db: Session, params: dict, ctx: RunContext) -> None:
    _, apps_v1, _ = k8s_mgr.get_apis(db, params["cluster"])
    namespace = params.get("namespace") or "default"
    ctx.log(f"$ kubectl rollout restart deployment/{params['deployment']} -n {namespace}\n")
    try:
        k8s_mgr.rollout_restart_deployment(apps_v1, namespace, params["deployment"])
        ctx.log("rollout restarted\n")
    except k8s_mgr.K8sError as exc:
        raise StepError(str(exc)) from exc


def _step_jenkins_trigger(db: Session, params: dict, ctx: RunContext) -> None:
    server = jenkins_mgr.get_client(db, params["instance"])
    job_name = params["job_name"]
    build_params = {k: expand_vars(v, ctx.vars) for k, v in parse_kv_text(params.get("params_text")).items()}
    ctx.log(f"$ jenkins build {job_name} {build_params or ''}\n")
    try:
        queue_id = jenkins_mgr.trigger_build(server, job_name, build_params or None)
        number = jenkins_mgr.wait_for_build_number(server, queue_id, timeout=30)
        if not number:
            ctx.log("Build queued but not started within 30s; not waiting further.\n")
            return
        ctx.vars["JENKINS_BUILD_NUMBER"] = str(number)
        ctx.log(f"Build #{number} started, waiting for completion...\n")
        for _ in range(600):  # up to ~10 minutes
            info = jenkins_mgr.build_info(server, job_name, number)
            if not info["building"]:
                ctx.log(f"Build #{number} finished: {info['result']}\n")
                if info["result"] not in ("SUCCESS", None):
                    raise StepError(f"Jenkins build {job_name} #{number} result: {info['result']}")
                return
            if ctx.cancel_event.wait(1):
                raise RunCancelled()
        ctx.log("Timed out waiting for build to finish.\n")
    except jenkins_mgr.JenkinsError as exc:
        raise StepError(str(exc)) from exc


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _with_exported_env(command: str, variables: dict) -> str:
    """Prefix a remote shell command with `export KEY=value` for each pipeline
    variable so tools like `docker compose` see them. Values are shell-quoted;
    the caller's log line should still show only `command` (not the secrets)."""
    exports = [
        f"export {key}={shlex.quote(str(value))}"
        for key, value in variables.items()
        if _ENV_KEY_RE.match(key) and value is not None
    ]
    if not exports:
        return command
    return "; ".join(exports) + "; " + command


def _step_ssh_exec(db: Session, params: dict, ctx: RunContext) -> None:
    record = ssh_mgr.get_server(db, params["server"])
    ctx.log(f"$ ssh {record.username}@{record.host} '{params['command']}'\n")
    if ctx.vars:
        ctx.log(f"  (exporting {len(ctx.vars)} pipeline variable(s) into the remote shell)\n")
    try:
        remote_cmd = _with_exported_env(params["command"], ctx.vars)
        result = ssh_mgr.run_command(record, remote_cmd, timeout=int(params.get("timeout") or 60))
    except ssh_mgr.SSHError as exc:
        raise StepError(str(exc)) from exc
    ctx.output(result["stdout"])
    ctx.output(result["stderr"])
    if result["exit_code"] != 0:
        raise StepError(f"Remote command exited {result['exit_code']}")


def _step_ssh_run_script(db: Session, params: dict, ctx: RunContext) -> None:
    script = ssh_mgr.get_script(db, int(params["script_id"]))
    record = script.server
    ctx.log(f"$ ssh {record.username}@{record.host} run saved script '{script.name}'\n")
    if ctx.vars:
        ctx.log(f"  (exporting {len(ctx.vars)} pipeline variable(s) into the remote shell)\n")
    try:
        script_text = expand_vars(script.script_text, ctx.vars)
        remote_cmd = _with_exported_env(script_text, ctx.vars)
        result = ssh_mgr.run_script(record, remote_cmd, timeout=int(params.get("timeout") or 300))
    except ssh_mgr.SSHError as exc:
        raise StepError(str(exc)) from exc
    ctx.output(result["stdout"])
    ctx.output(result["stderr"])
    if result["exit_code"] != 0:
        raise StepError(f"Script exited {result['exit_code']}")


def _step_repo_run_script(db: Session, params: dict, ctx: RunContext) -> None:
    script = git_mgr.get_script(db, int(params["repo_script_id"]))
    record = script.repo
    ctx.log(f"$ ({record.name}) run saved script '{script.name}'\n")
    if ctx.vars:
        ctx.log(f"  (exporting {len(ctx.vars)} pipeline variable(s) into the script env)\n")
    try:
        script_text = expand_vars(script.script_text, ctx.vars)
        # Remote (server-deployed) repos run over SSH — same export prefix.
        # Local repos get env via git_mgr when we pass variables through.
        if git_mgr.is_remote(record):
            script_text = _with_exported_env(script_text, ctx.vars)
            result = git_mgr.run_script(record, script_text, timeout=int(params.get("timeout") or 300))
        else:
            result = git_mgr.run_script(
                record, script_text, timeout=int(params.get("timeout") or 300), env=ctx.vars
            )
    except git_mgr.GitError as exc:
        raise StepError(str(exc)) from exc
    ctx.output(result["stdout"])
    if result.get("stderr"):
        ctx.output(result["stderr"])
    if result["exit_code"] != 0:
        raise StepError(f"Script exited {result['exit_code']}")


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _step_shell(db: Session, params: dict, ctx: RunContext) -> None:
    cwd = params.get("cwd") or str(BASE_DIR)
    timeout = int(params.get("timeout") or 300)
    command = params["command"]
    ctx.log(f"$ {command}  (cwd={cwd})\n")
    env = os.environ.copy()
    env.update(ctx.vars)
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,  # own process group so cancel/timeout kills children too
    )
    stopped: dict[str, str] = {}

    def watchdog():
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if ctx.cancel_event.wait(0.5):
                stopped["reason"] = "cancelled"
                _kill_process_group(proc)
                return
            if time.monotonic() > deadline:
                stopped["reason"] = "timeout"
                _kill_process_group(proc)
                return

    threading.Thread(target=watchdog, daemon=True).start()
    assert proc.stdout is not None
    for line in proc.stdout:
        ctx.output(line, check=False)
    proc.wait()

    if stopped.get("reason") == "cancelled":
        raise RunCancelled()
    if stopped.get("reason") == "timeout":
        raise StepError(f"Command timed out after {timeout}s")
    if proc.returncode != 0:
        raise StepError(f"Command exited {proc.returncode}")


STEP_TYPES: dict[str, dict] = {
    "git_pull": {"label": "Git pull", "fields": ["repo", "branch"], "run": _step_git_pull},
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

# Accepted by every step type in addition to its own fields.
COMMON_FIELDS = ["name", "continue_on_error"]


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


def duplicate_pipeline(db: Session, pipeline: models.Pipeline) -> models.Pipeline:
    base = f"{pipeline.name} (copy)"
    name, n = base, 2
    while db.query(models.Pipeline).filter(models.Pipeline.name == name).first():
        name = f"{base} {n}"
        n += 1
    copy = models.Pipeline(
        name=name,
        description=pipeline.description,
        steps_json=pipeline.steps_json,
        env_text=pipeline.env_text,
        schedule_cron=pipeline.schedule_cron,
        schedule_enabled=False,
        webhook_branch=pipeline.webhook_branch,
    )
    db.add(copy)
    db.commit()
    db.refresh(copy)
    return copy


def delete_pipeline(db: Session, pipeline_id: int) -> None:
    pipeline = db.get(models.Pipeline, pipeline_id)
    if pipeline:
        for run in pipeline.runs:
            cancel_run(db, run.id)
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


def update_step(db: Session, pipeline: models.Pipeline, index: int, step_type: str, params: dict) -> None:
    if step_type not in STEP_TYPES:
        raise StepError(f"Unknown step type '{step_type}'")
    steps = get_steps(pipeline)
    if not 0 <= index < len(steps):
        raise StepError(f"No step #{index + 1}")
    steps[index] = {"type": step_type, "params": params}
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


# --- schedule / webhook settings ---------------------------------------------

def validate_cron(expr: str) -> bool:
    return bool(expr) and croniter.is_valid(expr)


def next_fire_time(expr: str, after_utc: dt.datetime) -> dt.datetime:
    """Next cron fire time strictly after `after_utc` (naive UTC). Cron is evaluated in local time (TZ)."""
    base_local = after_utc.replace(tzinfo=dt.timezone.utc).astimezone()
    nxt = croniter(expr, base_local).get_next(dt.datetime)
    return nxt.astimezone(dt.timezone.utc).replace(tzinfo=None)


def update_settings(
    db: Session,
    pipeline: models.Pipeline,
    *,
    name: str,
    description: str,
    env_text: str,
    schedule_cron: str,
    schedule_enabled: bool,
    webhook_enabled: bool,
    webhook_branch: str,
) -> None:
    schedule_cron = schedule_cron.strip()
    if schedule_enabled and not validate_cron(schedule_cron):
        raise StepError(f"Invalid cron expression '{schedule_cron}' (expected e.g. '0 3 * * *')")
    if (schedule_cron, schedule_enabled) != (pipeline.schedule_cron or "", bool(pipeline.schedule_enabled)):
        # Start counting from now so enabling a schedule never fires for past slots.
        pipeline.last_scheduled_at = models.utcnow()
    pipeline.name = name.strip() or pipeline.name
    pipeline.description = description.strip()
    pipeline.env_text = env_text.replace("\r\n", "\n")
    pipeline.schedule_cron = schedule_cron or None
    pipeline.schedule_enabled = schedule_enabled
    if webhook_enabled and not pipeline.webhook_token:
        pipeline.webhook_token = secrets.token_urlsafe(24)
    elif not webhook_enabled:
        pipeline.webhook_token = None
    pipeline.webhook_branch = webhook_branch.strip() or None
    db.commit()


def regenerate_webhook_token(db: Session, pipeline: models.Pipeline) -> None:
    pipeline.webhook_token = secrets.token_urlsafe(24)
    db.commit()


def parse_webhook(headers, payload: dict | None) -> dict:
    """Pulls branch/commit info out of GitHub, GitLab, Gitea/Forgejo or Bitbucket push payloads.

    Also accepts a generic `{"variables": {...}}` body for plain curl triggers.
    """
    payload = payload or {}
    event = (
        headers.get("x-github-event")
        or headers.get("x-gitea-event")
        or headers.get("x-gitlab-event")
        or headers.get("x-event-key")
        or "generic"
    )
    variables: dict[str, str] = {}
    ref = payload.get("ref") or ""
    commit = payload.get("after") or payload.get("checkout_sha") or ""
    repo = payload.get("repository") or payload.get("project") or {}
    repo_url = repo.get("clone_url") or repo.get("git_http_url") or repo.get("html_url") or ""
    actor = (payload.get("pusher") or {}).get("name") or payload.get("user_name") or ""

    deleted = bool(payload.get("deleted")) or commit == "0" * 40
    changes = (payload.get("push") or {}).get("changes") or []  # Bitbucket
    if changes:
        new = changes[0].get("new")
        if new:
            kind = "tags" if new.get("type") == "tag" else "heads"
            ref = ref or f"refs/{kind}/{new.get('name', '')}"
            commit = commit or (new.get("target") or {}).get("hash", "")
        else:
            deleted = True

    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""
    tag = ref[len("refs/tags/"):] if ref.startswith("refs/tags/") else ""

    skip = None
    if event.lower() not in _PUSH_EVENTS:
        skip = f"ignored '{event}' event (only push events start runs)"
    elif deleted:
        skip = f"'{branch or tag or ref}' was deleted"

    if ref:
        variables["GIT_REF"] = ref
    if branch:
        variables["GIT_BRANCH"] = branch
    if tag:
        variables["GIT_TAG"] = tag
    if commit:
        variables["GIT_COMMIT"] = commit
        variables["GIT_SHORT_SHA"] = commit[:8]
    if repo_url:
        variables["GIT_REPO_URL"] = repo_url
    if actor:
        variables["GIT_PUSHER"] = actor
    for k, v in (payload.get("variables") or {}).items():
        if isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            variables[k] = str(v)

    info_bits = [event]
    if branch or tag:
        info_bits.append(branch or tag)
    if commit:
        info_bits.append(commit[:8])
    if actor:
        info_bits.append(f"by {actor}")
    return {
        "event": event,
        "branch": branch,
        "tag": tag,
        "skip": skip,
        "variables": variables,
        "info": " · ".join(info_bits),
    }


# Header values (lowercased) of push events; anything else is acknowledged but ignored.
_PUSH_EVENTS = {"push", "push hook", "tag push hook", "repo:push", "generic"}


def branch_matches(pattern: str | None, branch: str, tag: str = "") -> bool:
    """`pattern` is comma-separated globs matched against the pushed branch or tag name."""
    if not pattern:
        return True
    name = branch or tag
    if not name:  # generic triggers (plain curl) carry no ref
        return True
    return any(fnmatch.fnmatch(name, p.strip()) for p in pattern.split(",") if p.strip())


# --- execution ------------------------------------------------------------

_cancel_events: dict[int, threading.Event] = {}
_pipeline_locks: dict[int, threading.Lock] = {}
_registry_lock = threading.Lock()


def _pipeline_lock(pipeline_id: int) -> threading.Lock:
    with _registry_lock:
        return _pipeline_locks.setdefault(pipeline_id, threading.Lock())


def has_active_run(db: Session, pipeline_id: int) -> bool:
    return (
        db.query(models.PipelineRun)
        .filter(models.PipelineRun.pipeline_id == pipeline_id, models.PipelineRun.status.in_(ACTIVE_STATUSES))
        .count()
        > 0
    )


def _builtin_vars(pipeline: models.Pipeline, run: models.PipelineRun) -> dict[str, str]:
    return {
        "COCKPIT_RUN_ID": str(run.id),
        "COCKPIT_PIPELINE": pipeline.name,
        "COCKPIT_PIPELINE_ID": str(pipeline.id),
        "COCKPIT_TRIGGER": run.trigger or "manual",
        "COCKPIT_REPOS_DIR": str(REPOS_DIR),
        "COCKPIT_DATE": dt.datetime.now().strftime("%Y%m%d"),
        "COCKPIT_TIMESTAMP": dt.datetime.now().strftime("%Y%m%d%H%M%S"),
    }


def _execute(run_id: int, cancel_event: threading.Event) -> None:
    db = SessionLocal()
    try:
        run = db.get(models.PipelineRun, run_id)
        if not run or not run.pipeline:
            return
        pipeline = run.pipeline
        lock = _pipeline_lock(pipeline.id)

        while not lock.acquire(timeout=1):
            if cancel_event.is_set():
                run.status = "cancelled"
                run.log_text = (run.log_text or "") + "=== Cancelled while queued ===\n"
                run.finished_at = models.utcnow()
                db.commit()
                return
        try:
            _run_steps(db, run, pipeline, cancel_event)
        finally:
            lock.release()
    except Exception:  # noqa: BLE001 - never let a worker thread die silently
        log.exception("pipeline run %s crashed", run_id)
    finally:
        with _registry_lock:
            _cancel_events.pop(run_id, None)
        db.close()


def _run_steps(db: Session, run: models.PipelineRun, pipeline: models.Pipeline, cancel_event: threading.Event) -> None:
    variables = _builtin_vars(pipeline, run)
    variables.update(parse_kv_text(pipeline.env_text))
    variables.update(json.loads(run.vars_json or "{}"))

    run.status = "running"
    run.started_at = models.utcnow()
    db.commit()

    ctx = RunContext(db, run, cancel_event, variables)
    steps = get_steps(pipeline)
    trigger = run.trigger or "manual"
    ctx.write(f"=== Starting pipeline '{pipeline.name}' ({len(steps)} step(s), trigger: {trigger}) ===\n")
    if run.trigger_info:
        ctx.write(f"    {run.trigger_info}\n")
    ctx.write("\n")

    failed_steps: list[int] = []
    try:
        for i, step in enumerate(steps, start=1):
            ctx.check()
            step_def = STEP_TYPES.get(step["type"])
            if not step_def:
                raise StepError(f"Unknown step type '{step['type']}'")
            params = {k: expand_vars(v, ctx.vars) for k, v in (step.get("params") or {}).items()}
            title = params.get("name") or step_def["label"]
            ctx.write(f"--- Step {i}/{len(steps)}: {title} ---\n")
            started = time.monotonic()
            try:
                step_def["run"](db, params, ctx)
                ctx.write(f"--- Step {i} ok ({time.monotonic() - started:.1f}s) ---\n\n")
            except RunCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if params.get("continue_on_error"):
                    failed_steps.append(i)
                    ctx.write(f"\n--- Step {i} FAILED ({exc}), continuing (continue on error) ---\n\n")
                    continue
                ctx.write(f"\n--- Step {i} FAILED after {time.monotonic() - started:.1f}s ---\n")
                raise
        run.status = "success"
        if failed_steps:
            ctx.write(f"=== Pipeline finished; step(s) {', '.join(map(str, failed_steps))} failed but were allowed to ===\n")
        else:
            ctx.write("=== Pipeline succeeded ===\n")
    except RunCancelled:
        run.status = "cancelled"
        ctx.write("\n=== Pipeline cancelled ===\n")
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        ctx.write(f"\n=== Pipeline FAILED: {exc} ===\n")
    finally:
        run.finished_at = models.utcnow()
        run.vars_json = json.dumps(ctx.vars)
        ctx.flush()


def launch_run(
    pipeline_id: int,
    trigger: str = "manual",
    trigger_info: str | None = None,
    variables: dict | None = None,
) -> int:
    """Create a run and execute it on a background thread. Returns the run id."""
    db = SessionLocal()
    try:
        run = models.PipelineRun(
            pipeline_id=pipeline_id,
            status="queued",
            trigger=trigger,
            trigger_info=(trigger_info or "")[:500] or None,
            vars_json=json.dumps(variables or {}),
            log_text="",
        )
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()

    event = threading.Event()
    with _registry_lock:
        _cancel_events[run_id] = event
    threading.Thread(target=_execute, args=(run_id, event), name=f"pipeline-run-{run_id}", daemon=True).start()
    return run_id


def cancel_run(db: Session, run_id: int) -> bool:
    with _registry_lock:
        event = _cancel_events.get(run_id)
    if event:
        event.set()
        return True
    # Not owned by this process (e.g. left over from before a restart): just mark it.
    run = db.get(models.PipelineRun, run_id)
    if run and run.status in ACTIVE_STATUSES:
        run.status = "cancelled"
        run.finished_at = models.utcnow()
        db.commit()
        return True
    return False


def recover_interrupted_runs() -> int:
    """Runs still 'running'/'queued' at startup belonged to a previous process that died."""
    db = SessionLocal()
    try:
        stale = db.query(models.PipelineRun).filter(models.PipelineRun.status.in_(ACTIVE_STATUSES)).all()
        for run in stale:
            run.status = "failed"
            run.finished_at = run.finished_at or models.utcnow()
            run.log_text = (run.log_text or "") + "\n=== Interrupted: the cockpit was restarted during this run ===\n"
        db.commit()
        return len(stale)
    finally:
        db.close()


# --- scheduler ------------------------------------------------------------

class Scheduler:
    """Fires cron-scheduled pipelines. One instance per process; checks every 15s."""

    INTERVAL = 15

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread or not settings.scheduler_enabled:
            return
        self._thread = threading.Thread(target=self._loop, name="pipeline-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.INTERVAL):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")

    def tick(self) -> list[int]:
        fired: list[int] = []
        db = SessionLocal()
        try:
            now = models.utcnow()
            pipelines = (
                db.query(models.Pipeline)
                .filter(models.Pipeline.schedule_enabled.is_(True), models.Pipeline.schedule_cron.isnot(None))
                .all()
            )
            for p in pipelines:
                if not validate_cron(p.schedule_cron):
                    continue
                base = p.last_scheduled_at or p.updated_at or now
                if next_fire_time(p.schedule_cron, base) > now:
                    continue
                p.last_scheduled_at = now
                db.commit()
                if has_active_run(db, p.id):
                    log.info("schedule for '%s' skipped: previous run still active", p.name)
                    continue
                fired.append(launch_run(p.id, trigger="schedule", trigger_info=f"cron: {p.schedule_cron}"))
        finally:
            db.close()
        return fired


scheduler = Scheduler()
