import datetime as dt

from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship

from .database import Base


def utcnow():
    return dt.datetime.utcnow()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_admin = Column(Boolean, default=True)
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
    local_path = Column(String(500), nullable=False)
    branch = Column(String(120), default="main")
    username = Column(String(120), nullable=True)
    credential_encrypted = Column(Text, nullable=True)  # PAT / password for HTTPS remotes
    created_at = Column(DateTime, default=utcnow)


class Pipeline(Base):
    __tablename__ = "pipelines"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(String(500), nullable=True)
    steps_json = Column(Text, nullable=False, default="[]")
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
