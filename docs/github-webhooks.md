# Deploying a branch from GitHub with webhooks

This guide sets up "push to a branch on GitHub → DevOps Cockpit pipeline runs".
The same steps work for GitLab, Gitea/Forgejo and Bitbucket; only the
screens in step 4 differ.

```
 git push origin main
        │
        ▼
 GitHub ──POST /hooks/pipelines/<token>──▶ Cockpit
                                            │  event = push? branch matches filter?
                                            ├─ no  → 200 {"skipped": "..."}
                                            └─ yes → 200 {"run_id": 42}
                                                     └─ pipeline runs with GIT_BRANCH,
                                                        GIT_COMMIT, GIT_SHORT_SHA, ...
```

Contents:

1. [Make the cockpit reachable from GitHub](#1-make-the-cockpit-reachable-from-github)
2. [Register the repository](#2-register-the-repository)
3. [Build the pipeline for your branch](#3-build-the-pipeline-for-your-branch)
4. [Enable the webhook and add it on GitHub](#4-enable-the-webhook-and-add-it-on-github)
5. [Test it](#5-test-it)
- [Branch filter reference](#branch-filter-reference)
- [Common setups](#common-setups) (one pipeline per environment, release tags, deploy only after CI passes)
- [Variables you get from a push](#variables-you-get-from-a-push)
- [Troubleshooting](#troubleshooting)
- [Security checklist](#security-checklist)

---

## 1. Make the cockpit reachable from GitHub

GitHub sends webhooks from the internet, so it has to be able to reach
`https://<your-cockpit>/hooks/...`. `http://localhost:8000` will not work.

The recommended setup is to expose **only the `/hooks/` path** publicly
through a reverse proxy with TLS, and keep the rest of the UI on your VPN or
internal network.

**Caddy** (automatic HTTPS):

```caddyfile
cockpit.example.com {
    # Only webhook endpoints are public.
    handle /hooks/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle {
        respond "Not found" 404
    }
}
```

**nginx**:

```nginx
server {
    listen 443 ssl;
    server_name cockpit.example.com;
    # ssl_certificate ... / ssl_certificate_key ...

    location /hooks/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location / {
        return 404;
    }
}
```

Then tell the cockpit its public address so the pipeline page shows the
correct URL to paste into GitHub. In `.env`:

```bash
COCKPIT_PUBLIC_URL=https://cockpit.example.com
```

and restart (`./cockpit restart`).

> **Just testing?** `cloudflared tunnel --url http://localhost:8000` (or
> `ngrok http 8000`) gives you a temporary public HTTPS URL. This exposes the
> *whole* UI, login page included, so shut it down when you're done.

If you firewall the server, GitHub's webhook source IPs are listed under
`"hooks"` in <https://api.github.com/meta>.

## 2. Register the repository

In the cockpit, open **Git Repos** and clone the repository:

| Field | Value |
|---|---|
| Name | e.g. `myapp` (used in pipeline steps and in `${COCKPIT_REPOS_DIR}/myapp`) |
| URL | `https://github.com/you/myapp.git` |
| Branch | the branch you deploy most, e.g. `main` |
| Username / Token | only for private repos (see below) |

For a **private repository**, create a fine-grained personal access token on
GitHub under **Settings → Developer settings → Personal access tokens → Fine-grained tokens**:

- *Repository access*: Only select repositories → `myapp`
- *Permissions → Contents*: **Read-only** (or Read and write if you also
  commit/push from the cockpit)

Paste it into the Token field and leave Username empty. Tokens are stored
encrypted.

> SSH remotes (`git@github.com:...`) also work if the cockpit's environment
> has a deploy key configured, but an HTTPS token is simpler.

## 3. Build the pipeline for your branch

Open **Pipelines**, create one (e.g. `Deploy myapp (main)`), and add steps.
A typical branch deployment:

| # | Step type | Settings |
|---|---|---|
| 1 | **Git pull** | Repo: `myapp` · Branch: `${GIT_BRANCH}` |
| 2 | **Docker build** | Repo: `myapp` · Tag: `myapp:${GIT_SHORT_SHA}` |
| 3 | **Local shell command** | `docker tag myapp:${GIT_SHORT_SHA} myapp:latest` |
| 4 | Deploy: **Swarm stack deploy**, **Kubernetes apply**, **SSH run saved script**, … | use `${GIT_SHORT_SHA}` for the image tag |

About the **Branch** field on the Git pull step:

- `${GIT_BRANCH}` checks out and pulls whichever branch the push was for.
  That's needed when one pipeline serves several branches (e.g.
  `release/*`). On a manual **Run now** there is no push, so it falls back to
  the clone's current branch.
- A fixed name (`main`) always deploys that branch.
- Leave it empty to pull whatever the clone currently has checked out.

The Git pull step also sets `GIT_COMMIT` / `GIT_SHORT_SHA` to the commit that
was actually checked out, so later steps tag the right image even on manual
runs.

## 4. Enable the webhook and add it on GitHub

### In the cockpit

On the pipeline page, scroll to **Pipeline settings → Webhook trigger**:

1. Tick **Allow triggering via webhook**.
2. **Only for branches**: e.g. `main` (see the [filter reference](#branch-filter-reference)).
3. **Save settings**, then **Copy** the webhook URL. It looks like
   `https://cockpit.example.com/hooks/pipelines/3x4mpl3T0k3n...`

### On GitHub

Go to your repository → **Settings → Webhooks → Add webhook** and fill in:

| GitHub field | Value |
|---|---|
| **Payload URL** | the URL you copied |
| **Content type** | `application/json` |
| **Secret** | leave empty. The random token in the URL is the secret. |
| **SSL verification** | Enable |
| **Which events would you like to trigger this webhook?** | **Just the push event** |
| **Active** | ✓ |

Click **Add webhook**. GitHub immediately sends a *ping*, and the cockpit
answers `{"ok": true, "pong": true, "pipeline": "Deploy myapp (main)"}`, so the
webhook shows a green check.

> If you choose "Send me everything", that's fine: the cockpit ignores every
> event except `push` (pull requests, issues, stars, …) and responds with
> `skipped`. Deleting a branch or tag sends a push event too, and the
> cockpit skips it.

## 5. Test it

```bash
git commit --allow-empty -m "test cockpit deploy"
git push origin main
```

- **In GitHub**, open Settings → Webhooks → your hook → **Recent Deliveries**.
  The latest delivery should be `200` with a response like
  `{"ok": true, "run_id": 12, "pipeline": "Deploy myapp (main)"}`.
  **Redeliver** re-sends any delivery, which is handy while you adjust the
  pipeline.
- **In the cockpit**, the run appears on the pipeline page and dashboard with
  trigger `webhook · push · main · 1a2b3c4d · by you`.

Pushing to a branch that doesn't match the filter returns `200` with
`{"skipped": "'feature/x' does not match 'main'"}` and starts nothing.

**Without pushing**, you can simulate GitHub with curl:

```bash
curl -X POST "https://cockpit.example.com/hooks/pipelines/<token>" \
  -H 'X-GitHub-Event: push' \
  -H 'Content-Type: application/json' \
  -d '{"ref": "refs/heads/main",
       "after": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
       "pusher": {"name": "me"}}'
```

---

## Branch filter reference

**Only for branches** takes comma-separated glob patterns matched against the
pushed **branch name, or tag name** for tag pushes. An empty filter accepts
every push.

| Filter | Runs for | Skips |
|---|---|---|
| *(empty)* | every branch and tag push | nothing |
| `main` | `main` | everything else, including tags |
| `main, develop` | `main`, `develop` | `feature/x`, tags |
| `release/*` | `release/1.2`, `release/2.0-rc` | `main` |
| `main, v*` | `main` and tags like `v1.4.0` | other branches, tags like `nightly` |
| `v[0-9]*` | release tags `v1.0`, `v2.3.1` | `main`, tag `vNext` |
| `hotfix/*, release/*` | both families | `main` |

`*` matches anything, including `/` (so `release/*` also matches
`release/1.2/fix`); `?` matches one character; `[0-9]` a character class.
Triggers without a branch (a plain `curl -X POST`) are never filtered out.

## Common setups

### One pipeline per environment

Each pipeline has its own webhook URL, and GitHub allows many webhooks per
repository. Add one GitHub webhook per pipeline:

| Pipeline | Branch filter | Git pull branch | Deploys to |
|---|---|---|---|
| `myapp → staging` | `develop` | `develop` | staging stack / namespace |
| `myapp → production` | `main` | `main` | production |
| `myapp → preview` | `feature/*` | `${GIT_BRANCH}` | `preview-${GIT_BRANCH}` |

Use pipeline **Variables** (e.g. `DEPLOY_ENV=staging`, `NAMESPACE=staging`)
so the steps can be identical across pipelines: build one, **Duplicate** it,
change the variables, branch filter and webhook.

### Release on tags

Filter `v*`, and in the steps use `${GIT_TAG}`:

- Docker build tag: `myapp:${GIT_TAG}`
- Git pull branch: `${GIT_TAG}` (checks out the tag)

Then `git tag v1.4.0 && git push origin v1.4.0` ships it.

### Deploy only after CI passes (GitHub Actions)

A webhook fires on every push, even if your tests fail. To deploy only
green builds, skip the repository webhook and call the cockpit from the
last job of your workflow instead:

1. Copy the pipeline's webhook URL into a repository secret:
   **Settings → Secrets and variables → Actions → New repository secret**,
   name `COCKPIT_WEBHOOK_URL`.
2. Add a deploy job:

```yaml
# .github/workflows/deploy.yml
name: test-and-deploy
on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test

  deploy:
    needs: test                 # only runs if tests passed
    runs-on: ubuntu-latest
    steps:
      - name: Trigger DevOps Cockpit pipeline
        env:
          COCKPIT_WEBHOOK_URL: ${{ secrets.COCKPIT_WEBHOOK_URL }}
        run: |
          curl -fsS -X POST "$COCKPIT_WEBHOOK_URL" \
            -H 'Content-Type: application/json' \
            -d "{\"ref\": \"$GITHUB_REF\", \"after\": \"$GITHUB_SHA\",
                 \"variables\": {\"GITHUB_RUN_ID\": \"$GITHUB_RUN_ID\"}}"
```

The branch filter and `GIT_BRANCH` / `GIT_COMMIT` variables work exactly as
with a repository webhook, and anything under `variables` becomes a pipeline
variable (`${GITHUB_RUN_ID}` here).

## Variables you get from a push

| Variable | Example | Notes |
|---|---|---|
| `GIT_BRANCH` | `main` | branch pushes only |
| `GIT_TAG` | `v1.4.0` | tag pushes only |
| `GIT_REF` | `refs/heads/main` | full ref |
| `GIT_COMMIT` | `1a2b3c4d5e6f…` | pushed commit (a Git pull step updates it to what it checked out) |
| `GIT_SHORT_SHA` | `1a2b3c4d` | first 8 characters |
| `GIT_REPO_URL` | `https://github.com/you/myapp.git` | |
| `GIT_PUSHER` | `you` | |
| `COCKPIT_TRIGGER` | `webhook` | `manual` / `schedule` for other runs |

Plus your pipeline variables and the built-ins (`COCKPIT_RUN_ID`,
`COCKPIT_TIMESTAMP`, …). Use them as `${NAME}` in any step field; shell
steps also see them as environment variables.

If several pushes arrive while a run is in progress, the extra runs wait as
**queued** and execute one after another, never in parallel on the same
checkout.

## Troubleshooting

Check **Recent Deliveries** on GitHub first: it shows the exact response.

| What you see | Cause | Fix |
|---|---|---|
| *We couldn't deliver this payload: failed to connect* / timed out | GitHub can't reach the server | public DNS, firewall, reverse proxy ([step 1](#1-make-the-cockpit-reachable-from-github)) |
| `404 {"error": "unknown webhook"}` | webhook disabled on the pipeline, URL regenerated, or a typo | copy the URL again from the pipeline settings |
| `404 Not found` from your proxy | proxy doesn't forward `/hooks/` | check the `location`/`handle` path |
| `301`/`302` redirect, or a login page | URL uses `http://` behind an HTTPS redirect, or an auth layer protects `/hooks/` | use the `https://` URL; exempt `/hooks/` from proxy auth |
| `200 {"skipped": "'x' does not match 'main'"}` | branch filter | adjust **Only for branches** |
| `200 {"skipped": "ignored 'pull_request' event…"}` | non-push event | select "Just the push event" (harmless otherwise) |
| `200 {"skipped": "'…' was deleted"}` | branch/tag deletion | expected |
| Run starts but **Git pull** fails with an auth error | private repo without token, or token expired | edit the repo's token in **Git Repos** |
| Git pull fails with `pathspec … did not match` | branch doesn't exist on the remote | check the Branch field / push the branch |
| Webhook URL in the UI shows `localhost` | `COCKPIT_PUBLIC_URL` not set | set it in `.env`, `./cockpit restart` |

Server-side details are in `./cockpit logs -f`.

## Security checklist

- The webhook URL **is** the credential: anyone who has it can start that
  pipeline (but nothing else). Don't paste it into public issues or commit it
  to a repo. Put it in GitHub secrets if you call it from Actions.
- If it leaks, open the pipeline settings → **Webhook examples → Regenerate
  URL**, then update GitHub. The old URL stops working immediately.
- Expose only `/hooks/` publicly; keep the UI behind a VPN or internal network.
- Always use HTTPS so the token in the URL isn't sent in clear text.
- A pipeline that runs shell or SSH steps from a webhook runs code from
  whatever was pushed. Restrict who can push to the deploy branch with
  GitHub **branch protection rules**.
