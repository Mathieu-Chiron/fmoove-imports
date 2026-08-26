"""Réception des imports par email.

Une boîte IMAP dédiée reçoit un email par import : le client y joint ses deux
exports FMS. Un cron réveille l'app, qui relève les messages non lus, identifie
les deux fichiers par leur contenu (pas seulement leur nom), les traite et
répond par email. Les messages traités sont marqués « lus » pour ne jamais être
rejoués.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.utils import getaddresses, parseaddr

import pandas as pd

from app import gmail_client
from app.transform import CLIENT_REQUIRED_SOURCE

logger = logging.getLogger(__name__)

EXCEL_SUFFIXES = (".xlsx", ".xls")


# En-têtes portant l'adresse de destination (utile derrière un catch-all).
_RECIPIENT_HEADERS = ("To", "Cc", "Delivered-To", "X-Original-To", "X-Forwarded-To")


@dataclass
class InboundEmail:
    sender: str
    subject: str
    attachments: dict[str, bytes] = field(default_factory=dict)
    recipients: set[str] = field(default_factory=set)


def parse_message(raw: bytes) -> InboundEmail:
    """Extrait expéditeur, destinataires, sujet et pièces jointes d'un email brut."""
    msg = message_from_bytes(raw, policy=policy.default)
    sender = parseaddr(msg.get("From", ""))[1].lower()
    subject = msg.get("Subject", "") or ""

    header_values = [v for h in _RECIPIENT_HEADERS for v in msg.get_all(h, [])]
    recipients = {addr.lower() for _, addr in getaddresses(header_values) if addr}

    attachments: dict[str, bytes] = {}
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            attachments[filename] = payload
    return InboundEmail(
        sender=sender, subject=subject, attachments=attachments, recipients=recipients
    )


def _looks_like_documents(content: bytes) -> bool:
    try:
        sheets = pd.ExcelFile(io.BytesIO(content)).sheet_names
    except Exception:  # noqa: BLE001 - pièce jointe illisible => pas ce fichier
        return False
    return any(s in ("Factures", "Avoirs") for s in sheets)


def _looks_like_clients(content: bytes) -> bool:
    try:
        columns = pd.read_excel(io.BytesIO(content), dtype=str, nrows=0).columns
    except Exception:  # noqa: BLE001
        return False
    return set(CLIENT_REQUIRED_SOURCE).issubset(set(columns))


def identify_files(attachments: dict[str, bytes]) -> tuple[bytes, bytes, str, str]:
    """Renvoie (clients, documents, nom_clients, nom_documents).

    L'identification se fait par le contenu : le fichier « tous documents » a des
    onglets Factures/Avoirs, le fichier « base clients » a les colonnes attendues.
    Lève ValueError si on ne trouve pas exactement un de chaque.
    """
    xlsx = {
        n: b for n, b in attachments.items() if n.lower().endswith(EXCEL_SUFFIXES)
    }
    if len(xlsx) != 2:
        raise ValueError(
            f"Il faut exactement 2 fichiers Excel en pièce jointe (reçu : {len(xlsx)})."
        )

    clients = documents = None
    clients_name = documents_name = ""
    for name, content in xlsx.items():
        if _looks_like_documents(content):
            documents, documents_name = content, name
        elif _looks_like_clients(content):
            clients, clients_name = content, name

    if clients is None or documents is None:
        raise ValueError(
            "Impossible d'identifier les 2 fichiers : il faut un export « base "
            "clients » (colonnes Raison sociale, Adresse…) et un export « tous "
            "documents » (onglets Factures/Avoirs)."
        )
    return clients, documents, clients_name, documents_name


# Le handler renvoie un statut (processed/skipped/rejected) ; détermine si
# l'email est marqué « lu ».
def poll(handler: Callable[[InboundEmail], str]) -> dict[str, int]:
    """Relève les non-lus via l'API Gmail, les passe au handler, marque « lus ».

    Un handler qui lève laisse l'email non lu (retenté au prochain cron) ; un
    handler qui renvoie un statut marque l'email « lu » (retire le label UNREAD).
    """
    summary = {"seen": 0, "processed": 0, "skipped": 0, "rejected": 0, "errors": 0}
    svc = gmail_client.service()
    for message_id in gmail_client.list_unread_ids(svc):
        summary["seen"] += 1
        try:
            status = handler(parse_message(gmail_client.get_raw(svc, message_id)))
        except Exception:  # noqa: BLE001 - on garde l'email pour le prochain run
            logger.exception("Traitement d'un email en échec, laissé non lu")
            summary["errors"] += 1
            continue
        summary[status] = summary.get(status, 0) + 1
        gmail_client.mark_read(svc, message_id)
    return summary
