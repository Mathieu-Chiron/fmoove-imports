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
import time

import httpx

logger = logging.getLogger(__name__)

# Codes rejouables : 429 (limite Payt : > 3 fichiers/s) et erreurs serveur 5xx.
# Une 400/401 n'est jamais rejouée : elle se reproduirait à l'identique.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


def _sleep(seconds: float) -> None:  # indirection pour neutraliser l'attente en test
    time.sleep(seconds)


def _post(url: str, *, json: dict, headers: dict, timeout: float, proxy: str = ""):
    """POST httpx, éventuellement via un proxy à IP statique (ex. Fixie).

    Le proxy n'est appliqué qu'à l'appel Payt (pas aux autres flux, S3…), pour
    présenter à Payt une IP de sortie fixe à whitelister sur le token.
    """
    with httpx.Client(proxy=proxy or None, timeout=timeout) as client:
        return client.post(url, json=json, headers=headers)


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
    proxy: str = "",
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

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        is_last = attempt == _MAX_ATTEMPTS
        try:
            response = _post(
                import_url, json=body, headers=headers, timeout=timeout, proxy=proxy
            )
        except httpx.RequestError as exc:
            logger.warning(
                "Appel API Payt en échec (tentative %d/%d) : %s", attempt, _MAX_ATTEMPTS, exc
            )
            if is_last:
                raise PaytUploadError(f"Appel de l'API Payt impossible : {exc}") from exc
            _sleep(2 ** (attempt - 1))
            continue

        if response.status_code // 100 == 2:
            logger.info(
                "CSV %s (%d octets) accepté par l'API d'import Payt", filename, len(payload)
            )
            return

        detail = response.text.strip()[:500]
        if response.status_code in _RETRYABLE_STATUS and not is_last:
            logger.warning(
                "Import Payt HTTP %s, nouvelle tentative (%d/%d)",
                response.status_code, attempt, _MAX_ATTEMPTS,
            )
            _sleep(2 ** (attempt - 1))
            continue

        logger.error("Import Payt refusé : HTTP %s — %s", response.status_code, detail)
        raise PaytUploadError(
            f"Import Payt refusé (HTTP {response.status_code}) : {detail or 'sans détail'}"
        )
