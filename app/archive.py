"""Archivage des envois dans Object Storage (S3), en best-effort.

Rien ici ne doit interrompre un envoi : Payt reste la source de vérité. Si
l'archivage n'est pas configuré ou échoue, on journalise et on continue. Le
compteur de lignes du dernier envoi sert uniquement à détecter une chute de
volume (avertissement, jamais bloquant).
"""

from __future__ import annotations

import logging

from app.settings import settings

logger = logging.getLogger(__name__)

_STATE_KEY = "state/last_row_count.txt"


def _client():
    """Client S3 ou None si l'archivage n'est pas configuré / boto3 absent."""
    if not settings.archive_enabled:
        return None
    try:
        import boto3
    except ImportError:  # pragma: no cover - boto3 est une dépendance runtime
        logger.warning("boto3 absent : archivage désactivé")
        return None
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint or None,
        region_name=settings.s3_region or None,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
    )


def read_last_row_count() -> int | None:
    """Nombre de lignes du dernier envoi archivé, ou None si indisponible."""
    client = _client()
    if client is None:
        return None
    try:
        obj = client.get_object(Bucket=settings.s3_bucket, Key=_STATE_KEY)
        return int(obj["Body"].read().decode().strip())
    except Exception:  # noqa: BLE001 - best-effort, l'absence est normale au 1er envoi
        logger.info("Compteur du dernier envoi indisponible")
        return None


def save_run(
    stamp: str,
    csv_text: str,
    sources: dict[str, bytes],
    row_count: int,
) -> None:
    """Archive le CSV, les fichiers sources et met à jour le compteur de lignes."""
    client = _client()
    if client is None:
        logger.info("Archivage non configuré : run %s non archivé", stamp)
        return
    bucket = settings.s3_bucket
    prefix = f"runs/{stamp}"
    try:
        client.put_object(
            Bucket=bucket,
            Key=f"{prefix}/export.csv",
            Body=csv_text.encode("utf-8"),
            ContentType="text/csv; charset=utf-8",
        )
        for name, content in sources.items():
            client.put_object(Bucket=bucket, Key=f"{prefix}/sources/{name}", Body=content)
        client.put_object(
            Bucket=bucket, Key=_STATE_KEY, Body=str(row_count).encode()
        )
        logger.info("Run %s archivé (%d lignes)", stamp, row_count)
    except Exception:  # noqa: BLE001 - l'archivage ne doit jamais casser un envoi
        logger.exception("Archivage du run %s en échec", stamp)
