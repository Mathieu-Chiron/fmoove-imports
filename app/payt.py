"""Dépôt du CSV sur le serveur SFTP de Payt.

Payt relève les fichiers une fois par jour, généralement vers 01h00. Un dépôt
réussi ne garantit donc pas que l'import s'est bien passé : il faut vérifier
l'onglet Import de l'administration Payt le lendemain.
"""

from __future__ import annotations

import contextlib
import io
import logging
import posixpath

import paramiko

logger = logging.getLogger(__name__)


class PaytUploadError(RuntimeError):
    """Le dépôt n'a pas abouti."""


def upload_csv(
    csv_text: str,
    filename: str,
    host: str,
    username: str,
    password: str,
    port: int = 22,
    remote_dir: str = "/",
) -> str:
    """Dépose le CSV et renvoie le chemin distant. Lève PaytUploadError sinon.

    Le fichier est d'abord écrit sous un nom temporaire puis renommé : Payt ne
    doit jamais tomber sur un fichier partiellement transféré s'il relève
    pendant l'écriture.
    """
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
        logger.exception("Dépôt SFTP en échec")
        raise PaytUploadError(f"Dépôt SFTP impossible : {exc}") from exc
    finally:
        client.close()

    logger.info("CSV déposé sur %s (%d octets)", remote_path, len(payload))
    return remote_path
