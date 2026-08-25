"""Dépôt du CSV via l'API d'import de Payt.

On envoie le CSV encodé en base64 à l'endpoint d'import de fichiers. Comme le
dépôt SFTP, ce canal est traité par Payt une fois par jour (~01h00) : une
réponse 2xx signifie que le fichier est accepté pour traitement, pas que
l'import a réussi. Le résultat se vérifie le lendemain dans l'onglet Import de
l'administration Payt.

Auth : le token statique part dans l'en-tête `Authorization: Bearer`, et le
`import_token` dans le corps — on envoie les deux, Payt utilise ce qu'il attend.
"""

from __future__ import annotations

import base64
import logging

import httpx

logger = logging.getLogger(__name__)


class PaytUploadError(RuntimeError):
    """Le dépôt via l'API n'a pas abouti."""


def upload_csv(
    csv_text: str,
    filename: str,
    *,
    import_url: str,
    api_token: str,
    import_token: str,
    administration_code: str,
    timeout: float = 30.0,
) -> None:
    """Poste le CSV à Payt. Lève PaytUploadError si l'appel n'aboutit pas.

    `base64encode` produit une seule ligne (pas de saut tous les 76 caractères,
    contrairement à `encodebytes`), ce qui est requis : une chaîne JSON ne peut
    pas contenir de retour à la ligne.
    """
    payload = csv_text.encode("utf-8")
    body = {
        "file": {
            "filename": filename,
            "base64_data": base64.b64encode(payload).decode("ascii"),
        },
        "administration_code": administration_code,
        "import_token": import_token,
    }
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(import_url, json=body, headers=headers, timeout=timeout)
    except httpx.RequestError as exc:
        logger.exception("Appel de l'API d'import Payt en échec")
        raise PaytUploadError(f"Appel de l'API Payt impossible : {exc}") from exc

    if response.status_code // 100 != 2:
        detail = response.text.strip()[:500]
        logger.error("Import Payt refusé : HTTP %s — %s", response.status_code, detail)
        raise PaytUploadError(
            f"Import Payt refusé (HTTP {response.status_code}) : {detail or 'sans détail'}"
        )

    logger.info(
        "CSV %s (%d octets) accepté par l'API d'import Payt", filename, len(payload)
    )
