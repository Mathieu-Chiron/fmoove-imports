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

    # Payt — livraison sur le SFTP de Payt + code administration
    administration_code: str = ""
    payt_sftp_host: str = ""
    payt_sftp_port: int = 22
    payt_sftp_user: str = ""
    payt_sftp_password: str = ""
    payt_sftp_dir: str = "."  # dossier de dépôt sur le SFTP de Payt
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

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_user=os.environ.get("APP_USER", "fairmoove").strip(),
            app_password=os.environ.get("APP_PASSWORD", ""),
            administration_code=os.environ.get("PAYT_ADMINISTRATION_CODE", "").strip(),
            payt_sftp_host=os.environ.get("PAYT_SFTP_HOST", "").strip(),
            payt_sftp_port=_int("PAYT_SFTP_PORT", 22),
            payt_sftp_user=os.environ.get("PAYT_SFTP_USER", "").strip(),
            payt_sftp_password=os.environ.get("PAYT_SFTP_PASSWORD", ""),
            payt_sftp_dir=os.environ.get("PAYT_SFTP_DIR", ".").strip() or ".",
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
        )

    # Variables sans lesquelles un envoi ne peut pas aboutir.
    _REQUIRED = {
        "APP_PASSWORD": "app_password",
        "PAYT_ADMINISTRATION_CODE": "administration_code",
        "PAYT_SFTP_HOST": "payt_sftp_host",
        "PAYT_SFTP_USER": "payt_sftp_user",
        "PAYT_SFTP_PASSWORD": "payt_sftp_password",
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


settings = Settings.from_env()
