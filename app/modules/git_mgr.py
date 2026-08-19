"""Git repo management, built on GitPython (which shells out to the system git).

Repos are cloned under data/repos/<name>/. For HTTPS remotes with a stored
credential (PAT or password), the credential is spliced into the URL for
clone/pull/push only -- it's never written to .git/config on disk. SSH
remotes rely on whatever SSH identity is available to the git process
(e.g. an agent), same as running git by hand.
"""

from __future__ import annotations

import shutil
from urllib.parse import urlsplit, urlunsplit

import git
from git import GitCommandError
from sqlalchemy.orm import Session

from ..config import REPOS_DIR
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


def register_and_clone(
    db: Session,
    name: str,
    url: str,
    branch: str = "main",
    username: str | None = None,
    credential: str | None = None,
) -> models.GitRepo:
    local_path = REPOS_DIR / name
    if local_path.exists():
        raise GitError(f"A local clone already exists at {local_path}")

    clone_url = _authed_url(url, username, credential)
    try:
        git.Repo.clone_from(clone_url, str(local_path), branch=branch or None)
    except GitCommandError as exc:
        shutil.rmtree(local_path, ignore_errors=True)
        raise GitError(f"Clone failed: {exc.stderr or exc}") from exc

    repo = models.GitRepo(
        name=name,
        url=url,
        local_path=str(local_path),
        branch=branch,
        username=username or None,
        credential_encrypted=security.encrypt(credential),
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
    url = _authed_url(record.url, record.username, security.decrypt(record.credential_encrypted))
    try:
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

    url = _authed_url(record.url, record.username, security.decrypt(record.credential_encrypted))
    try:
        with repo.remotes.origin.config_writer as cw:
            cw.set("url", url)
        push_info = repo.remotes.origin.push()
        with repo.remotes.origin.config_writer as cw:
            cw.set("url", record.url)
        return "\n".join(pi.summary for pi in push_info)
    except GitCommandError as exc:
        raise GitError(f"Push failed: {exc.stderr or exc}") from exc
