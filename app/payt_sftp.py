"""Livraison du CSV sur le serveur SFTP de Payt.

Le fichier est déposé sous un nom temporaire (`.part`) puis renommé, pour que
Payt ne relève jamais un fichier partiellement transféré. Payt traite les
fichiers déposés (≈ une fois par jour) : un dépôt réussi ne garantit pas
l'import, à vérifier le lendemain dans l'onglet Import de Payt.
"""

from __future__ import annotations

import contextlib
import io
import logging
import posixpath

import paramiko

logger = logging.getLogger(__name__)


class PaytUploadError(RuntimeError):
    """La livraison SFTP n'a pas abouti."""


def upload_csv(
    csv_text: str,
    filename: str,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    remote_dir: str = ".",
) -> str:
    """Dépose le CSV sur le SFTP de Payt. Lève PaytUploadError en cas d'échec."""
    payload = csv_text.encode("utf-8")
    remote_path = posixpath.join(remote_dir, filename)
    temp_path = f"{remote_path}.part"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=30,
            allow_agent=False,
            look_for_keys=False,
        )
        with client.open_sftp() as sftp:
            sftp.putfo(io.BytesIO(payload), temp_path, confirm=True)
            # Le fichier du jour n'existe pas encore dans le cas nominal.
            with contextlib.suppress(OSError):
                sftp.remove(remote_path)
            sftp.rename(temp_path, remote_path)
    except Exception as exc:  # noqa: BLE001 - on remonte une erreur lisible
        logger.exception("Livraison SFTP Payt en échec")
        raise PaytUploadError(f"Livraison SFTP impossible : {exc}") from exc
    finally:
        client.close()

    logger.info("CSV livré sur le SFTP de Payt : %s (%d octets)", remote_path, len(payload))
    return remote_path
