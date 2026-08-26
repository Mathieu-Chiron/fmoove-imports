"""Accès à la boîte catch-all via l'API Gmail (compte de service).

Un compte de service, autorisé par délégation à l'échelle du domaine, impersonne
`MAILBOX_USER` (ex. imports@mondomaine). Aucune 2FA, aucun mot de passe
d'application, aucun IMAP : l'auth se fait avec la clé du compte de service.
"""

from __future__ import annotations

import base64
import json
import logging
from email.message import EmailMessage

from app.settings import settings

logger = logging.getLogger(__name__)

# gmail.modify : lire les messages + retirer le label UNREAD ; gmail.send : répondre.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

_service = None


def service():
    """Service Gmail impersonnant MAILBOX_USER (mémoïsé ; tokens auto-rafraîchis)."""
    global _service
    if _service is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        info = json.loads(settings.gmail_sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        ).with_subject(settings.mailbox_user)
        _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


def list_unread_ids(svc) -> list[str]:
    """IDs des messages non lus avec pièce jointe."""
    result = (
        svc.users()
        .messages()
        .list(userId="me", q="is:unread has:attachment", maxResults=50)
        .execute()
    )
    return [m["id"] for m in result.get("messages", [])]


def get_raw(svc, message_id: str) -> bytes:
    """Message brut (RFC822) pour réutiliser inbound.parse_message."""
    msg = svc.users().messages().get(userId="me", id=message_id, format="raw").execute()
    return base64.urlsafe_b64decode(msg["raw"])


def mark_read(svc, message_id: str) -> None:
    svc.users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def send_message(from_addr: str, to: str, subject: str, body: str) -> None:
    """Envoie un email depuis la boîte impersonnée."""
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service().users().messages().send(userId="me", body={"raw": raw}).execute()
