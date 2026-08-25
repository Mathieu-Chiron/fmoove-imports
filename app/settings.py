"""Configuration de l'application, lue depuis l'environnement.

Aucun secret n'est écrit en dur : tout vient de variables d'environnement
(voir `.env.example`). `check()` renvoie la liste des variables critiques encore
manquantes, ce qui permet à `/health` et au formulaire de signaler une
configuration incomplète avant tout envoi.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass
class Settings:
    # Accès à l'application (HTTP Basic)
    app_user: str = ""
    app_password: str = ""

    # Payt — API d'import + code administration
    administration_code: str = ""
    import_url: str = "https://backend.paytsoftware.com/import/files/csv"
    api_token: str = ""  # token statique, en-tête Authorization
    import_token: str = ""  # token d'importation, corps de la requête
    filename_pattern: str = "%Y%m%d.csv"

    # Archivage Object Storage (optionnel)
    s3_endpoint: str = ""
    s3_region: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""

    # Alerte mail (optionnel)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_from: str = ""
    alert_to: str = ""

    # Réception par email : boîte IMAP relevée par cron
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    inbound_allowlist: str = ""  # expéditeurs autorisés, séparés par des virgules
    poll_token: str = ""  # jeton protégeant l'endpoint déclenché par le cron

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_user=os.environ.get("APP_USER", "fairmoove").strip(),
            app_password=os.environ.get("APP_PASSWORD", ""),
            administration_code=os.environ.get("PAYT_ADMINISTRATION_CODE", "").strip(),
            import_url=os.environ.get(
                "PAYT_IMPORT_URL", "https://backend.paytsoftware.com/import/files/csv"
            ).strip()
            or "https://backend.paytsoftware.com/import/files/csv",
            api_token=os.environ.get("PAYT_API_TOKEN", ""),
            import_token=os.environ.get("PAYT_IMPORT_TOKEN", ""),
            filename_pattern=os.environ.get("PAYT_FILENAME_PATTERN", "%Y%m%d.csv").strip()
            or "%Y%m%d.csv",
            s3_endpoint=os.environ.get("S3_ENDPOINT", "").strip(),
            s3_region=os.environ.get("S3_REGION", "").strip(),
            s3_bucket=os.environ.get("S3_BUCKET", "").strip(),
            s3_access_key=os.environ.get("S3_ACCESS_KEY", ""),
            s3_secret_key=os.environ.get("S3_SECRET_KEY", ""),
            smtp_host=os.environ.get("SMTP_HOST", "").strip(),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            alert_from=os.environ.get("ALERT_FROM", "").strip(),
            alert_to=os.environ.get("ALERT_TO", "").strip(),
            imap_host=os.environ.get("IMAP_HOST", "").strip(),
            imap_port=_int("IMAP_PORT", 993),
            imap_user=os.environ.get("IMAP_USER", "").strip(),
            imap_password=os.environ.get("IMAP_PASSWORD", ""),
            inbound_allowlist=os.environ.get("INBOUND_ALLOWLIST", ""),
            poll_token=os.environ.get("POLL_TOKEN", ""),
        )

    # Variables sans lesquelles un envoi ne peut pas aboutir.
    _REQUIRED = {
        "APP_PASSWORD": "app_password",
        "PAYT_ADMINISTRATION_CODE": "administration_code",
        "PAYT_API_TOKEN": "api_token",
        "PAYT_IMPORT_TOKEN": "import_token",
    }

    def check(self) -> list[str]:
        """Renvoie les noms des variables critiques encore vides."""
        return [name for name, attr in self._REQUIRED.items() if not getattr(self, attr)]

    @property
    def archive_enabled(self) -> bool:
        return bool(self.s3_bucket and self.s3_access_key and self.s3_secret_key)

    @property
    def alerts_enabled(self) -> bool:
        return bool(self.smtp_host and self.alert_from and self.alert_to)

    @property
    def replies_enabled(self) -> bool:
        """Suffit pour répondre à un expéditeur (pas besoin d'ALERT_TO)."""
        return bool(self.smtp_host and self.alert_from)

    @property
    def inbound_enabled(self) -> bool:
        return bool(
            self.imap_host and self.imap_user and self.imap_password and self.poll_token
        )

    @property
    def allowed_senders(self) -> set[str]:
        return {a.strip().lower() for a in self.inbound_allowlist.split(",") if a.strip()}


settings = Settings.from_env()
