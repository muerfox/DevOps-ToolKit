import asyncio

from fastapi import APIRouter, Depends, Request, Form, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..templating import templates
from ..auth import require_login
from .. import models
from ..modules import k8s_mgr

router = APIRouter(dependencies=[Depends(require_login)])


def _ctx(request, db, cluster, namespace, **extra):
    ctx = {
        "request": request,
        "clusters": k8s_mgr.list_clusters(db),
        "cluster": cluster,
        "namespace": namespace or k8s_mgr.ALL_NAMESPACES,
    }
    ctx.update(extra)
    return ctx


def _first_cluster(db: Session) -> str | None:
    clusters = k8s_mgr.list_clusters(db)
    return clusters[0].name if clusters else None


@router.get("/k8s")
def k8s_index(db: Session = Depends(get_db)):
    cluster = _first_cluster(db)
    if not cluster:
        return RedirectResponse("/k8s/clusters", status_code=303)
    return RedirectResponse(f"/k8s/pods?cluster={cluster}", status_code=303)


# --- clusters ------------------------------------------------------------

@router.get("/k8s/clusters")
def k8s_clusters(request: Request, db: Session = Depends(get_db)):
    clusters = k8s_mgr.list_clusters(db)
    pings = {c.name: k8s_mgr.ping(db, c.name) for c in clusters}
    return templates.TemplateResponse(
        "k8s/clusters.html", {"request": request, "clusters": clusters, "pings": pings}
    )


@router.post("/k8s/clusters/create")
def k8s_clusters_create(
    name: str = Form(...),
    kubeconfig_text: str = Form(...),
    context_name: str = Form(""),
    db: Session = Depends(get_db),
):
    k8s_mgr.save_cluster(db, name.strip(), kubeconfig_text, context_name.strip() or None)
    return RedirectResponse("/k8s/clusters", status_code=303)


@router.post("/k8s/clusters/{cluster_id}/delete")
def k8s_clusters_delete(cluster_id: int, db: Session = Depends(get_db)):
    k8s_mgr.delete_cluster(db, cluster_id)
    return RedirectResponse("/k8s/clusters", status_code=303)


# --- pods ------------------------------------------------------------

@router.get("/k8s/pods")
def k8s_pods(
    request: Request, cluster: str, namespace: str = k8s_mgr.ALL_NAMESPACES, db: Session = Depends(get_db)
):
    pods, namespaces, error = [], [], None
    try:
        core_v1, _, _ = k8s_mgr.get_apis(db, cluster)
        namespaces = k8s_mgr.list_namespaces(core_v1)
        pods = k8s_mgr.list_pods(core_v1, namespace)
    except k8s_mgr.K8sError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "k8s/pods.html", _ctx(request, db, cluster, namespace, pods=pods, namespaces=namespaces, error=error)
    )


@router.post("/k8s/pods/{namespace}/{name}/delete")
def k8s_pod_delete(namespace: str, name: str, cluster: str, db: Session = Depends(get_db)):
    try:
        core_v1, _, _ = k8s_mgr.get_apis(db, cluster)
        k8s_mgr.delete_pod(core_v1, namespace, name)
    except k8s_mgr.K8sError:
        pass
    return RedirectResponse(f"/k8s/pods?cluster={cluster}&namespace={namespace}", status_code=303)


@router.get("/k8s/pods/{namespace}/{name}/logs")
def k8s_pod_logs(request: Request, namespace: str, name: str, cluster: str, db: Session = Depends(get_db)):
    logs, error = "", None
    try:
        core_v1, _, _ = k8s_mgr.get_apis(db, cluster)
        logs = k8s_mgr.pod_logs(core_v1, namespace, name)
    except k8s_mgr.K8sError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "k8s/pod_logs.html",
        _ctx(request, db, cluster, namespace, pod_name=name, logs=logs, error=error),
    )


@router.websocket("/ws/k8s/pods/{namespace}/{name}/logs")
async def ws_pod_logs(websocket: WebSocket, namespace: str, name: str, cluster: str):
    if not websocket.session.get("user_id"):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        core_v1, _, _ = k8s_mgr.get_apis(db, cluster)
        loop = asyncio.get_event_loop()
        it = await loop.run_in_executor(None, lambda: k8s_mgr.stream_pod_logs(core_v1, namespace, name))
        try:
            while True:
                chunk = await loop.run_in_executor(None, lambda: next(it, None))
                if chunk is None:
                    break
                await websocket.send_text(chunk)
        except WebSocketDisconnect:
            pass
    except k8s_mgr.K8sError as exc:
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


# --- deployments ------------------------------------------------------------

@router.get("/k8s/deployments")
def k8s_deployments(
    request: Request, cluster: str, namespace: str = k8s_mgr.ALL_NAMESPACES, db: Session = Depends(get_db)
):
    deployments, namespaces, error = [], [], None
    try:
        core_v1, apps_v1, _ = k8s_mgr.get_apis(db, cluster)
        namespaces = k8s_mgr.list_namespaces(core_v1)
        deployments = k8s_mgr.list_deployments(apps_v1, namespace)
    except k8s_mgr.K8sError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "k8s/deployments.html",
        _ctx(request, db, cluster, namespace, deployments=deployments, namespaces=namespaces, error=error),
    )


@router.post("/k8s/deployments/{namespace}/{name}/scale")
def k8s_deployment_scale(namespace: str, name: str, cluster: str, replicas: int = Form(...), db: Session = Depends(get_db)):
    try:
        _, apps_v1, _ = k8s_mgr.get_apis(db, cluster)
        k8s_mgr.scale_deployment(apps_v1, namespace, name, replicas)
    except k8s_mgr.K8sError:
        pass
    return RedirectResponse(f"/k8s/deployments?cluster={cluster}&namespace={namespace}", status_code=303)


@router.post("/k8s/deployments/{namespace}/{name}/restart")
def k8s_deployment_restart(namespace: str, name: str, cluster: str, db: Session = Depends(get_db)):
    try:
        _, apps_v1, _ = k8s_mgr.get_apis(db, cluster)
        k8s_mgr.rollout_restart_deployment(apps_v1, namespace, name)
    except k8s_mgr.K8sError:
        pass
    return RedirectResponse(f"/k8s/deployments?cluster={cluster}&namespace={namespace}", status_code=303)


@router.post("/k8s/deployments/{namespace}/{name}/delete")
def k8s_deployment_delete(namespace: str, name: str, cluster: str, db: Session = Depends(get_db)):
    try:
        _, apps_v1, _ = k8s_mgr.get_apis(db, cluster)
        k8s_mgr.delete_deployment(apps_v1, namespace, name)
    except k8s_mgr.K8sError:
        pass
    return RedirectResponse(f"/k8s/deployments?cluster={cluster}&namespace={namespace}", status_code=303)


# --- services ------------------------------------------------------------

@router.get("/k8s/services")
def k8s_services(
    request: Request, cluster: str, namespace: str = k8s_mgr.ALL_NAMESPACES, db: Session = Depends(get_db)
):
    services, namespaces, error = [], [], None
    try:
        core_v1, _, _ = k8s_mgr.get_apis(db, cluster)
        namespaces = k8s_mgr.list_namespaces(core_v1)
        services = k8s_mgr.list_services(core_v1, namespace)
    except k8s_mgr.K8sError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "k8s/services.html",
        _ctx(request, db, cluster, namespace, services=services, namespaces=namespaces, error=error),
    )


# --- apply ------------------------------------------------------------

@router.get("/k8s/apply")
def k8s_apply_get(request: Request, cluster: str, namespace: str = "default", db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "k8s/apply.html", _ctx(request, db, cluster, namespace, output=None, error=None)
    )


@router.post("/k8s/apply")
def k8s_apply_post(
    request: Request,
    cluster: str,
    manifest_text: str = Form(...),
    namespace: str = Form("default"),
    db: Session = Depends(get_db),
):
    output, error = None, None
    try:
        _, _, cluster_obj = k8s_mgr.get_apis(db, cluster)
        output = k8s_mgr.apply_manifest(cluster_obj, namespace, manifest_text)
    except k8s_mgr.K8sError as exc:
        error = str(exc)
    return templates.TemplateResponse(
        "k8s/apply.html", _ctx(request, db, cluster, namespace, output=output, error=error)
    )
