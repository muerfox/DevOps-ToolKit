"""Git repo management, built on GitPython (which shells out to the system git).

Repos are cloned under data/repos/<name>/. Two auth modes:

- HTTPS: a stored credential (PAT or password) is spliced into the remote
  URL for clone/pull/push only -- it's never written to .git/config on disk.
- SSH key: the decrypted private key (passphrase stripped, if any -- see
  security.strip_ssh_key_passphrase) is written to a private temp file for
  the duration of one clone/pull/push, referenced via a GIT_SSH_COMMAND
  override (GitPython's custom_environment()/clone_from(env=...)), and
  removed immediately after. Never written to .git/config either.
"""

from __future__ import annotations

import os
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


class GitError(Exception):
    pass


def list_repos(db: Session) -> list[models.GitRepo]:
    return db.query(models.GitRepo).order_by(models.GitRepo.name).all()


def get_repo_record(db: Session, name: str) -> models.GitRepo:
    repo = db.query(models.GitRepo).filter(models.GitRepo.name == name).first()
    if not repo:
        raise GitError(f"Unknown repo '{name}'")
    return repo


def _authed_url(url: str, username: str | None, credential: str | None) -> str:
    if not credential or not url.startswith("http"):
        return url
    parts = urlsplit(url)
    netloc = f"{username or 'x-access-token'}:{credential}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@contextmanager
def _temp_ssh_key_env(key_text: str, passphrase: str | None):
    """Writes a private key to a private temp file for the duration of one
    git operation and yields a GIT_SSH_COMMAND env override pointing at it."""
    # Covers both callers below: a freshly-submitted key straight from the
    # create-repo form (browser <textarea> submission normalizes to CRLF --
    # see security.normalize_key_text) and an already-stored, already-broken
    # key from before register_and_clone normalized at save time too.
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
    already-registered repo's encrypted columns. Yields {} for an HTTPS
    repo (nothing to override)."""
    if record.auth_type != "ssh_key":
        yield {}
        return
    key_text = security.decrypt(record.ssh_key_encrypted)
    passphrase = security.decrypt(record.ssh_key_passphrase_encrypted)
    with _temp_ssh_key_env(key_text, passphrase) as env:
        yield env


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
) -> models.GitRepo:
    local_path = REPOS_DIR / name
    if local_path.exists():
        raise GitError(f"A local clone already exists at {local_path}")

    if auth_type == "ssh_key" and ssh_key:
        ssh_key = security.normalize_key_text(ssh_key)

    try:
        if auth_type == "ssh_key":
            if not ssh_key:
                raise GitError("SSH auth requires a private key")
            with _temp_ssh_key_env(ssh_key, ssh_key_passphrase) as env:
                git.Repo.clone_from(url, str(local_path), branch=branch or None, env=env)
        else:
            clone_url = _authed_url(url, username, credential)
            git.Repo.clone_from(clone_url, str(local_path), branch=branch or None)
    except GitCommandError as exc:
        shutil.rmtree(local_path, ignore_errors=True)
        raise GitError(f"Clone failed: {exc.stderr or exc}") from exc

    repo = models.GitRepo(
        name=name,
        url=url,
        local_path=str(local_path),
        branch=branch,
        auth_type=auth_type,
        username=username or None,
        credential_encrypted=security.encrypt(credential),
        ssh_key_encrypted=security.encrypt(ssh_key),
        ssh_key_passphrase_encrypted=security.encrypt(ssh_key_passphrase),
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
        shutil.rmtree(repo.local_path, ignore_errors=True)
    db.delete(repo)
    db.commit()


def open_repo(record: models.GitRepo) -> git.Repo:
    try:
        return git.Repo(record.local_path)
    except Exception as exc:  # noqa: BLE001
        raise GitError(f"Could not open local clone at {record.local_path}: {exc}") from exc


def status(record: models.GitRepo) -> dict:
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
    repo = open_repo(record)
    try:
        repo.git.checkout(branch)
    except GitCommandError as exc:
        raise GitError(f"Checkout failed: {exc.stderr or exc}") from exc


def diff(record: models.GitRepo) -> str:
    repo = open_repo(record)
    try:
        return repo.git.diff()
    except GitCommandError as exc:
        raise GitError(str(exc)) from exc


def pull(record: models.GitRepo) -> str:
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


# --- custom scripts (build/test/lint/... run locally against the checkout) --

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
    """Runs entirely locally, with the repo's checkout as cwd -- unlike
    SSHServer scripts there's no remote host involved, so this shells out
    directly instead of going over an SSH connection."""
    proc = subprocess.run(
        script_text, shell=True, cwd=record.local_path, capture_output=True, text=True, timeout=timeout
    )
    return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
