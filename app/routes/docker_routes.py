import asyncio
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_operator
from ..flash import flash
from .. import models
from ..modules import docker_mgr

router = APIRouter(dependencies=[Depends(require_operator)])

# Websocket routes live on their own router: a router-level Depends(require_login)
# would still get resolved for websocket routes too, but require_login() takes a
# Request (only available for HTTP requests) -- FastAPI can't satisfy that for a
# websocket connection and the handshake fails with a 500 before it ever reaches
# our code. These routes do their own session check by hand instead.
ws_router = APIRouter()


def _error_text(exc: Exception) -> str:
    return getattr(exc, "explanation", None) or str(exc) or exc.__class__.__name__


@contextmanager
def _action(request: Request, db: Session, host: str, success: str | None = None):
    """Runs a docker action; any failure becomes an error toast instead of a 500 page."""
    client = None
    try:
        client = docker_mgr.get_client(db, host)
        yield client
        if success:
            flash(request, success, "success")
    except Exception as exc:  # noqa: BLE001 - docker APIError/NotFound/connection errors
        flash(request, _error_text(exc), "error")
    finally:
        if client is not None:
            client.close()


def _ctx(request, db, host, **extra):
    ctx = {
        "request": request,
        "hosts": docker_mgr.list_hosts(db),
        "host": host or docker_mgr.LOCAL_HOST_NAME,
    }
    ctx.update(extra)
    return ctx


# --- containers ------------------------------------------------------------

@router.get("/docker")
def docker_index(request: Request, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    error = None
    containers = []
    try:
        client = docker_mgr.get_client(db, host)
        containers = docker_mgr.list_containers(client)
        client.close()
    except docker_mgr.DockerError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "docker/containers.html", _ctx(request, db, host, containers=containers, error=error)
    )


@router.post("/docker/containers/{container_id}/{action}")
def docker_container_action(
    request: Request,
    container_id: str,
    action: str,
    host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    past = {"start": "started", "stop": "stopped", "restart": "restarted", "remove": "removed", "pause": "paused", "unpause": "unpaused", "kill": "killed"}
    with _action(request, db, host, f"Container {container_id} {past.get(action, action)}.") as client:
        if action == "remove":
            docker_mgr.remove_container(client, container_id, force=True)
        else:
            docker_mgr.container_action(client, container_id, action)
    return RedirectResponse(f"/docker?host={host}", status_code=303)


@router.get("/docker/containers/{container_id}/logs")
def docker_container_logs(
    request: Request, container_id: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    logs, error = "", None
    try:
        client = docker_mgr.get_client(db, host)
        logs = docker_mgr.container_logs(client, container_id)
        client.close()
    except docker_mgr.DockerError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "docker/logs.html",
        _ctx(request, db, host, container_id=container_id, logs=logs, error=error),
    )


@router.post("/docker/containers/{container_id}/exec")
def docker_container_exec(
    request: Request,
    container_id: str,
    command: str = Form(...),
    host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    result, error = None, None
    try:
        client = docker_mgr.get_client(db, host)
        result = docker_mgr.exec_in_container(client, container_id, command)
        client.close()
    except docker_mgr.DockerError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "docker/logs.html",
        _ctx(request, db, host, container_id=container_id, logs=None, exec_result=result, error=error),
    )


@ws_router.websocket("/ws/docker/containers/{container_id}/logs")
async def ws_container_logs(websocket: WebSocket, container_id: str, host: str = docker_mgr.LOCAL_HOST_NAME):
    from ..database import SessionLocal
    from ..auth import websocket_user_is_operator

    user_id = websocket.session.get("user_id")
    if not user_id:
        await websocket.close(code=4401)
        return
    auth_db = SessionLocal()
    try:
        is_operator = websocket_user_is_operator(auth_db, user_id)
    finally:
        auth_db.close()
    if not is_operator:
        await websocket.close(code=4403)
        return
    await websocket.accept()

    db = SessionLocal()
    loop = asyncio.get_event_loop()
    try:
        # docker.DockerClient() auto-negotiates the API version with a real
        # HTTP call to the daemon; for a slow/unreachable host that blocks
        # up to its own timeout. Keep it off the event loop like the log
        # stream below already is.
        client = await loop.run_in_executor(None, docker_mgr.get_client, db, host)

        def _gen():
            return docker_mgr.stream_container_logs(client, container_id)

        it = await loop.run_in_executor(None, _gen)
        try:
            while True:
                chunk = await loop.run_in_executor(None, lambda: next(it, None))
                if chunk is None:
                    break
                await websocket.send_text(chunk)
        except WebSocketDisconnect:
            pass
        client.close()
    except docker_mgr.DockerError as exc:
        try:
            await websocket.send_text(f"\n[error] {exc}\n")
        except Exception:
            pass
    finally:
        db.close()
        try:
            await websocket.close()
        except Exception:
            pass


# --- images ------------------------------------------------------------

@router.get("/docker/images")
def docker_images(request: Request, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    images, error = [], None
    try:
        client = docker_mgr.get_client(db, host)
        images = docker_mgr.list_images(client)
        client.close()
    except docker_mgr.DockerError as exc:
        error = str(exc)
    return templates.TemplateResponse("docker/images.html", _ctx(request, db, host, images=images, error=error))


@router.post("/docker/images/pull")
def docker_pull_image(
    request: Request,
    repository: str = Form(...),
    tag: str = Form("latest"),
    host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    tag = tag.strip() or "latest"
    with _action(request, db, host, f"Pulled {repository}:{tag}.") as client:
        for line in docker_mgr.pull_image(client, repository.strip(), tag):
            if line.lower().startswith("error"):
                raise docker_mgr.DockerError(line.strip())
    return RedirectResponse(f"/docker/images?host={host}", status_code=303)


@router.post("/docker/images/{image_id}/remove")
def docker_remove_image(
    request: Request, image_id: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    with _action(request, db, host, f"Image {image_id} removed.") as client:
        docker_mgr.remove_image(client, image_id, force=True)
    return RedirectResponse(f"/docker/images?host={host}", status_code=303)


# --- networks ------------------------------------------------------------

@router.get("/docker/networks")
def docker_networks(request: Request, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    networks, error = [], None
    try:
        client = docker_mgr.get_client(db, host)
        networks = docker_mgr.list_networks(client)
        client.close()
    except docker_mgr.DockerError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "docker/networks.html", _ctx(request, db, host, networks=networks, error=error)
    )


@router.post("/docker/networks/create")
def docker_create_network(
    request: Request,
    name: str = Form(...),
    driver: str = Form("bridge"),
    host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    with _action(request, db, host, f"Network {name} created.") as client:
        docker_mgr.create_network(client, name, driver=driver)
    return RedirectResponse(f"/docker/networks?host={host}", status_code=303)


@router.post("/docker/networks/{network_id}/remove")
def docker_remove_network(
    request: Request, network_id: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    with _action(request, db, host, "Network removed.") as client:
        docker_mgr.remove_network(client, network_id)
    return RedirectResponse(f"/docker/networks?host={host}", status_code=303)


# --- volumes ------------------------------------------------------------

@router.get("/docker/volumes")
def docker_volumes(request: Request, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    volumes, error = [], None
    try:
        client = docker_mgr.get_client(db, host)
        volumes = docker_mgr.list_volumes(client)
        client.close()
    except docker_mgr.DockerError as exc:
        error = str(exc)
    return templates.TemplateResponse("docker/volumes.html", _ctx(request, db, host, volumes=volumes, error=error))


@router.post("/docker/volumes/{name}/remove")
def docker_remove_volume(
    request: Request, name: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    with _action(request, db, host, f"Volume {name} removed.") as client:
        docker_mgr.remove_volume(client, name, force=True)
    return RedirectResponse(f"/docker/volumes?host={host}", status_code=303)


# --- docker hosts ------------------------------------------------------------

@router.get("/docker/hosts")
def docker_hosts(request: Request, db: Session = Depends(get_db)):
    hosts = db.query(models.DockerHost).order_by(models.DockerHost.name).all()
    pings = {"local": docker_mgr.ping(db, "local")}
    for h in hosts:
        pings[h.name] = docker_mgr.ping(db, h.name)
    return templates.TemplateResponse(
        "docker/hosts.html", {"request": request, "hosts": hosts, "pings": pings}
    )


@router.post("/docker/hosts/create")
def docker_hosts_create(
    request: Request, name: str = Form(...), base_url: str = Form(...), db: Session = Depends(get_db)
):
    db.add(models.DockerHost(name=name.strip(), base_url=base_url.strip()))
    db.commit()
    status = docker_mgr.ping(db, name.strip())
    if status.get("ok"):
        flash(request, f"Docker host '{name.strip()}' added (engine v{status.get('version')}).", "success")
    else:
        flash(request, f"Docker host '{name.strip()}' added, but it isn't reachable yet: {status.get('error')}", "error")
    return RedirectResponse("/docker/hosts", status_code=303)


@router.post("/docker/hosts/{host_id}/delete")
def docker_hosts_delete(host_id: int, db: Session = Depends(get_db)):
    host = db.get(models.DockerHost, host_id)
    if host:
        db.delete(host)
        db.commit()
    return RedirectResponse("/docker/hosts", status_code=303)


# --- swarm ------------------------------------------------------------

@router.get("/docker/swarm")
def swarm_index(request: Request, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    error = None
    info, nodes, services, stacks = None, [], [], []
    try:
        client = docker_mgr.get_client(db, host)
        info = docker_mgr.swarm_info(client)
        if info["active"]:
            nodes = docker_mgr.list_nodes(client)
            services = docker_mgr.list_services(client)
            try:
                stacks = docker_mgr.list_stacks(docker_mgr.resolve_base_url(db, host))
            except docker_mgr.DockerError:
                stacks = []
        client.close()
    except docker_mgr.DockerError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "docker/swarm.html",
        _ctx(request, db, host, info=info, nodes=nodes, services=services, stacks=stacks, error=error),
    )


@router.post("/docker/swarm/init")
def swarm_init_route(
    request: Request, advertise_addr: str = Form(""), host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    with _action(request, db, host, "Swarm initialised.") as client:
        docker_mgr.swarm_init(client, advertise_addr or None)
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/leave")
def swarm_leave_route(request: Request, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    with _action(request, db, host, "Left the swarm.") as client:
        docker_mgr.swarm_leave(client, force=True)
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.get("/docker/swarm/join-tokens")
def swarm_join_tokens_route(request: Request, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    tokens, error = None, None
    try:
        client = docker_mgr.get_client(db, host)
        tokens = docker_mgr.swarm_join_tokens(client)
        client.close()
    except docker_mgr.DockerError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "docker/join_tokens.html", _ctx(request, db, host, tokens=tokens, error=error)
    )


@router.post("/docker/swarm/networks/create")
def swarm_create_overlay_network(
    request: Request, name: str = Form(...), host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    with _action(request, db, host, f"Overlay network {name} created.") as client:
        docker_mgr.create_network(client, name, driver="overlay", attachable=True)
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/services/create")
def swarm_create_service(
    request: Request,
    name: str = Form(...),
    image: str = Form(...),
    replicas: int = Form(1),
    ports: str = Form(""),
    env: str = Form(""),
    host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    port_map = {}
    for pair in ports.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        published, target = pair.split(":", 1)
        try:
            port_map[int(target)] = int(published)
        except ValueError:
            flash(request, f"Invalid port mapping '{pair}' (expected published:target, e.g. 8080:80).", "error")
            return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)
    env_list = [line.strip() for line in env.splitlines() if line.strip()]
    with _action(request, db, host, f"Service {name} created.") as client:
        docker_mgr.create_service(client, name=name, image=image, replicas=replicas, ports=port_map or None, env=env_list)
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/services/{service_id}/scale")
def swarm_scale_service(
    request: Request,
    service_id: str,
    replicas: int = Form(...),
    host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    with _action(request, db, host, f"Service scaled to {replicas} replica(s).") as client:
        docker_mgr.scale_service(client, service_id, replicas)
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/services/{service_id}/remove")
def swarm_remove_service(
    request: Request, service_id: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    with _action(request, db, host, "Service removed.") as client:
        docker_mgr.remove_service(client, service_id)
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/stacks/deploy")
def swarm_deploy_stack(
    request: Request,
    stack_name: str = Form(...),
    compose_text: str = Form(...),
    host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    try:
        base_url = docker_mgr.resolve_base_url(db, host)
        docker_mgr.stack_deploy(base_url, stack_name, compose_text)
        flash(request, f"Stack {stack_name} deployed.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(request, _error_text(exc), "error")
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/stacks/{stack_name}/remove")
def swarm_remove_stack(
    request: Request, stack_name: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    try:
        base_url = docker_mgr.resolve_base_url(db, host)
        docker_mgr.stack_rm(base_url, stack_name)
        flash(request, f"Stack {stack_name} removed.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(request, _error_text(exc), "error")
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)
