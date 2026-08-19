import asyncio

from fastapi import APIRouter, Depends, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from .. import models
from ..modules import ssh_mgr

router = APIRouter(dependencies=[Depends(require_login)])


@router.get("/ssh")
def ssh_index(request: Request, db: Session = Depends(get_db)):
    servers = ssh_mgr.list_servers(db)
    pings = {s.name: ssh_mgr.ping(s) for s in servers}
    return templates.TemplateResponse(
        "ssh/index.html", {"request": request, "servers": servers, "pings": pings}
    )


@router.post("/ssh/servers/create")
def ssh_servers_create(
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    username: str = Form(...),
    auth_type: str = Form("password"),
    secret: str = Form(...),
    passphrase: str = Form(""),
    tags: str = Form(""),
    db: Session = Depends(get_db),
):
    ssh_mgr.create_server(
        db, name.strip(), host.strip(), port, username.strip(), auth_type, secret, passphrase or None, tags.strip() or None
    )
    return RedirectResponse("/ssh", status_code=303)


@router.post("/ssh/servers/{server_id}/delete")
def ssh_servers_delete(server_id: int, db: Session = Depends(get_db)):
    ssh_mgr.delete_server(db, server_id)
    return RedirectResponse("/ssh", status_code=303)


@router.get("/ssh/servers/{name}/run")
def ssh_run_get(request: Request, name: str, db: Session = Depends(get_db)):
    record = ssh_mgr.get_server(db, name)
    return templates.TemplateResponse("ssh/run.html", {"request": request, "record": record, "result": None, "error": None})


@router.post("/ssh/servers/{name}/run")
def ssh_run_post(request: Request, name: str, command: str = Form(...), db: Session = Depends(get_db)):
    record = ssh_mgr.get_server(db, name)
    result, error = None, None
    try:
        result = ssh_mgr.run_command(record, command)
    except ssh_mgr.SSHError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "ssh/run.html", {"request": request, "record": record, "result": result, "error": error, "command": command}
    )


@router.get("/ssh/servers/{name}/terminal")
def ssh_terminal(request: Request, name: str, db: Session = Depends(get_db)):
    record = ssh_mgr.get_server(db, name)
    return templates.TemplateResponse("ssh/terminal.html", {"request": request, "record": record})


@router.websocket("/ws/ssh/{name}/shell")
async def ws_ssh_shell(websocket: WebSocket, name: str):
    if not websocket.session.get("user_id"):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    from ..database import SessionLocal

    db = SessionLocal()
    try:
        record = ssh_mgr.get_server(db, name)
    except ssh_mgr.SSHError as exc:
        await websocket.send_text(f"\r\n[error] {exc}\r\n")
        await websocket.close()
        db.close()
        return
    db.close()

    try:
        client, channel = ssh_mgr.open_shell(record)
    except ssh_mgr.SSHError as exc:
        await websocket.send_text(f"\r\n[error] {exc}\r\n")
        await websocket.close()
        return

    loop = asyncio.get_event_loop()

    async def reader():
        while True:
            data = await loop.run_in_executor(None, channel.recv, 4096)
            if not data:
                break
            await websocket.send_text(data.decode(errors="replace"))

    async def writer():
        while True:
            msg = await websocket.receive_text()
            channel.send(msg)

    reader_task = asyncio.create_task(reader())
    writer_task = asyncio.create_task(writer())
    try:
        done, pending = await asyncio.wait({reader_task, writer_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            channel.close()
        except Exception:
            pass
        client.close()
        try:
            await websocket.close()
        except Exception:
            pass
