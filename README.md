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
- **Git** — clone/register repos over HTTPS (token/password) or SSH (private
  key, with or without a passphrase), view status/diff/log/branches, pull,
  checkout, and commit + push. Save reusable custom scripts per repo (build,
  test, lint, whatever) that run locally with the checkout as their working
  directory -- one click from the repo's page, or as a pipeline step.
- **SSH / Servers** — register servers (password or private key auth), see
  the Docker containers running on each one (over SSH, no Docker API needed),
  run one-off commands, save reusable multi-line automation scripts (e.g.
  `cd /app && git pull && docker compose up -d --build`), or open a full
  interactive terminal in the browser (xterm.js over a WebSocket).
- **Ansible** — run playbooks against a single server, a group (servers
  sharing a tag), or all of them, with a dynamic inventory generated per run
  from the servers you've already registered. Quick actions for the common
  cases (create/update/remove a user + password + sudo + SSH key, install OS
  updates, write a file) generate the playbook for you; or write/save your
  own. Live streamed output, same as pipeline runs.
- **Pipelines** — build a named, ordered pipeline out of steps (git pull,
  docker build/push, Compose up `-d --build`, swarm stack deploy, k8s apply,
  k8s rollout restart, Jenkins trigger, SSH exec/saved script, repo saved
  script, raw shell), run it, and watch live output. Each pipeline gets its
  own secret webhook token so it can also be
  triggered by a `git push` — from a GitHub Actions workflow step, a native
  GitHub repository webhook, or any CI system that can do an HTTP POST — for
  a full git-to-deploy flow with no manual click needed.
- **Users & roles** — three account levels: **Admin** (everything, plus
  managing users), **Operator** (everything except managing users), and
  **Developer** — locked out of the whole cockpit except a `/deploy` page
  showing only the pipelines an admin/operator has explicitly granted it,
  each a single "Deploy" button with no steps, servers, or credentials
  visible. Built for handing a contractor or junior dev a one-click way to
  ship their own project without giving them the keys to the infrastructure.
  Every account, regardless of role, can change its own password from the
  "Change password" link in the sidebar.

All credentials (SSH keys/passwords, Jenkins tokens, Git PATs) are encrypted
at rest with a locally-generated Fernet key; the cockpit itself sits behind a
login screen created on first run.

## Quick start

Two ways to run this. If any of your pipelines run `docker compose up
--build` (or Ansible/scripts that expect real host paths), **use the host
install** -- see why below.

### Directly on the host (recommended if you run `docker compose` from pipelines)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit as needed: set COCKPIT_SECRET_KEY
python run.py
```

Requires Python 3.11+, `git`, and `docker`/`docker compose` on PATH (already
there if Docker's installed normally). For the Swarm "stack deploy" feature
you'll also need the `docker` CLI (same binary); for `kubectl apply` you'll
need `kubectl` on PATH.

**Why this instead of Docker Compose:** if the cockpit itself runs inside a
container, any pipeline step that runs `docker compose ... --build` still
technically works (mounting `/var/run/docker.sock` lets the container's
`docker` CLI talk to the *host's* daemon) -- but any **bind-mount volume**
in *your* project's `docker-compose.yml` gets resolved against the
cockpit **container's** filesystem paths (e.g. `/app/data/repos/myapp`),
not the real host paths those mounts need to point at. Running the cockpit
as a normal process on the host removes that indirection entirely: a
pipeline's `docker compose up --build` step runs exactly as if you'd typed
it in a terminal there, against real host paths, with the host's `docker`
socket used directly (no mount, no path translation, nothing to conflict).

To run it persistently (auto-restart, starts at boot -- what Docker
Compose's `restart: unless-stopped` would otherwise give you), install it
as a systemd service:

```bash
sudo useradd -r -s /usr/sbin/nologin devops   # or reuse an existing user
sudo usermod -aG docker devops                # so it can talk to Docker/Swarm
sudo cp -r . /opt/DevOps-ToolKit && cd /opt/DevOps-ToolKit
sudo -u devops python3 -m venv .venv
sudo -u devops .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env   # edit it: COCKPIT_SECRET_KEY, etc.
sudo chown -R devops:devops /opt/DevOps-ToolKit

sudo cp deploy/systemd/devops-cockpit.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now devops-cockpit
```

The unit file has two placeholders (`User=`, `WorkingDirectory=`) -- edit
`/etc/systemd/system/devops-cockpit.service` if you installed somewhere
other than `/opt/DevOps-ToolKit` or under a different user. `journalctl -u
devops-cockpit -f` for logs.

### Docker Compose

```bash
cp .env.example .env
# edit .env: set COCKPIT_SECRET_KEY to a long random string

docker compose up -d --build
```

Open http://localhost:8000 and create the first admin account. Mounts
`/var/run/docker.sock` so the cockpit can manage the host's Docker/Swarm
daemon -- remove that volume in `docker-compose.yml` if you don't want that.
Fine for managing Docker/K8s/Jenkins/servers and running most pipeline
steps; just keep the bind-mount caveat above in mind for any `docker
compose`/Ansible steps that touch host paths on the machine the cockpit
runs on.

## Webhook-triggered deploys

Every pipeline gets its own webhook token, shown on that pipeline's page
along with ready-to-copy snippets. Three ways to wire it up:

1. **Any CI, a plain POST** — works for GitHub Actions, GitLab CI, Bitbucket,
   Jenkins, or a bare `curl`:
   ```bash
   curl -X POST "https://your-cockpit/pipelines/<id>/trigger" \
     -H "X-Webhook-Token: <token>"
   ```
   (the token also works as a `?token=` query param if a header is awkward
   for your CI).
2. **A GitHub Actions workflow step** — same call, with the token kept as a
   repo secret instead of pasted into the workflow file.
3. **A native GitHub repository webhook** — no workflow file at all: repo
   Settings → Webhooks → Add webhook, Payload URL
   `https://your-cockpit/pipelines/<id>/webhook/github`, Secret = the token.
   Verified via GitHub's own HMAC-SHA256 signature rather than the bearer
   token; a webhook `ping` is acknowledged without triggering a run.

Regenerate a pipeline's token any time from its page to revoke every
existing trigger using it.

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
                         Jenkins/Git/SSH/Ansible/pipeline logic, framework-agnostic
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
- The pipeline engine's `shell` step, Ansible playbooks, and the SSH terminal
  all run arbitrary commands by design; only give accounts to people you'd
  trust with direct access to the underlying infrastructure.
- A pipeline's webhook token is a bearer secret — anyone who has it can
  trigger that pipeline (whatever its steps do) with no login. Treat it like
  a password: keep it in your CI's secret store, not committed to a repo,
  and regenerate it if it ever leaks.
