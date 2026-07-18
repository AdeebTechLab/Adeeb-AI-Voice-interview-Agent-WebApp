from __future__ import annotations
import argparse
import secrets
from pathlib import Path
from cryptography.fernet import Fernet


def update_env(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    values = {
        "IDENTITY_ENCRYPTION_KEY": Fernet.generate_key().decode("utf-8"),
        "CNIC_HMAC_SECRET": secrets.token_urlsafe(48),
    }
    output = []
    seen = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate local encryption and CNIC HMAC secrets in .env.")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    update_env(Path(args.env))
    print("Identity encryption and CNIC HMAC secrets saved. Keep .env private and back it up securely.")
