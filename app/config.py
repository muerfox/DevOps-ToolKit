import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPOS_DIR = DATA_DIR / "repos"
KUBECONFIGS_DIR = DATA_DIR / "kubeconfigs"

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

    app_name: str = "DevOps Cockpit"


settings = Settings()
