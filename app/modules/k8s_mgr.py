"""Kubernetes control plane.

Each registered cluster stores its kubeconfig file under data/kubeconfigs/.
We build a fresh ApiClient per request via config.new_client_from_config()
(rather than mutating global kubernetes client state) so multiple clusters
can be used concurrently without stepping on each other.

Listing/scaling/logs go through the typed python client; `kubectl apply` is
shelled out to since it already does the right thing for arbitrary
multi-document manifests (create-or-update, strategic merge, etc.).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from sqlalchemy.orm import Session

from ..config import KUBECONFIGS_DIR
from .. import models

ALL_NAMESPACES = "all"


class K8sError(Exception):
    pass


def list_clusters(db: Session) -> list[models.K8sCluster]:
    return db.query(models.K8sCluster).order_by(models.K8sCluster.name).all()


def get_cluster(db: Session, cluster_name: str) -> models.K8sCluster:
    cluster = db.query(models.K8sCluster).filter(models.K8sCluster.name == cluster_name).first()
    if not cluster:
        raise K8sError(f"Unknown cluster '{cluster_name}'")
    return cluster


def save_cluster(db: Session, name: str, kubeconfig_text: str, context_name: str | None = None) -> models.K8sCluster:
    path = KUBECONFIGS_DIR / f"{name}.yaml"
    path.write_text(kubeconfig_text)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    cluster = models.K8sCluster(name=name, kubeconfig_path=str(path), context_name=context_name or None)
    db.add(cluster)
    db.commit()
    db.refresh(cluster)
    return cluster


def delete_cluster(db: Session, cluster_id: int) -> None:
    cluster = db.get(models.K8sCluster, cluster_id)
    if not cluster:
        return
    try:
        Path(cluster.kubeconfig_path).unlink(missing_ok=True)
    except OSError:
        pass
    db.delete(cluster)
    db.commit()


def _api_client(cluster: models.K8sCluster):
    try:
        return config.new_client_from_config(config_file=cluster.kubeconfig_path, context=cluster.context_name or None)
    except Exception as exc:  # noqa: BLE001
        raise K8sError(f"Could not load kubeconfig for '{cluster.name}': {exc}") from exc


def get_apis(db: Session, cluster_name: str):
    cluster = get_cluster(db, cluster_name)
    api_client = _api_client(cluster)
    return client.CoreV1Api(api_client), client.AppsV1Api(api_client), cluster


def ping(db: Session, cluster_name: str) -> dict:
    try:
        cluster = get_cluster(db, cluster_name)
        api_client = _api_client(cluster)
        version = client.VersionApi(api_client).get_code()
        return {"ok": True, "version": f"{version.major}.{version.minor}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def list_namespaces(core_v1: client.CoreV1Api) -> list[str]:
    try:
        return [ns.metadata.name for ns in core_v1.list_namespace().items]
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc


def list_pods(core_v1: client.CoreV1Api, namespace: str) -> list[dict]:
    try:
        items = (
            core_v1.list_pod_for_all_namespaces().items
            if namespace == ALL_NAMESPACES
            else core_v1.list_namespaced_pod(namespace).items
        )
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc
    out = []
    for p in items:
        statuses = p.status.container_statuses or []
        restarts = sum(s.restart_count for s in statuses)
        ready = sum(1 for s in statuses if s.ready)
        out.append(
            {
                "name": p.metadata.name,
                "namespace": p.metadata.namespace,
                "phase": p.status.phase,
                "ready": f"{ready}/{len(statuses)}",
                "restarts": restarts,
                "node": p.spec.node_name,
                "ip": p.status.pod_ip,
            }
        )
    return out


def list_deployments(apps_v1: client.AppsV1Api, namespace: str) -> list[dict]:
    try:
        items = (
            apps_v1.list_deployment_for_all_namespaces().items
            if namespace == ALL_NAMESPACES
            else apps_v1.list_namespaced_deployment(namespace).items
        )
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc
    out = []
    for d in items:
        out.append(
            {
                "name": d.metadata.name,
                "namespace": d.metadata.namespace,
                "replicas": d.spec.replicas,
                "ready": d.status.ready_replicas or 0,
                "available": d.status.available_replicas or 0,
                "image": d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "?",
            }
        )
    return out


def list_services(core_v1: client.CoreV1Api, namespace: str) -> list[dict]:
    try:
        items = (
            core_v1.list_service_for_all_namespaces().items
            if namespace == ALL_NAMESPACES
            else core_v1.list_namespaced_service(namespace).items
        )
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc
    out = []
    for s in items:
        ports = ", ".join(f"{p.port}:{p.target_port}/{p.protocol}" for p in (s.spec.ports or []))
        out.append(
            {
                "name": s.metadata.name,
                "namespace": s.metadata.namespace,
                "type": s.spec.type,
                "cluster_ip": s.spec.cluster_ip,
                "ports": ports,
            }
        )
    return out


def pod_logs(core_v1: client.CoreV1Api, namespace: str, pod_name: str, tail_lines: int = 300) -> str:
    try:
        return core_v1.read_namespaced_pod_log(pod_name, namespace, tail_lines=tail_lines, timestamps=True)
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc


def stream_pod_logs(core_v1: client.CoreV1Api, namespace: str, pod_name: str):
    resp = core_v1.read_namespaced_pod_log(
        pod_name, namespace, follow=True, tail_lines=100, timestamps=True, _preload_content=False
    )
    for line in resp:
        yield line.decode(errors="replace") if isinstance(line, bytes) else str(line)


def delete_pod(core_v1: client.CoreV1Api, namespace: str, pod_name: str) -> None:
    try:
        core_v1.delete_namespaced_pod(pod_name, namespace)
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc


def scale_deployment(apps_v1: client.AppsV1Api, namespace: str, name: str, replicas: int) -> None:
    try:
        apps_v1.patch_namespaced_deployment_scale(name, namespace, {"spec": {"replicas": replicas}})
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc


def rollout_restart_deployment(apps_v1: client.AppsV1Api, namespace: str, name: str) -> None:
    import datetime as dt

    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": dt.datetime.utcnow().isoformat() + "Z"}
                }
            }
        }
    }
    try:
        apps_v1.patch_namespaced_deployment(name, namespace, patch)
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc


def delete_deployment(apps_v1: client.AppsV1Api, namespace: str, name: str) -> None:
    try:
        apps_v1.delete_namespaced_deployment(name, namespace)
    except ApiException as exc:
        raise K8sError(exc.reason or str(exc)) from exc


def _kubectl_env(cluster: models.K8sCluster) -> dict:
    import os

    env = os.environ.copy()
    env["KUBECONFIG"] = cluster.kubeconfig_path
    return env


def apply_manifest(cluster: models.K8sCluster, namespace: str, manifest_text: str) -> str:
    cmd = ["kubectl", "apply", "-f", "-"]
    if namespace and namespace != ALL_NAMESPACES:
        cmd += ["-n", namespace]
    if cluster.context_name:
        cmd += ["--context", cluster.context_name]
    try:
        proc = subprocess.run(
            cmd, input=manifest_text, capture_output=True, text=True, env=_kubectl_env(cluster), timeout=60
        )
    except FileNotFoundError as exc:
        raise K8sError("kubectl is not installed in this environment") from exc
    if proc.returncode != 0:
        raise K8sError(proc.stderr.strip() or "kubectl apply failed")
    return proc.stdout
