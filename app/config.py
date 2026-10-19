import os
import time
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPOS_DIR = DATA_DIR / "repos"
KUBECONFIGS_DIR = DATA_DIR / "kubeconfigs"

APP_VERSION = "1.1.0"

# Real environment variables (e.g. from docker compose) win over .env.
load_dotenv(BASE_DIR / ".env", override=False)

# An empty TZ (e.g. a blank `TZ=` line in .env) means UTC to libc; treat it as unset.
if os.environ.get("TZ") == "":
    del os.environ["TZ"]
if hasattr(time, "tzset"):
    time.tzset()

for _d in (DATA_DIR, REPOS_DIR, KUBECONFIGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


class Settings:
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"

    secret_key: str = os.getenv("COCKPIT_SECRET_KEY", "dev-insecure-secret-change-me")
    encryption_key_env: str | None = os.getenv("COCKPIT_ENCRYPTION_KEY") or None

    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'cockpit.db'}")

    default_docker_host: str = os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock")

    # Public base URL used when displaying webhook URLs (e.g. https://cockpit.example.com).
    # Falls back to the URL the browser used to reach the cockpit.
    public_url: str = os.getenv("COCKPIT_PUBLIC_URL", "").rstrip("/")

    scheduler_enabled: bool = os.getenv("COCKPIT_SCHEDULER", "true").lower() != "false"

    app_name: str = "DevOps Cockpit"
    version: str = APP_VERSION


settings = Settings()

INSECURE_SECRET_KEYS = {"dev-insecure-secret-change-me", "change-me-to-a-random-secret", ""}
