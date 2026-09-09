"""Generate deployment secrets only on explicit invocation; never print values."""
from pathlib import Path
import secrets
import os

from cryptography.fernet import Fernet


def main():
    deploy_root = Path(__file__).resolve().parent
    directory = deploy_root / "secrets"
    if directory.is_symlink() or not directory.resolve().is_relative_to(deploy_root):
        raise ValueError("Deployment secrets must use this deployment directory, without links.")
    directory.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        directory.chmod(0o700)
    values = {"django-secret": secrets.token_urlsafe(64), "postgres-password": secrets.token_urlsafe(48),
              "otp-key": Fernet.generate_key().decode("ascii")}
    # Existing files are immutable here: running again cannot invalidate MFA.
    for name, value in values.items():
        path = directory / name
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(value + "\n")
        # Compose file-backed secrets are bind mounts: app UID 1000 and the
        # PostgreSQL UID both need read access. Host traversal is blocked by the
        # 0700 directory; the files are mounted only into their named services.
        if os.name != "nt":
            path.chmod(0o444)
    print("Deployment secret files exist. Back them up separately in a protected secret vault.")


if __name__ == "__main__":
    main()
