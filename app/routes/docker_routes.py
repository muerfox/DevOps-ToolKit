import asyncio

from fastapi import APIRouter, Depends, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from .. import models
from ..modules import docker_mgr

router = APIRouter(dependencies=[Depends(require_login)])


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
    container_id: str, action: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    try:
        client = docker_mgr.get_client(db, host)
        if action == "remove":
            docker_mgr.remove_container(client, container_id, force=True)
        else:
            docker_mgr.container_action(client, container_id, action)
        client.close()
    except docker_mgr.DockerError:
        pass
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


@router.websocket("/ws/docker/containers/{container_id}/logs")
async def ws_container_logs(websocket: WebSocket, container_id: str, host: str = docker_mgr.LOCAL_HOST_NAME):
    if not websocket.session.get("user_id"):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        client = docker_mgr.get_client(db, host)

        def _gen():
            return docker_mgr.stream_container_logs(client, container_id)

        loop = asyncio.get_event_loop()
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
    repository: str = Form(...), tag: str = Form("latest"), host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    try:
        client = docker_mgr.get_client(db, host)
        for _ in docker_mgr.pull_image(client, repository, tag):
            pass
        client.close()
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/images?host={host}", status_code=303)


@router.post("/docker/images/{image_id}/remove")
def docker_remove_image(image_id: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.remove_image(client, image_id, force=True)
        client.close()
    except docker_mgr.DockerError:
        pass
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
    name: str = Form(...), driver: str = Form("bridge"), host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.create_network(client, name, driver=driver)
        client.close()
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/networks?host={host}", status_code=303)


@router.post("/docker/networks/{network_id}/remove")
def docker_remove_network(network_id: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.remove_network(client, network_id)
        client.close()
    except docker_mgr.DockerError:
        pass
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
def docker_remove_volume(name: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.remove_volume(client, name, force=True)
        client.close()
    except docker_mgr.DockerError:
        pass
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
    name: str = Form(...), base_url: str = Form(...), db: Session = Depends(get_db)
):
    db.add(models.DockerHost(name=name.strip(), base_url=base_url.strip()))
    db.commit()
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
    advertise_addr: str = Form(""), host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.swarm_init(client, advertise_addr or None)
        client.close()
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/leave")
def swarm_leave_route(host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.swarm_leave(client, force=True)
        client.close()
    except docker_mgr.DockerError:
        pass
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
    name: str = Form(...), host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.create_network(client, name, driver="overlay", attachable=True)
        client.close()
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/services/create")
def swarm_create_service(
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
        port_map[int(target)] = int(published)
    env_list = [line.strip() for line in env.splitlines() if line.strip()]
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.create_service(client, name=name, image=image, replicas=replicas, ports=port_map or None, env=env_list)
        client.close()
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/services/{service_id}/scale")
def swarm_scale_service(
    service_id: str, replicas: int = Form(...), host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)
):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.scale_service(client, service_id, replicas)
        client.close()
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/services/{service_id}/remove")
def swarm_remove_service(service_id: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    try:
        client = docker_mgr.get_client(db, host)
        docker_mgr.remove_service(client, service_id)
        client.close()
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/stacks/deploy")
def swarm_deploy_stack(
    stack_name: str = Form(...), compose_text: str = Form(...), host: str = docker_mgr.LOCAL_HOST_NAME,
    db: Session = Depends(get_db),
):
    try:
        base_url = docker_mgr.resolve_base_url(db, host)
        docker_mgr.stack_deploy(base_url, stack_name, compose_text)
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)


@router.post("/docker/swarm/stacks/{stack_name}/remove")
def swarm_remove_stack(stack_name: str, host: str = docker_mgr.LOCAL_HOST_NAME, db: Session = Depends(get_db)):
    try:
        base_url = docker_mgr.resolve_base_url(db, host)
        docker_mgr.stack_rm(base_url, stack_name)
    except docker_mgr.DockerError:
        pass
    return RedirectResponse(f"/docker/swarm?host={host}", status_code=303)
