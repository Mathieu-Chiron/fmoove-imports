"""Configuration : réglages globaux + registre des clients (catch-all).

Une seule boîte mail (catch-all) reçoit tous les alias `client@mondomaine`.
L'app la relève et route chaque email vers le bon client selon l'**adresse
destinataire** (l'alias). Chaque client a sa propre administration Payt.
Le registre est fourni via `TENANTS_JSON` (tableau JSON), stocké en secret.
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
    """Un client : son adresse de réception (alias) et sa configuration Payt."""

    name: str
    inbox_address: str  # l'alias, sert au routage (comparé au « To: »)
    administration_code: str
    api_token: str
    import_token: str
    allowed_senders: set[str] = field(default_factory=set)
    from_addr: str = ""  # optionnel : « send-as » l'alias ; sinon MAILBOX_FROM

    @classmethod
    def from_dict(cls, data: dict) -> Tenant:
        inbox = str(data.get("inbox_address", "")).strip().lower()
        return cls(
            name=str(data.get("name") or inbox or "?").strip(),
            inbox_address=inbox,
            administration_code=str(data.get("administration_code", "")).strip(),
            api_token=str(data.get("api_token", "")),
            import_token=str(data.get("import_token", "")),
            allowed_senders={
                str(s).strip().lower()
                for s in data.get("allowed_senders", [])
                if str(s).strip()
            },
            from_addr=str(data.get("from_addr", "")).strip(),
        )

    def issues(self) -> list[str]:
        required = (
            "inbox_address", "administration_code", "api_token", "import_token",
        )
        missing = [name for name in required if not getattr(self, name)]
        if not self.allowed_senders:
            missing.append("allowed_senders")
        return [f"{self.name}: {name}" for name in missing]


@dataclass
class Settings:
    # Global
    import_url: str = "https://backend.paytsoftware.com/import/files/csv"
    filename_pattern: str = "%Y%m%d.csv"
    poll_token: str = ""

    # Boîte catch-all (relevée en IMAP, répond en SMTP)
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587
    mailbox_user: str = ""
    mailbox_password: str = ""
    mailbox_from: str = ""  # « De : » par défaut si le client n'a pas de from_addr

    tenants: list[Tenant] = field(default_factory=list)

    # Archivage Object Storage (optionnel)
    s3_endpoint: str = ""
    s3_region: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        mailbox_user = os.environ.get("MAILBOX_USER", "").strip()
        imap_host = os.environ.get("MAILBOX_IMAP_HOST", "").strip()
        return cls(
            import_url=os.environ.get(
                "PAYT_IMPORT_URL", "https://backend.paytsoftware.com/import/files/csv"
            ).strip()
            or "https://backend.paytsoftware.com/import/files/csv",
            filename_pattern=os.environ.get("PAYT_FILENAME_PATTERN", "%Y%m%d.csv").strip()
            or "%Y%m%d.csv",
            poll_token=os.environ.get("POLL_TOKEN", ""),
            imap_host=imap_host,
            imap_port=_int("MAILBOX_IMAP_PORT", 993),
            smtp_host=os.environ.get("MAILBOX_SMTP_HOST", imap_host).strip(),
            smtp_port=_int("MAILBOX_SMTP_PORT", 587),
            mailbox_user=mailbox_user,
            mailbox_password=os.environ.get("MAILBOX_PASSWORD", ""),
            mailbox_from=os.environ.get("MAILBOX_FROM", mailbox_user).strip(),
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

    def tenant_for(self, recipients: set[str]) -> Tenant | None:
        """Le client dont l'alias figure parmi les destinataires de l'email."""
        recipients = {r.lower() for r in recipients}
        for tenant in self.tenants:
            if tenant.inbox_address in recipients:
                return tenant
        return None

    @property
    def mailbox_configured(self) -> bool:
        return bool(self.imap_host and self.mailbox_user and self.mailbox_password)

    def check(self) -> list[str]:
        problems: list[str] = []
        if not self.poll_token:
            problems.append("POLL_TOKEN")
        if not self.mailbox_configured:
            problems.append("MAILBOX (IMAP host/user/password)")
        if not self.tenants:
            problems.append("TENANTS_JSON (aucun client)")
        # Deux clients ne peuvent pas partager la même adresse de réception.
        seen: set[str] = set()
        for tenant in self.tenants:
            problems += tenant.issues()
            if tenant.inbox_address and tenant.inbox_address in seen:
                problems.append(f"{tenant.name}: inbox_address en doublon")
            seen.add(tenant.inbox_address)
        return problems

    @property
    def inbound_enabled(self) -> bool:
        return bool(self.poll_token and self.tenants and self.mailbox_configured)

    @property
    def archive_enabled(self) -> bool:
        return bool(self.s3_bucket and self.s3_access_key and self.s3_secret_key)


settings = Settings.from_env()
