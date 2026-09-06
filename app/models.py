import datetime as dt
import secrets

from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from .database import Base


def utcnow():
    return dt.datetime.utcnow()


def generate_webhook_token():
    return secrets.token_urlsafe(32)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=True)
    # A developer account is locked out of the whole cockpit except /deploy,
    # scoped to whichever pipelines an admin/operator has granted it via
    # PipelineAccess -- see auth.require_operator. Kept as its own flag
    # (default False, additive) rather than folding into is_admin so every
    # existing account's access is unchanged by this column's addition.
    is_developer = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class SSHServer(Base):
    __tablename__ = "ssh_servers"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    host = Column(String(255), nullable=False)
    port = Column(Integer, default=22)
    username = Column(String(120), nullable=False)
    auth_type = Column(String(20), default="password")  # password | key
    secret_encrypted = Column(Text, nullable=True)  # password OR private key PEM
    passphrase_encrypted = Column(Text, nullable=True)
    tags = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)


class LoginThrottle(Base):
    """One row per client IP, tracking the login-page rate limit/ban state."""

    __tablename__ = "login_throttle"

    ip = Column(String(64), primary_key=True)
    fail_count = Column(Integer, default=0)
    ban_level = Column(Integer, default=0)  # 0 = none, 1 = 10min, 2 = 1hr, 3 = 1day (capped)
    banned_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class SSHScript(Base):
    __tablename__ = "ssh_scripts"

    id = Column(Integer, primary_key=True)
    server_id = Column(Integer, ForeignKey("ssh_servers.id"), nullable=False)
    name = Column(String(120), nullable=False)
    script_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    server = relationship("SSHServer", backref="scripts")


class DockerHost(Base):
    __tablename__ = "docker_hosts"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    base_url = Column(String(255), nullable=False)  # unix:///... or tcp://host:port
    tls_verify = Column(Boolean, default=False)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class K8sCluster(Base):
    __tablename__ = "k8s_clusters"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    kubeconfig_path = Column(String(500), nullable=False)
    context_name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)


class JenkinsInstance(Base):
    __tablename__ = "jenkins_instances"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    url = Column(String(255), nullable=False)
    username = Column(String(120), nullable=False)
    token_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class GitRepo(Base):
    __tablename__ = "git_repos"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    url = Column(String(500), nullable=False)
    # Empty string (not NULL, to avoid an ALTER COLUMN our auto-migration
    # can't do) for a server-deployed repo -- there is no local clone.
    local_path = Column(String(500), nullable=False, default="")
    branch = Column(String(120), default="main")
    auth_type = Column(String(20), default="https")  # https | ssh_key
    username = Column(String(120), nullable=True)
    credential_encrypted = Column(Text, nullable=True)  # PAT / password for HTTPS remotes
    ssh_key_encrypted = Column(Text, nullable=True)  # private key PEM for auth_type=ssh_key
    ssh_key_passphrase_encrypted = Column(Text, nullable=True)
    # When set, this repo lives and runs entirely on that registered server
    # (cloned/pulled/built there over SSH) instead of on the cockpit's own
    # disk -- see git_mgr.py's module docstring for how every operation
    # branches on this.
    deploy_server_id = Column(Integer, ForeignKey("ssh_servers.id"), nullable=True)
    remote_path = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    deploy_server = relationship("SSHServer", backref="deployed_repos")


class RepoScript(Base):
    """A saved, reusable command (or multi-line script) for a repo -- build,
    test, lint, whatever -- run locally with the repo's checkout as its
    working directory. Same idea as SSHScript, just local instead of remote."""

    __tablename__ = "repo_scripts"

    id = Column(Integer, primary_key=True)
    repo_id = Column(Integer, ForeignKey("git_repos.id"), nullable=False)
    name = Column(String(120), nullable=False)
    script_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)

    repo = relationship("GitRepo", backref="scripts")


class Pipeline(Base):
    __tablename__ = "pipelines"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(String(500), nullable=True)
    steps_json = Column(Text, nullable=False, default="[]")
    webhook_token = Column(String(64), unique=True, default=generate_webhook_token)
    # Only meaningful for the native GitHub webhook route: if set, a push
    # whose ref doesn't match this branch is acknowledged but not run. Blank
    # means "any branch" (the generic /trigger endpoint always ignores this
    # -- whatever calls it already decided when to, e.g. a workflow's own
    # `on: push: branches:` filter).
    webhook_branch = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    runs = relationship("PipelineRun", back_populates="pipeline", cascade="all, delete-orphan")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(Integer, primary_key=True)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"))
    status = Column(String(20), default="running")  # running | success | failed | cancelled
    log_text = Column(Text, default="")
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)

    pipeline = relationship("Pipeline", back_populates="runs")


class PipelineAccess(Base):
    """Grants a developer-role user permission to deploy (run) one pipeline.
    Irrelevant for admin/operator accounts, which can already run anything."""

    __tablename__ = "pipeline_access"
    __table_args__ = (UniqueConstraint("user_id", "pipeline_id", name="uq_pipeline_access_user_pipeline"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    pipeline_id = Column(Integer, ForeignKey("pipelines.id"), nullable=False)
    created_at = Column(DateTime, default=utcnow)

    user = relationship("User", backref="pipeline_access")
    pipeline = relationship("Pipeline", backref="developer_access")


class AnsiblePlaybook(Base):
    __tablename__ = "ansible_playbooks"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(String(500), nullable=True)
    playbook_yaml = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class AnsibleRun(Base):
    __tablename__ = "ansible_runs"

    id = Column(Integer, primary_key=True)
    label = Column(String(200), nullable=False)  # playbook name, or "Manage user", etc.
    target_type = Column(String(20), nullable=False)  # server | group | all
    target_value = Column(String(255), nullable=True)  # server name, or group/tag name
    # Snapshot of the exact playbook that ran, kept independent of any saved
    # AnsiblePlaybook row so run history stays accurate even if that row is
    # later edited or deleted.
    playbook_yaml = Column(Text, nullable=False)
    status = Column(String(20), default="running")  # running | success | failed
    log_text = Column(Text, default="")
    started_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)
