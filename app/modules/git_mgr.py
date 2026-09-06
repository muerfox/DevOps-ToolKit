"""Git repo management.

Two ways a repo can live:

- Local (default): cloned under data/repos/<name>/, managed via GitPython
  (which shells out to the system git on the cockpit's own host).
- Server-deployed (GitRepo.deploy_server_id set): cloned/pulled/built on a
  registered SSH server instead -- there is no local checkout at all. Every
  operation below runs as a plain SSH command against record.remote_path on
  record.deploy_server, reusing the same SSHServer credentials already used
  for the Servers page. This is what lets "register many servers, each
  running its own project" actually mean the repo (and its custom scripts,
  and any pipeline step referencing this repo) run ON that server, not on
  the cockpit's own disk -- pipeline steps (git_pull, repo_run_script) call
  the same functions below either way and never need to know the
  difference.

Two auth modes apply to origin either way:

- HTTPS: a stored credential (PAT or password) is spliced into the remote
  URL for clone/pull only -- it's never written to .git/config on disk
  (local) or on the server (remote). For a remote pull this does mean the
  credentialed URL is briefly visible in that server's own process list
  while the command runs (there's no SSH-side equivalent of GitPython's
  config-writer trick) -- an accepted tradeoff for a self-hosted admin tool
  that already runs arbitrary commands via other features.
- SSH key: the decrypted private key (passphrase stripped, if any -- see
  security.strip_ssh_key_passphrase) is written to a private temp file --
  locally via tempfile, or on the server via SFTP -- for the duration of
  one clone/pull, referenced via a GIT_SSH_COMMAND override, and removed
  immediately after.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import git
from git import GitCommandError
from sqlalchemy.orm import Session

from ..config import REPOS_DIR, DATA_DIR
from .. import models, security
from . import ssh_mgr

_SEP = "@@COCKPIT_SEP@@"


class GitError(Exception):
    pass


def list_repos(db: Session) -> list[models.GitRepo]:
    return db.query(models.GitRepo).order_by(models.GitRepo.name).all()


def get_repo_record(db: Session, name: str) -> models.GitRepo:
    repo = db.query(models.GitRepo).filter(models.GitRepo.name == name).first()
    if not repo:
        raise GitError(f"Unknown repo '{name}'")
    return repo


def is_remote(record: models.GitRepo) -> bool:
    return record.deploy_server_id is not None


def _authed_url(url: str, username: str | None, credential: str | None) -> str:
    if not credential or not url.startswith("http"):
        return url
    parts = urlsplit(url)
    netloc = f"{username or 'x-access-token'}:{credential}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# --- local (GitPython) SSH-key handling ------------------------------------------------------------

@contextmanager
def _temp_ssh_key_env(key_text: str, passphrase: str | None):
    """Writes a private key to a private temp file for the duration of one
    LOCAL git operation and yields a GIT_SSH_COMMAND env override."""
    key_text = security.normalize_key_text(key_text)
    if passphrase:
        try:
            key_text = security.strip_ssh_key_passphrase(key_text, passphrase)
        except (ValueError, TypeError) as exc:
            raise GitError(f"Could not decrypt private key: {exc}") from exc

    fd, path = tempfile.mkstemp(prefix="git-ssh-key-", dir=str(DATA_DIR))
    os.close(fd)
    try:
        Path(path).write_text(key_text)
        Path(path).chmod(0o600)
        yield {"GIT_SSH_COMMAND": f"ssh -i {path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"}
    finally:
        Path(path).unlink(missing_ok=True)


@contextmanager
def _repo_ssh_env(record: models.GitRepo):
    """Same as _temp_ssh_key_env but pulling the key/passphrase from an
    already-registered LOCAL repo. Yields {} for an HTTPS repo."""
    if record.auth_type != "ssh_key":
        yield {}
        return
    key_text = security.decrypt(record.ssh_key_encrypted)
    passphrase = security.decrypt(record.ssh_key_passphrase_encrypted)
    with _temp_ssh_key_env(key_text, passphrase) as env:
        yield env


# --- remote (over SSH) SSH-key handling ------------------------------------------------------------

def _server_run(server: models.SSHServer, command: str, timeout: int = 300) -> dict:
    try:
        return ssh_mgr.run_command(server, command, timeout=timeout)
    except ssh_mgr.SSHError as exc:
        raise GitError(str(exc)) from exc


@contextmanager
def _remote_temp_key(server: models.SSHServer, key_text: str, passphrase: str | None):
    """SFTP-uploads a passphrase-stripped private key to a temp path in the
    remote account's home directory for the duration of one git operation on
    that server, yields a GIT_SSH_COMMAND-style shell prefix using it,
    removes the file afterward."""
    key_text = security.normalize_key_text(key_text)
    if passphrase:
        try:
            key_text = security.strip_ssh_key_passphrase(key_text, passphrase)
        except (ValueError, TypeError) as exc:
            raise GitError(f"Could not decrypt private key: {exc}") from exc

    try:
        client = ssh_mgr.connect(server)
    except ssh_mgr.SSHError as exc:
        raise GitError(str(exc)) from exc

    remote_key_path = f".cockpit_git_key_{secrets.token_hex(8)}"
    try:
        sftp = client.open_sftp()
        try:
            with sftp.open(remote_key_path, "w") as f:
                f.write(key_text)
            sftp.chmod(remote_key_path, 0o600)
            yield f"GIT_SSH_COMMAND={shlex.quote('ssh -i ' + remote_key_path + ' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new')} "
        finally:
            try:
                sftp.remove(remote_key_path)
            except Exception:  # noqa: BLE001
                pass
            sftp.close()
    finally:
        client.close()


@contextmanager
def _remote_repo_env_prefix(record: models.GitRepo):
    """Same idea as _repo_ssh_env, but for an already-registered
    server-deployed repo. Yields "" for an HTTPS repo."""
    if record.auth_type != "ssh_key":
        yield ""
        return
    key_text = security.decrypt(record.ssh_key_encrypted)
    passphrase = security.decrypt(record.ssh_key_passphrase_encrypted)
    with _remote_temp_key(record.deploy_server, key_text, passphrase) as prefix:
        yield prefix


def _remote_run(record: models.GitRepo, command: str, timeout: int = 300) -> dict:
    server = record.deploy_server
    if not server:
        raise GitError(f"Repo '{record.name}' has no deploy server configured")
    return _server_run(server, command, timeout=timeout)


def _require_ok(result: dict, action: str) -> str:
    if result["exit_code"] != 0:
        raise GitError(f"{action} failed: {(result['stderr'] or result['stdout']).strip()}")
    return result["stdout"]


# --- register / clone ------------------------------------------------------------

def register_and_clone(
    db: Session,
    name: str,
    url: str,
    branch: str = "main",
    auth_type: str = "https",
    username: str | None = None,
    credential: str | None = None,
    ssh_key: str | None = None,
    ssh_key_passphrase: str | None = None,
    deploy_server_id: int | None = None,
    remote_path: str | None = None,
) -> models.GitRepo:
    if auth_type == "ssh_key" and ssh_key:
        ssh_key = security.normalize_key_text(ssh_key)

    if deploy_server_id:
        server = db.get(models.SSHServer, deploy_server_id)
        if not server:
            raise GitError("Unknown deploy server")
        if not remote_path:
            raise GitError("A remote path is required when deploying a repo to a server")

        check = _server_run(server, f"test -e {shlex.quote(remote_path)} && echo exists", timeout=15)
        if "exists" in check["stdout"]:
            raise GitError(f"{remote_path} already exists on {server.name}")

        branch_flag = f"--branch {shlex.quote(branch)} " if branch else ""
        if auth_type == "ssh_key":
            if not ssh_key:
                raise GitError("SSH auth requires a private key")
            with _remote_temp_key(server, ssh_key, ssh_key_passphrase) as prefix:
                result = _server_run(
                    server, f"{prefix}git clone {branch_flag}{shlex.quote(url)} {shlex.quote(remote_path)}", timeout=300
                )
        else:
            clone_url = _authed_url(url, username, credential)
            result = _server_run(
                server, f"git clone {branch_flag}{shlex.quote(clone_url)} {shlex.quote(remote_path)}", timeout=300
            )
        _require_ok(result, "Clone")
        local_path = ""
    else:
        local_path_obj = REPOS_DIR / name
        if local_path_obj.exists():
            raise GitError(f"A local clone already exists at {local_path_obj}")
        try:
            if auth_type == "ssh_key":
                if not ssh_key:
                    raise GitError("SSH auth requires a private key")
                with _temp_ssh_key_env(ssh_key, ssh_key_passphrase) as env:
                    git.Repo.clone_from(url, str(local_path_obj), branch=branch or None, env=env)
            else:
                clone_url = _authed_url(url, username, credential)
                git.Repo.clone_from(clone_url, str(local_path_obj), branch=branch or None)
        except GitCommandError as exc:
            shutil.rmtree(local_path_obj, ignore_errors=True)
            raise GitError(f"Clone failed: {exc.stderr or exc}") from exc
        local_path = str(local_path_obj)

    repo = models.GitRepo(
        name=name,
        url=url,
        local_path=local_path,
        branch=branch,
        auth_type=auth_type,
        username=username or None,
        credential_encrypted=security.encrypt(credential),
        ssh_key_encrypted=security.encrypt(ssh_key),
        ssh_key_passphrase_encrypted=security.encrypt(ssh_key_passphrase),
        deploy_server_id=deploy_server_id or None,
        remote_path=remote_path or None,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def delete_repo(db: Session, repo_id: int, delete_files: bool = True) -> None:
    repo = db.get(models.GitRepo, repo_id)
    if not repo:
        return
    if delete_files:
        if is_remote(repo):
            try:
                _remote_run(repo, f"rm -rf {shlex.quote(repo.remote_path)}", timeout=60)
            except GitError:
                pass  # best-effort -- still remove the DB record either way
        else:
            shutil.rmtree(repo.local_path, ignore_errors=True)
    db.delete(repo)
    db.commit()


def open_repo(record: models.GitRepo) -> git.Repo:
    try:
        return git.Repo(record.local_path)
    except Exception as exc:  # noqa: BLE001
        raise GitError(f"Could not open local clone at {record.local_path}: {exc}") from exc


def status(record: models.GitRepo) -> dict:
    if is_remote(record):
        cmd = (
            f"cd {shlex.quote(record.remote_path)} && "
            f"(git status --short; echo {_SEP}; git rev-parse --abbrev-ref HEAD)"
        )
        out = _require_ok(_remote_run(record, cmd), "Status")
        short_status, _, branch = out.partition(_SEP)
        lines = [line for line in short_status.splitlines() if line.strip()]
        return {
            "branch": branch.strip() or "?",
            "is_dirty": bool(lines),
            "untracked": [line[3:].strip() for line in lines if line.startswith("??")],
            "changed": [line[3:].strip() for line in lines if not line.startswith("??")],
            "staged": [],  # not distinguished in this simplified remote view
        }
    repo = open_repo(record)
    try:
        branch = repo.active_branch.name
    except TypeError:
        branch = "(detached HEAD)"
    return {
        "branch": branch,
        "is_dirty": repo.is_dirty(untracked_files=True),
        "untracked": repo.untracked_files,
        "changed": [item.a_path for item in repo.index.diff(None)],
        "staged": [item.a_path for item in repo.index.diff("HEAD")],
    }


def log(record: models.GitRepo, max_count: int = 30) -> list[dict]:
    if is_remote(record):
        fmt = "%h\x1f%an\x1f%cI\x1f%s"
        cmd = f"cd {shlex.quote(record.remote_path)} && git log -n {int(max_count)} --format={shlex.quote(fmt)}"
        try:
            out = _require_ok(_remote_run(record, cmd), "Log")
        except GitError:
            return []
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            commits.append({"hexsha": parts[0], "author": parts[1], "date": parts[2], "message": parts[3]})
        return commits
    repo = open_repo(record)
    out = []
    try:
        for c in repo.iter_commits(max_count=max_count):
            out.append(
                {
                    "hexsha": c.hexsha[:8],
                    "author": c.author.name,
                    "date": c.committed_datetime.isoformat(),
                    "message": c.message.strip().splitlines()[0] if c.message else "",
                }
            )
    except (ValueError, GitCommandError):
        pass
    return out


def branches(record: models.GitRepo) -> dict:
    if is_remote(record):
        cmd = (
            f"cd {shlex.quote(record.remote_path)} && "
            f"git branch --format='%(refname:short)' && echo {_SEP} && git rev-parse --abbrev-ref HEAD"
        )
        out = _require_ok(_remote_run(record, cmd), "Branches")
        branch_list, _, current = out.partition(_SEP)
        return {"current": current.strip() or None, "local": [b.strip() for b in branch_list.splitlines() if b.strip()]}
    repo = open_repo(record)
    try:
        current = repo.active_branch.name
    except TypeError:
        current = None
    return {
        "current": current,
        "local": [b.name for b in repo.branches],
    }


def checkout(record: models.GitRepo, branch: str) -> None:
    if is_remote(record):
        cmd = f"cd {shlex.quote(record.remote_path)} && git checkout {shlex.quote(branch)}"
        _require_ok(_remote_run(record, cmd), "Checkout")
        return
    repo = open_repo(record)
    try:
        repo.git.checkout(branch)
    except GitCommandError as exc:
        raise GitError(f"Checkout failed: {exc.stderr or exc}") from exc


def diff(record: models.GitRepo) -> str:
    if is_remote(record):
        cmd = f"cd {shlex.quote(record.remote_path)} && git diff"
        return _require_ok(_remote_run(record, cmd), "Diff")
    repo = open_repo(record)
    try:
        return repo.git.diff()
    except GitCommandError as exc:
        raise GitError(str(exc)) from exc


def pull(record: models.GitRepo) -> str:
    if is_remote(record):
        remote_path_q = shlex.quote(record.remote_path)
        with _remote_repo_env_prefix(record) as prefix:
            if record.auth_type == "ssh_key":
                cmd = f"cd {remote_path_q} && {prefix}git pull"
            else:
                url = _authed_url(record.url, record.username, security.decrypt(record.credential_encrypted))
                plain = shlex.quote(record.url)
                authed = shlex.quote(url)
                # Same "swap credential in, pull, swap it back out" idea as the
                # local path's config_writer trick -- there's no SSH-side
                # equivalent that avoids a brief appearance in that server's own
                # process list, which is an accepted tradeoff (see module
                # docstring).
                cmd = (
                    f"cd {remote_path_q} && git remote set-url origin {authed} && "
                    f"git pull; status=$?; git remote set-url origin {plain}; exit $status"
                )
            result = _remote_run(record, cmd, timeout=300)
        return _require_ok(result, "Pull")

    repo = open_repo(record)
    try:
        if record.auth_type == "ssh_key":
            with _repo_ssh_env(record) as env, repo.git.custom_environment(**env):
                result = repo.remotes.origin.pull()
        else:
            url = _authed_url(record.url, record.username, security.decrypt(record.credential_encrypted))
            with repo.remotes.origin.config_writer as cw:
                cw.set("url", url)
            result = repo.remotes.origin.pull()
            with repo.remotes.origin.config_writer as cw:
                cw.set("url", record.url)
        return "\n".join(f"{r.ref}: {r.note or r.flags}" for r in result)
    except GitCommandError as exc:
        raise GitError(f"Pull failed: {exc.stderr or exc}") from exc


def commit_and_push(record: models.GitRepo, message: str) -> str:
    if is_remote(record):
        raise GitError(
            "Commit & push isn't available for a server-deployed repo -- it's meant as a deploy "
            "target, not a place to author commits from. Use that server's terminal if you really need to."
        )

    repo = open_repo(record)
    with repo.config_reader() as cr:
        has_identity = cr.has_option("user", "email")
    if not has_identity:
        with repo.config_writer() as cw:
            cw.set_value("user", "name", "DevOps Cockpit")
            cw.set_value("user", "email", "cockpit@localhost")

    repo.git.add(A=True)
    if not repo.is_dirty(index=True, working_tree=False) and not repo.index.diff("HEAD"):
        return "Nothing to commit."
    repo.index.commit(message)

    try:
        if record.auth_type == "ssh_key":
            with _repo_ssh_env(record) as env, repo.git.custom_environment(**env):
                push_info = repo.remotes.origin.push()
        else:
            url = _authed_url(record.url, record.username, security.decrypt(record.credential_encrypted))
            with repo.remotes.origin.config_writer as cw:
                cw.set("url", url)
            push_info = repo.remotes.origin.push()
            with repo.remotes.origin.config_writer as cw:
                cw.set("url", record.url)
        return "\n".join(pi.summary for pi in push_info)
    except GitCommandError as exc:
        raise GitError(f"Push failed: {exc.stderr or exc}") from exc


# --- custom scripts (build/test/lint/... run against the checkout) --------

def list_scripts(db: Session, repo_id: int) -> list[models.RepoScript]:
    return (
        db.query(models.RepoScript)
        .filter(models.RepoScript.repo_id == repo_id)
        .order_by(models.RepoScript.name)
        .all()
    )


def list_all_scripts(db: Session) -> list[models.RepoScript]:
    """All saved scripts across every repo, for pickers like the pipeline builder."""
    return (
        db.query(models.RepoScript)
        .join(models.GitRepo)
        .order_by(models.GitRepo.name, models.RepoScript.name)
        .all()
    )


def get_script(db: Session, script_id: int) -> models.RepoScript:
    script = db.get(models.RepoScript, script_id)
    if not script:
        raise GitError(f"Unknown script id {script_id}")
    return script


def create_script(db: Session, repo_id: int, name: str, script_text: str) -> models.RepoScript:
    # Same CRLF fix as ssh_mgr's create_script: a browser <textarea>
    # normalizes to CRLF on submission regardless of what was pasted, which
    # would otherwise show up as a literal '\r' in the middle of commands.
    script_text = script_text.replace("\r\n", "\n").replace("\r", "\n")
    script = models.RepoScript(repo_id=repo_id, name=name.strip(), script_text=script_text)
    db.add(script)
    db.commit()
    db.refresh(script)
    return script


def delete_script(db: Session, script_id: int) -> None:
    script = db.get(models.RepoScript, script_id)
    if script:
        db.delete(script)
        db.commit()


def run_script(record: models.GitRepo, script_text: str, timeout: int = 300) -> dict:
    """Runs with the repo's checkout as cwd: locally via subprocess for a
    local repo, or over SSH on record.deploy_server for a server-deployed
    one -- same shape return either way ({exit_code, stdout, stderr})."""
    if is_remote(record):
        cmd = f"cd {shlex.quote(record.remote_path)} && {script_text}"
        try:
            return ssh_mgr.run_command(record.deploy_server, cmd, timeout=timeout)
        except ssh_mgr.SSHError as exc:
            raise GitError(str(exc)) from exc

    proc = subprocess.run(
        script_text, shell=True, cwd=record.local_path, capture_output=True, text=True, timeout=timeout
    )
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
