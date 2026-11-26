#!/usr/bin/env python3
"""Generate development-only configuration without reading provider secrets."""
import os
from pathlib import Path
import secrets


def main() -> None:
    target = Path(__file__).resolve().parents[1] / ".env.development"
    password = secrets.token_hex(24)
    content = (
        "# Generated local development configuration. Do not commit.\n"
        "POSTGRES_USER=energy\nPOSTGRES_DB=energy\n"
        f"POSTGRES_PASSWORD={password}\n"
        f"DATABASE_URL=postgresql+psycopg://energy:{password}@db:5432/energy\n"
    )
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Development configuration already exists; preserved unchanged.")
        return
    with os.fdopen(fd, "w") as file:
        file.write(content)
    print("Created .env.development with a random database password.")


if __name__ == "__main__":
    main()
