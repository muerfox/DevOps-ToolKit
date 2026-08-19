"""Docker + Swarm control plane.

Wraps the docker-py SDK for everything the API supports (containers, images,
networks, volumes, swarm nodes/services) and shells out to the `docker` CLI
for the handful of operations docker-py has no API for (`stack deploy`,
which is a Compose-file feature layered on top of the Engine API).
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import docker
from docker.errors import APIError, NotFound
from sqlalchemy.orm import Session

from ..config import settings
from .. import models

LOCAL_HOST_NAME = "local"


class DockerError(Exception):
    pass


def list_hosts(db: Session) -> list[dict]:
    hosts = [{"name": LOCAL_HOST_NAME, "base_url": settings.default_docker_host, "is_local": True}]
    for h in db.query(models.DockerHost).order_by(models.DockerHost.name).all():
        hosts.append({"name": h.name, "base_url": h.base_url, "is_local": False})
    return hosts


def resolve_base_url(db: Session, host_name: str | None) -> str:
    if not host_name or host_name == LOCAL_HOST_NAME:
        return settings.default_docker_host
    host = db.query(models.DockerHost).filter(models.DockerHost.name == host_name).first()
    if not host:
        raise DockerError(f"Unknown docker host '{host_name}'")
    return host.base_url


def get_client(db: Session, host_name: str | None = None) -> docker.DockerClient:
    base_url = resolve_base_url(db, host_name)
    try:
        return docker.DockerClient(base_url=base_url, timeout=10)
    except Exception as exc:  # noqa: BLE001 - surface as a friendly cockpit error
        raise DockerError(f"Could not connect to Docker at {base_url}: {exc}") from exc


def ping(db: Session, host_name: str | None = None) -> dict:
    try:
        client = get_client(db, host_name)
        version = client.version()
        client.close()
        return {"ok": True, "version": version.get("Version"), "api_version": version.get("ApiVersion")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# --- containers ------------------------------------------------------------

def list_containers(client: docker.DockerClient, show_all: bool = True) -> list[dict]:
    out = []
    for c in client.containers.list(all=show_all):
        out.append(
            {
                "id": c.short_id,
                "full_id": c.id,
                "name": c.name,
                "image": c.image.tags[0] if c.image and c.image.tags else (c.image.short_id if c.image else "?"),
                "status": c.status,
                "state": c.attrs.get("State", {}),
                "ports": c.ports,
                "created": c.attrs.get("Created"),
            }
        )
    return out


def container_action(client: docker.DockerClient, container_id: str, action: str) -> None:
    c = client.containers.get(container_id)
    try:
        getattr(c, action)()
    except AttributeError as exc:
        raise DockerError(f"Unsupported container action '{action}'") from exc


def remove_container(client: docker.DockerClient, container_id: str, force: bool = False) -> None:
    client.containers.get(container_id).remove(force=force)


def container_logs(client: docker.DockerClient, container_id: str, tail: int = 300) -> str:
    c = client.containers.get(container_id)
    return c.logs(tail=tail, timestamps=True).decode(errors="replace")


def stream_container_logs(client: docker.DockerClient, container_id: str):
    c = client.containers.get(container_id)
    for chunk in c.logs(stream=True, follow=True, tail=100, timestamps=True):
        yield chunk.decode(errors="replace")


def exec_in_container(client: docker.DockerClient, container_id: str, command: str) -> dict:
    c = client.containers.get(container_id)
    result = c.exec_run(cmd=["/bin/sh", "-c", command], demux=False)
    return {"exit_code": result.exit_code, "output": result.output.decode(errors="replace")}


# --- images ------------------------------------------------------------

def list_images(client: docker.DockerClient) -> list[dict]:
    out = []
    for img in client.images.list():
        out.append(
            {
                "id": img.short_id.replace("sha256:", ""),
                "tags": img.tags,
                "size_mb": round((img.attrs.get("Size") or 0) / (1024 * 1024), 1),
                "created": img.attrs.get("Created"),
            }
        )
    return out


def pull_image(client: docker.DockerClient, repository: str, tag: str = "latest"):
    for line in client.api.pull(repository, tag=tag, stream=True, decode=True):
        status = line.get("status", "")
        progress = line.get("progress", "")
        yield f"{status} {progress}\n"


def remove_image(client: docker.DockerClient, image_id: str, force: bool = False) -> None:
    client.images.remove(image_id, force=force)


# --- networks / volumes ------------------------------------------------------------

def list_networks(client: docker.DockerClient) -> list[dict]:
    out = []
    for n in client.networks.list():
        out.append({"id": n.short_id, "name": n.name, "driver": n.attrs.get("Driver"), "scope": n.attrs.get("Scope")})
    return out


def create_network(client: docker.DockerClient, name: str, driver: str = "bridge", attachable: bool = True) -> None:
    client.networks.create(name, driver=driver, attachable=attachable)


def remove_network(client: docker.DockerClient, network_id: str) -> None:
    client.networks.get(network_id).remove()


def list_volumes(client: docker.DockerClient) -> list[dict]:
    out = []
    for v in client.volumes.list():
        out.append({"name": v.name, "driver": v.attrs.get("Driver"), "mountpoint": v.attrs.get("Mountpoint")})
    return out


def remove_volume(client: docker.DockerClient, name: str, force: bool = False) -> None:
    client.volumes.get(name).remove(force=force)


# --- swarm ------------------------------------------------------------

def swarm_info(client: docker.DockerClient) -> dict:
    info = client.info()
    swarm = info.get("Swarm", {})
    return {
        "active": swarm.get("LocalNodeState") == "active",
        "is_manager": swarm.get("ControlAvailable", False),
        "node_id": swarm.get("NodeID"),
        "nodes": swarm.get("Nodes"),
        "managers": swarm.get("Managers"),
    }


def swarm_init(client: docker.DockerClient, advertise_addr: str | None = None) -> str:
    kwargs = {}
    if advertise_addr:
        kwargs["advertise_addr"] = advertise_addr
    return client.swarm.init(**kwargs)


def swarm_leave(client: docker.DockerClient, force: bool = False) -> None:
    client.swarm.leave(force=force)


def swarm_join_tokens(client: docker.DockerClient) -> dict:
    client.swarm.reload()
    attrs = client.swarm.attrs.get("JoinTokens", {})
    return {"worker": attrs.get("Worker"), "manager": attrs.get("Manager")}


def list_nodes(client: docker.DockerClient) -> list[dict]:
    out = []
    for n in client.nodes.list():
        spec = n.attrs.get("Spec", {})
        status = n.attrs.get("Status", {})
        out.append(
            {
                "id": n.short_id,
                "hostname": n.attrs.get("Description", {}).get("Hostname"),
                "role": spec.get("Role"),
                "availability": spec.get("Availability"),
                "state": status.get("State"),
                "addr": status.get("Addr"),
                "leader": n.attrs.get("ManagerStatus", {}).get("Leader", False),
            }
        )
    return out


def list_services(client: docker.DockerClient) -> list[dict]:
    out = []
    for s in client.services.list():
        spec = s.attrs.get("Spec", {})
        mode = spec.get("Mode", {})
        replicas = mode.get("Replicated", {}).get("Replicas") if "Replicated" in mode else None
        out.append(
            {
                "id": s.short_id,
                "name": spec.get("Name"),
                "image": spec.get("TaskTemplate", {}).get("ContainerSpec", {}).get("Image"),
                "replicas": replicas,
                "mode": "replicated" if "Replicated" in mode else "global",
            }
        )
    return out


def create_service(
    client: docker.DockerClient,
    name: str,
    image: str,
    replicas: int = 1,
    ports: dict | None = None,
    env: list[str] | None = None,
    networks: list[str] | None = None,
):
    endpoint_spec = None
    if ports:
        endpoint_spec = docker.types.EndpointSpec(ports=ports)
    return client.services.create(
        image=image,
        name=name,
        mode=docker.types.ServiceMode("replicated", replicas=replicas),
        endpoint_spec=endpoint_spec,
        env=env or [],
        networks=networks or None,
    )


def scale_service(client: docker.DockerClient, service_id: str, replicas: int) -> None:
    svc = client.services.get(service_id)
    svc.scale(replicas)


def remove_service(client: docker.DockerClient, service_id: str) -> None:
    client.services.get(service_id).remove()


def service_logs(client: docker.DockerClient, service_id: str, tail: int = 300) -> str:
    svc = client.services.get(service_id)
    return b"".join(svc.logs(tail=tail, timestamps=True, stdout=True, stderr=True)).decode(errors="replace")


# --- stacks (compose-on-swarm; no docker-py API, shell out to the CLI) --------

def _cli_env(base_url: str) -> dict:
    import os

    env = os.environ.copy()
    env["DOCKER_HOST"] = base_url
    return env


def list_stacks(base_url: str) -> list[dict]:
    proc = subprocess.run(
        ["docker", "stack", "ls", "--format", "{{.Name}}\t{{.Services}}"],
        capture_output=True,
        text=True,
        env=_cli_env(base_url),
        timeout=20,
    )
    if proc.returncode != 0:
        raise DockerError(proc.stderr.strip() or "docker stack ls failed")
    stacks = []
    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        stacks.append({"name": parts[0], "services": parts[1] if len(parts) > 1 else "?"})
    return stacks


def stack_deploy(base_url: str, stack_name: str, compose_text: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
        f.write(compose_text)
        compose_path = f.name
    try:
        proc = subprocess.run(
            ["docker", "stack", "deploy", "-c", compose_path, stack_name],
            capture_output=True,
            text=True,
            env=_cli_env(base_url),
            timeout=120,
        )
        if proc.returncode != 0:
            raise DockerError(proc.stderr.strip() or "docker stack deploy failed")
        return proc.stdout
    finally:
        Path(compose_path).unlink(missing_ok=True)


def stack_rm(base_url: str, stack_name: str) -> str:
    proc = subprocess.run(
        ["docker", "stack", "rm", stack_name],
        capture_output=True,
        text=True,
        env=_cli_env(base_url),
        timeout=60,
    )
    if proc.returncode != 0:
        raise DockerError(proc.stderr.strip() or "docker stack rm failed")
    return proc.stdout
