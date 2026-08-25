"""Archivage des envois dans Object Storage (S3), en best-effort.

Rien ici ne doit interrompre un envoi : Payt reste la source de vérité. Si
l'archivage n'est pas configuré ou échoue, on journalise et on continue. Le
compteur de lignes du dernier envoi sert uniquement à détecter une chute de
volume (avertissement, jamais bloquant).
"""

from __future__ import annotations

import logging
import re

from app.settings import settings

logger = logging.getLogger(__name__)


def _slug(tenant_key: str) -> str:
    """Clé de chemin S3 sûre à partir du nom du client."""
    return re.sub(r"[^a-z0-9._-]+", "-", tenant_key.strip().lower()) or "default"


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


def read_last_row_count(tenant_key: str) -> int | None:
    """Nombre de lignes du dernier envoi archivé de ce client, ou None."""
    client = _client()
    if client is None:
        return None
    try:
        obj = client.get_object(
            Bucket=settings.s3_bucket, Key=f"state/{_slug(tenant_key)}/last_row_count.txt"
        )
        return int(obj["Body"].read().decode().strip())
    except Exception:  # noqa: BLE001 - best-effort, l'absence est normale au 1er envoi
        logger.info("Compteur du dernier envoi indisponible (%s)", tenant_key)
        return None


def save_run(
    tenant_key: str,
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
    slug = _slug(tenant_key)
    prefix = f"runs/{slug}/{stamp}"
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
            Bucket=bucket,
            Key=f"state/{slug}/last_row_count.txt",
            Body=str(row_count).encode(),
        )
        logger.info("Run %s/%s archivé (%d lignes)", slug, stamp, row_count)
    except Exception:  # noqa: BLE001 - l'archivage ne doit jamais casser un envoi
        logger.exception("Archivage du run %s en échec", stamp)
