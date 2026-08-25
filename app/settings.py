"""Configuration : réglages globaux + registre des clients (multi-tenant).

Chaque client a sa propre boîte mail (IMAP + SMTP) et sa propre administration
Payt (code + tokens). Le registre est fourni via la variable `TENANTS_JSON`
(un tableau JSON), stockée en secret car elle contient mots de passe et tokens.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass
class Tenant:
    """Un client : sa boîte mail et sa configuration Payt."""

    name: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    mailbox_user: str
    mailbox_password: str
    from_addr: str
    administration_code: str
    api_token: str
    import_token: str
    allowed_senders: set[str] = field(default_factory=set)

    @classmethod
    def from_dict(cls, data: dict) -> Tenant:
        user = str(data.get("mailbox_user", "")).strip()
        imap_host = str(data.get("imap_host", "")).strip()
        return cls(
            name=str(data.get("name") or user or "?").strip(),
            imap_host=imap_host,
            imap_port=int(data.get("imap_port") or 993),
            smtp_host=str(data.get("smtp_host") or imap_host).strip(),
            smtp_port=int(data.get("smtp_port") or 587),
            mailbox_user=user,
            mailbox_password=str(data.get("mailbox_password", "")),
            from_addr=str(data.get("from_addr") or user).strip(),
            administration_code=str(data.get("administration_code", "")).strip(),
            api_token=str(data.get("api_token", "")),
            import_token=str(data.get("import_token", "")),
            allowed_senders={
                str(s).strip().lower()
                for s in data.get("allowed_senders", [])
                if str(s).strip()
            },
        )

    def issues(self) -> list[str]:
        """Champs manquants pour ce client (préfixés par son nom)."""
        required = (
            "imap_host", "mailbox_user", "mailbox_password",
            "administration_code", "api_token", "import_token",
        )
        missing = [name for name in required if not getattr(self, name)]
        if not self.allowed_senders:
            missing.append("allowed_senders")
        return [f"{self.name}: {name}" for name in missing]


@dataclass
class Settings:
    # Global — partagé par tous les clients
    import_url: str = "https://backend.paytsoftware.com/import/files/csv"
    filename_pattern: str = "%Y%m%d.csv"
    poll_token: str = ""  # protège l'endpoint déclenché par le cron
    tenants: list[Tenant] = field(default_factory=list)

    # Archivage Object Storage (optionnel)
    s3_endpoint: str = ""
    s3_region: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            import_url=os.environ.get(
                "PAYT_IMPORT_URL", "https://backend.paytsoftware.com/import/files/csv"
            ).strip()
            or "https://backend.paytsoftware.com/import/files/csv",
            filename_pattern=os.environ.get("PAYT_FILENAME_PATTERN", "%Y%m%d.csv").strip()
            or "%Y%m%d.csv",
            poll_token=os.environ.get("POLL_TOKEN", ""),
            tenants=cls._parse_tenants(os.environ.get("TENANTS_JSON", "")),
            s3_endpoint=os.environ.get("S3_ENDPOINT", "").strip(),
            s3_region=os.environ.get("S3_REGION", "").strip(),
            s3_bucket=os.environ.get("S3_BUCKET", "").strip(),
            s3_access_key=os.environ.get("S3_ACCESS_KEY", ""),
            s3_secret_key=os.environ.get("S3_SECRET_KEY", ""),
        )

    @staticmethod
    def _parse_tenants(raw: str) -> list[Tenant]:
        raw = raw.strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("TENANTS_JSON invalide : JSON non analysable, registre vide")
            return []
        if not isinstance(data, list):
            logger.error("TENANTS_JSON doit être un tableau JSON")
            return []
        return [Tenant.from_dict(d) for d in data]

    def check(self) -> list[str]:
        """Problèmes de configuration, pour /health."""
        problems: list[str] = []
        if not self.poll_token:
            problems.append("POLL_TOKEN")
        if not self.tenants:
            problems.append("TENANTS_JSON (aucun client)")
        for tenant in self.tenants:
            problems += tenant.issues()
        return problems

    @property
    def inbound_enabled(self) -> bool:
        return bool(self.poll_token and self.tenants)

    @property
    def archive_enabled(self) -> bool:
        return bool(self.s3_bucket and self.s3_access_key and self.s3_secret_key)


settings = Settings.from_env()
