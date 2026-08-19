# DevOps Cockpit

A self-hosted, single-binary-ish web cockpit for everyday DevOps work: Docker,
Docker Swarm, Kubernetes, Jenkins, Git, and SSH — plus a built-in CI/CD
pipeline builder that chains them together. Python (FastAPI) backend, server
rendered UI, no separate frontend build step.

## Features

- **Dashboard** — at-a-glance status of every registered Docker host, Swarm,
  Kubernetes cluster, Jenkins instance, SSH server, Git repo, and recent
  pipeline runs.
- **Docker** — containers (start/stop/restart/remove, live log tail, one-shot
  exec), images (pull/remove), networks, volumes, and multiple named Docker
  hosts (local socket + remote `tcp://` engines).
- **Swarm** — init/leave a swarm, view nodes, get join tokens, create overlay
  networks, create/scale/remove services, and deploy/remove Compose stacks.
- **Kubernetes** — register clusters by pasting a kubeconfig, browse
  pods/deployments/services across namespaces, tail pod logs live, delete
  pods, scale/rollout-restart/delete deployments, and `kubectl apply` a
  pasted manifest.
- **Jenkins** — register instances, browse jobs, trigger builds with
  parameters, and watch console output.
- **Git** — clone/register repos, view status/diff/log/branches, pull,
  checkout, and commit + push.
- **SSH** — register servers (password or private key auth), run one-off
  commands, or open a full interactive terminal in the browser (xterm.js
  over a WebSocket).
- **Pipelines** — build a named, ordered pipeline out of steps (git pull,
  docker build/push, swarm stack deploy, k8s apply, k8s rollout restart,
  Jenkins trigger, SSH exec, raw shell), run it, and watch live output.

All credentials (SSH keys/passwords, Jenkins tokens, Git PATs) are encrypted
at rest with a locally-generated Fernet key; the cockpit itself sits behind a
login screen created on first run.

## Quick start

### Docker Compose (recommended)

```bash
cp .env.example .env
# edit .env: set COCKPIT_SECRET_KEY to a long random string

docker compose up -d --build
```

Open http://localhost:8000 and create the first admin account.

This mounts `/var/run/docker.sock` into the container so the cockpit can
manage the host's Docker/Swarm daemon. Remove that volume in
`docker-compose.yml` if you don't want that.

### Running locally with Python

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit as needed
python run.py
```

Requires Python 3.11+. For the Swarm "stack deploy" feature you'll also need
the `docker` CLI on PATH; for `kubectl apply` you'll need `kubectl` on PATH.
Both are already installed in the Docker image.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `COCKPIT_SECRET_KEY` | Signs the session cookie. Set to a long random string in production. |
| `COCKPIT_ENCRYPTION_KEY` | Fernet key encrypting stored credentials. Auto-generated into `data/secret.key` if unset. |
| `DOCKER_HOST` | Connection string for the built-in "local" Docker host (default `unix:///var/run/docker.sock`). |
| `HOST` / `PORT` | Bind address for the web server. |

Runtime data (SQLite DB, the encryption key, cloned repos, uploaded
kubeconfigs) lives under `data/`, which is gitignored.

## Architecture

```
app/
  main.py            FastAPI app factory, middleware, router wiring
  config.py           env-based settings
  database.py, models.py   SQLAlchemy (SQLite by default)
  security.py          password hashing + at-rest credential encryption
  auth.py              session-based login guard
  modules/              one file per integration — the actual Docker/K8s/
                         Jenkins/Git/SSH/pipeline logic, framework-agnostic
  routes/                FastAPI routers (HTTP glue over modules/)
  templates/, static/    server-rendered UI (Jinja2 + a small cockpit.css,
                         xterm.js from CDN for the SSH terminal)
```

Each integration in `modules/` degrades gracefully: if a Docker host,
Kubernetes cluster, Jenkins instance, or SSH server is unreachable, the
corresponding page shows an inline error instead of crashing.

## Security notes

This tool is as powerful as the credentials you give it — Docker socket
access, kubeconfigs, SSH keys, and Jenkins tokens are all real production
capabilities. Treat it like any other admin panel:

- Put it behind your own network boundary (VPN, internal network, or a
  reverse proxy with TLS) — it does not ship TLS itself.
- Set `COCKPIT_SECRET_KEY` and prefer setting `COCKPIT_ENCRYPTION_KEY`
  explicitly for anything beyond local/dev use.
- The pipeline engine's `shell` step and the SSH terminal run arbitrary
  commands by design; only give accounts to people you'd trust with direct
  access to the underlying infrastructure.
