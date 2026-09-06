"""Self-check for the external tools the cockpit shells out to.

Exists because "No such file or directory: 'docker'" (or kubectl, git,
ansible-playbook, ssh) always means the same thing -- that binary isn't on
PATH for whatever user/environment the cockpit process itself is running
in -- but there was no way to see that directly without SSHing in and
guessing. This just runs `shutil.which()` + a --version call for each tool
and reports back what the *cockpit* sees, not what a login shell sees,
which is exactly the thing that differs when running under systemd, cron,
or a container.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _check_tool(display_name: str, binary: str, version_cmd: list[str]) -> dict:
    path = shutil.which(binary)
    if not path:
        return {"name": display_name, "found": False, "path": None, "version": None}
    version = None
    try:
        proc = subprocess.run(version_cmd, capture_output=True, text=True, timeout=10)
        # ssh -V (and a few others) print their version to stderr, not stdout.
        output = (proc.stdout or proc.stderr or "").strip()
        version = output.splitlines()[0] if output else None
    except Exception:  # noqa: BLE001 - best-effort; a bad --version flag shouldn't hide that the binary *was* found
        pass
    return {"name": display_name, "found": True, "path": path, "version": version}


def run_diagnostics() -> dict:
    checks = [
        _check_tool("docker", "docker", ["docker", "--version"]),
        _check_tool("docker compose", "docker", ["docker", "compose", "version"]),
        _check_tool("kubectl", "kubectl", ["kubectl", "version", "--client"]),
        _check_tool("git", "git", ["git", "--version"]),
        _check_tool("ansible-playbook", "ansible-playbook", ["ansible-playbook", "--version"]),
        _check_tool("ssh", "ssh", ["ssh", "-V"]),
    ]
    return {
        "checks": checks,
        "path_env": os.environ.get("PATH", "(not set)"),
        "python_version": sys.version.split()[0],
        "docker_host_env": os.environ.get("DOCKER_HOST", "(not set -- defaults to unix:///var/run/docker.sock)"),
        "docker_socket_exists": os.path.exists("/var/run/docker.sock"),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
    }
