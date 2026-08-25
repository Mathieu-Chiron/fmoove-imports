"""Réception des imports par email.

Une boîte IMAP dédiée reçoit un email par import : le client y joint ses deux
exports FMS. Un cron réveille l'app, qui relève les messages non lus, identifie
les deux fichiers par leur contenu (pas seulement leur nom), les traite et
répond par email. Les messages traités sont marqués « lus » pour ne jamais être
rejoués.
"""

from __future__ import annotations

import contextlib
import imaplib
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.utils import parseaddr

import pandas as pd

from app.transform import CLIENT_REQUIRED_SOURCE

logger = logging.getLogger(__name__)

EXCEL_SUFFIXES = (".xlsx", ".xls")


@dataclass
class InboundEmail:
    sender: str
    subject: str
    attachments: dict[str, bytes] = field(default_factory=dict)


def parse_message(raw: bytes) -> InboundEmail:
    """Extrait l'expéditeur, le sujet et les pièces jointes d'un email brut."""
    msg = message_from_bytes(raw, policy=policy.default)
    sender = parseaddr(msg.get("From", ""))[1].lower()
    subject = msg.get("Subject", "") or ""
    attachments: dict[str, bytes] = {}
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            attachments[filename] = payload
    return InboundEmail(sender=sender, subject=subject, attachments=attachments)


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


# Statut renvoyé par le handler ; détermine si l'email est marqué « lu ».
def poll_inbox(settings, handler: Callable[[InboundEmail], str]) -> dict[str, int]:
    """Relève les emails non lus, les passe au handler, marque les traités « lus ».

    Un handler qui lève laisse l'email non lu (retenté au prochain cron) ; un
    handler qui renvoie un statut marque l'email « lu ».
    """
    summary = {"seen": 0, "processed": 0, "skipped": 0, "rejected": 0, "errors": 0}
    imap = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    try:
        imap.login(settings.imap_user, settings.imap_password)
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        message_ids = data[0].split() if data and data[0] else []
        for num in message_ids:
            summary["seen"] += 1
            _, fetched = imap.fetch(num, "(RFC822)")
            raw = fetched[0][1]
            try:
                status = handler(parse_message(raw))
            except Exception:  # noqa: BLE001 - on garde l'email pour le prochain run
                logger.exception("Traitement d'un email en échec, laissé non lu")
                summary["errors"] += 1
                continue
            summary[status] = summary.get(status, 0) + 1
            imap.store(num, "+FLAGS", "\\Seen")
        return summary
    finally:
        with contextlib.suppress(Exception):
            imap.logout()
