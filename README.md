# DevOps Cockpit

A self-hosted, single-binary-ish web cockpit for everyday DevOps work: Docker,
Docker Swarm, Kubernetes, Jenkins, Git, and SSH — plus a built-in CI/CD
pipeline builder that chains them together. Python (FastAPI) backend, server
rendered UI, no separate frontend build step.

## Features

- **Dashboard** — live CPU / memory / disk / network of the cockpit host
  (with sparklines), and the status of every registered Docker host, Kubernetes
  cluster, Jenkins instance and SSH server, checked in parallel in the
  background so one dead host never stalls the page. Recent runs and upcoming
  scheduled pipelines.
- **Docker** — containers with live CPU / memory / network stats, health and
  uptime, filter by name/state; start/stop/restart/remove, live log tail,
  one-shot exec; images (pull/remove), networks, volumes, and multiple named
  Docker hosts (local socket + remote `tcp://` engines).
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
  commands, saved per-server scripts, or open a full interactive terminal in
  the browser (xterm.js over a WebSocket).
- **Pipelines** — ordered steps (git pull, docker build/push, swarm stack
  deploy, k8s apply, k8s rollout restart, Jenkins trigger, SSH exec / saved
  script, local shell) with:
  - live streaming output, cancel and re-run,
  - **variables** (`${NAME}` in any field, per-run overrides, steps exporting
    values for later steps),
  - **cron schedules** and **webhook triggers** (GitHub, GitLab,
    Gitea/Forgejo, Bitbucket, or plain `curl`) with branch filters,
  - per-step names, "continue on error", step editing, duplicate pipeline.
- **Deploy Center** — one-click pipeline runs for developer accounts scoped via PipelineAccess
- **Ansible** — run playbooks against registered SSH servers and groups
- **Settings** — export/import the whole configuration as JSON (optionally
  with credentials, encrypted by a passphrase), system info.
- **Quality of life** — `Ctrl+K` command palette to jump to any page, host,
  cluster, server, repo or pipeline; toast notifications for every action
  (including the errors that used to fail silently); mobile-friendly layout.

All credentials (SSH keys/passwords, Jenkins tokens, Git PATs) are encrypted
at rest with a locally-generated Fernet key; the cockpit itself sits behind a
login screen created on first run.

## Quick start

```bash
./cockpit init      # creates .env with random secrets, picks Docker or Python mode
./cockpit start     # builds/starts it and waits until it's healthy
```

Open http://localhost:8000 and create the first admin account.

`init` uses **Docker mode** (docker compose) when Docker is available and
**local mode** (a Python virtualenv in `.venv`) otherwise. Force one with
`./cockpit init --mode docker` or `--mode local`.

- Docker mode mounts `/var/run/docker.sock` so the cockpit can manage the
  host's Docker/Swarm daemon — remove that volume in `docker-compose.yml` if
  you don't want that. The image already contains `git`, `ssh`, `docker` and
  `kubectl`.
- Local mode needs Python 3.11+. Install the `docker` CLI for Swarm stack
  deploys and `kubectl` for "Kubernetes apply".

## Managing the cockpit

Everything goes through the `./cockpit` script, in either mode:

| Command | What it does |
|---|---|
| `./cockpit status` | Mode, process, health check, data size, latest backup |
| `./cockpit logs [-f] [-n N]` | Show or follow the server log |
| `./cockpit start` / `stop` / `restart` | Control the service |
| `./cockpit update` | Backup → `git pull` → rebuild/reinstall → restart → health check |
| `./cockpit backup [--no-repos]` | Consistent snapshot of DB, encryption key, kubeconfigs, repos and `.env` into `backups/` |
| `./cockpit restore FILE` | Restore a backup (takes a safety backup of the current state first) |
| `./cockpit users` / `reset-password USER` / `create-admin USER` | Account recovery |
| `./cockpit doctor` | Checks secrets, permissions, dependencies, backups and the running instance |

**Upgrades are safe to run any time:** the database schema upgrades itself on
startup (new columns are added automatically; nothing is dropped), and
`update` takes a backup first.

**Backups contain secrets** (the encryption key and `.env`), so the
`backups/` directory is created `0700` and is gitignored. Old backups beyond
`COCKPIT_BACKUP_KEEP` (default 10) are pruned. To back up nightly:

```cron
30 2 * * * cd /path/to/DevOps-ToolKit && ./cockpit backup --no-repos >/dev/null
```

A backup taken in one mode can be restored in the other (paths are re-pointed
at startup).

## Pipelines

### Variables

Set `KEY=value` lines in a pipeline's settings, or pass overrides for a single
run with **Run with variables…**. Use them as `${KEY}` in any step field
(commands, image tags, compose/manifest text, Jenkins parameters, saved SSH
scripts). Local shell steps also receive them as environment variables.

Built in: `COCKPIT_RUN_ID`, `COCKPIT_PIPELINE`, `COCKPIT_TRIGGER`,
`COCKPIT_DATE`, `COCKPIT_TIMESTAMP`, `COCKPIT_REPOS_DIR`. A `git pull` step
sets `GIT_COMMIT` / `GIT_SHORT_SHA`.

A shell or SSH step can hand a value to later steps by printing:

```bash
echo "::set IMAGE_TAG=$(git rev-parse --short HEAD)"
```

### Schedules

Enable **Run on a schedule** with a cron expression such as `0 3 * * *` or
`*/15 * * * 1-5`. Schedules use the server's timezone (`TZ` in `.env`, set
automatically by `./cockpit init`). A scheduled run is skipped if the
previous run of that pipeline is still going.

### Webhooks

➡️ **Step-by-step guide: [Deploying a branch from GitHub with webhooks](docs/github-webhooks.md)**
(public URL / reverse proxy, GitHub settings, branch filters, one pipeline per
environment, release tags, deploy-after-CI with GitHub Actions, troubleshooting).

In short: enable **Allow triggering via webhook** on the pipeline to get a
secret URL, then add it to your git host with content type
`application/json` and push events only:

- **GitHub / Gitea / Forgejo**: Settings → Webhooks → Add webhook
- **GitLab**: Settings → Webhooks → Push events (and Tag push events)
- **Bitbucket**: Repository settings → Webhooks → Repository push

Only push events start runs; other events and branch/tag deletions are
acknowledged and skipped. The branch filter (e.g. `main, release/*, v*`)
matches the pushed branch or tag name. Webhook runs get `GIT_BRANCH`,
`GIT_TAG`, `GIT_COMMIT`, `GIT_SHORT_SHA`, `GIT_REPO_URL` and `GIT_PUSHER`; set
a **Git pull** step's branch to `${GIT_BRANCH}` to deploy whichever branch was
pushed. You can also trigger by hand:

```bash
curl -X POST https://cockpit.example.com/hooks/pipelines/<token> \
  -H 'Content-Type: application/json' -d '{"variables": {"IMAGE_TAG": "1.4.2"}}'
```

Runs of the same pipeline are queued, never run concurrently. Set
`COCKPIT_PUBLIC_URL` so the UI shows the externally reachable webhook URL.

## Configuration

All configuration is via environment variables in `.env` (see `.env.example`):

| Variable | Purpose |
|---|---|
| `COCKPIT_MODE` | `docker` or `local` — how `./cockpit` runs the app. |
| `COCKPIT_SECRET_KEY` | Signs the session cookie. `./cockpit init` generates one. |
| `COCKPIT_ENCRYPTION_KEY` | Fernet key encrypting stored credentials. Auto-generated into `data/secret.key` if unset. |
| `DOCKER_HOST` | Connection string for the built-in "local" Docker host (default `unix:///var/run/docker.sock`). |
| `TZ` | Timezone for cron schedules, e.g. `Europe/Berlin`. |
| `COCKPIT_PORT` | Host port published in Docker mode (default 8000). |
| `HOST` / `PORT` | Bind address/port of the web server (local mode). |
| `COCKPIT_PUBLIC_URL` | External URL used when displaying webhook URLs. |
| `COCKPIT_SCHEDULER` | `false` disables cron-scheduled runs on this instance. |
| `COCKPIT_BACKUP_KEEP` | Number of backups `./cockpit backup` keeps (default 10). |

Runtime data (SQLite DB, the encryption key, cloned repos, uploaded
kubeconfigs) lives under `data/`, which is gitignored and never copied into
the Docker image.

## Moving to another machine

Either restore a backup (`./cockpit backup` here, `./cockpit restore FILE`
there), or use **Settings → Export configuration** with a passphrase and
import the file on the new cockpit. The export covers hosts, clusters,
Jenkins instances, repos (re-cloned on import), SSH servers + scripts and
pipelines, but not users or run history. Without a passphrase, credentials
are left out and the file is safe to commit.

## Architecture

```
cockpit              management script (init/start/update/backup/restore/...)
app/
  main.py            FastAPI app, lifespan (migrations, scheduler), middleware, routers
  config.py          env-based settings (.env is loaded automatically)
  database.py, models.py, migrations.py   SQLAlchemy (SQLite, WAL) + auto schema upgrades
  security.py        password hashing + at-rest credential encryption
  auth.py, flash.py  session login guard, toast messages
  cli.py             maintenance commands used by ./cockpit (backup, restore, users, ...)
  modules/           one file per integration (docker, k8s, jenkins, git, ssh), plus
                     pipeline_engine (runs, variables, scheduler, webhooks),
                     system_mgr (host metrics) and config_io (export/import)
  routes/            FastAPI routers (HTTP glue over modules/), api_routes for JSON
  templates/, static/  server-rendered UI (Jinja2 + cockpit.css/js, xterm.js from CDN)
```

Each integration in `modules/` degrades gracefully: if a Docker host,
Kubernetes cluster, Jenkins instance, or SSH server is unreachable, the
corresponding page shows an inline error or toast instead of crashing.

`GET /healthz` returns `{"status": "ok", "version": ...}` without login, for
load balancers and uptime checks.

## Security notes

This tool is as powerful as the credentials you give it — Docker socket
access, kubeconfigs, SSH keys, and Jenkins tokens are all real production
capabilities. Treat it like any other admin panel:

- Put it behind your own network boundary (VPN, internal network, or a
  reverse proxy with TLS) — it does not ship TLS itself.
- Run `./cockpit init` (or set `COCKPIT_SECRET_KEY` yourself); the UI shows a
  warning banner while the default key is in use.
- Webhook URLs contain a secret token — anyone who has one can start that
  pipeline. Regenerate it from the pipeline settings if it leaks.
- The pipeline engine's `shell` step and the SSH terminal run arbitrary
  commands by design; only give accounts to people you'd trust with direct
  access to the underlying infrastructure.
