"""Local identity protection for candidate registration.

CNIC and father/guardian name are sensitive. The app stores a salted HMAC for
uniqueness checks and an encrypted value for HR-only display. This module is
self-hosted; for production set IDENTITY_ENCRYPTION_KEY and CNIC_HMAC_SECRET
in .env and back them up securely.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class IdentityRecord:
    full_name: str
    father_name: str
    cnic_digits: str
    cnic_hash: str
    father_name_encrypted: str
    cnic_encrypted: str


def normalize_person_name(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:120]


def normalize_cnic(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 13:
        raise ValueError("Enter a valid 13-digit CNIC, for example 12345-1234567-1.")
    # Basic Pakistani CNIC structural validation. The last digit is not a
    # universal public checksum, so length/format validation is intentionally used.
    return digits


def format_cnic(digits: str) -> str:
    digits = normalize_cnic(digits)
    return f"{digits[:5]}-{digits[5:12]}-{digits[12:]}"


def mask_cnic(value: str) -> str:
    try:
        digits = normalize_cnic(value)
    except ValueError:
        return "Hidden"
    return f"{digits[:5]}-*******-{digits[-1]}"


class IdentityProtector:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._secrets_path = data_dir / ".identity_secrets.json"
        self._fernet_key, self._hmac_secret, self.using_env_secrets = self._load_or_create_secrets()
        self._fernet = Fernet(self._fernet_key.encode("utf-8"))

    def _load_or_create_secrets(self) -> tuple[str, str, bool]:
        fernet_key = str(os.getenv("IDENTITY_ENCRYPTION_KEY", "")).strip()
        hmac_secret = str(os.getenv("CNIC_HMAC_SECRET", "")).strip()
        if fernet_key and hmac_secret:
            try:
                Fernet(fernet_key.encode("utf-8"))
            except Exception as exc:  # pragma: no cover - startup config error
                raise RuntimeError("IDENTITY_ENCRYPTION_KEY is not a valid Fernet key. Run scripts/generate_security_secrets.py.") from exc
            return fernet_key, hmac_secret, True

        # Local development fallback: generated once, never committed, and only used
        # on this host. Production deployment should set env values and back them up.
        if self._secrets_path.exists():
            try:
                payload = json.loads(self._secrets_path.read_text(encoding="utf-8"))
                key = str(payload["fernet_key"])
                secret = str(payload["cnic_hmac_secret"])
                Fernet(key.encode("utf-8"))
                if secret:
                    return key, secret, False
            except Exception:
                pass
        self._data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "fernet_key": Fernet.generate_key().decode("utf-8"),
            "cnic_hmac_secret": secrets.token_urlsafe(48),
        }
        temp = self._secrets_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        temp.replace(self._secrets_path)
        return payload["fernet_key"], payload["cnic_hmac_secret"], False

    def cnic_hash(self, digits: str) -> str:
        normalized = normalize_cnic(digits)
        return hmac.new(self._hmac_secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(str(value).encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str | None) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError):
            return ""

    def build_record(self, full_name: str, father_name: str, cnic_value: str) -> IdentityRecord:
        full_name = normalize_person_name(full_name)
        father_name = normalize_person_name(father_name)
        if len(full_name) < 2:
            raise ValueError("Enter your full name.")
        if len(father_name) < 2:
            raise ValueError("Enter your father or guardian name.")
        digits = normalize_cnic(cnic_value)
        return IdentityRecord(
            full_name=full_name,
            father_name=father_name,
            cnic_digits=digits,
            cnic_hash=self.cnic_hash(digits),
            father_name_encrypted=self.encrypt(father_name),
            cnic_encrypted=self.encrypt(digits),
        )

    def verify_father_name(self, encrypted: str | None, supplied: str) -> bool:
        saved = normalize_person_name(self.decrypt(encrypted))
        provided = normalize_person_name(supplied)
        return bool(saved and provided and hmac.compare_digest(saved.casefold(), provided.casefold()))

    @staticmethod
    def make_resume_code() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(8))

    @staticmethod
    def resume_code_hash(code: str) -> str:
        return hashlib.sha256(str(code or "").strip().upper().encode("utf-8")).hexdigest()
