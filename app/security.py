"""Password hashing and at-rest encryption for stored credentials.

Anything sensitive that gets persisted to the database (SSH passwords/private
keys, Jenkins API tokens, git PATs) is passed through encrypt()/decrypt()
before it touches SQLAlchemy. Kubeconfigs are stored as files on disk rather
than in the DB, but the files live under data/ which is gitignored and not
served by the app.
"""

import bcrypt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization

from .config import settings, DATA_DIR

_KEY_FILE = DATA_DIR / "secret.key"


def _load_or_create_key() -> bytes:
    if settings.encryption_key_env:
        return settings.encryption_key_env.encode()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    try:
        _KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


_fernet = Fernet(_load_or_create_key())


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet.decrypt(value.encode()).decode()


def normalize_key_text(key_text: str) -> str:
    """Strip CRLF/CR line endings from private key material before it ever
    reaches a real ssh client.

    A browser normalizes <textarea> content to CRLF on form submission (part
    of the HTML spec) regardless of what was actually pasted in, so any key
    entered through the web UI arrives with '\\r\\n' line endings. paramiko's
    own Python-based key parser tolerates that, but the real OpenSSH client
    (used by Ansible's `ssh` connection plugin and by git's GIT_SSH_COMMAND)
    does not: it fails outright with "Load key ...: error in libcrypto" and
    then falls through to "Permission denied (publickey)", since the key was
    never actually parsed, let alone offered. Called both when a key is
    saved and again right before it's written to a file for one of those
    tools, so already-stored keys are covered too without re-entering them.
    """
    return key_text.replace("\r\n", "\n").replace("\r", "\n")


def strip_ssh_key_passphrase(key_text: str, passphrase: str) -> str:
    """Decrypt a passphrase-protected private key and re-serialize it
    without the passphrase, so it can be handed to a non-interactive ssh
    client (ansible-playbook, git's GIT_SSH_COMMAND) with no prompt.

    Not paramiko's own write_private_key()/write_private_key_file(): those
    are simply unimplemented for Ed25519Key -- the most common modern key
    type -- in the paramiko version this project pins. The `cryptography`
    library (already a dependency) handles every key type OpenSSH does.
    """
    key_obj = serialization.load_ssh_private_key(key_text.encode(), password=passphrase.encode())
    return key_obj.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False
