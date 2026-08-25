"""Import Fairmoove → Payt, multi-clients.

Chaque client a sa boîte mail et sa propre administration Payt (voir
`TENANTS_JSON`). Un cron horaire relève la boîte de chaque client, identifie les
deux exports FMS joints à l'email, les fusionne, contrôle, transmet à l'API
Payt du client, et répond par email. Les clients sont isolés : une erreur sur
l'un n'affecte pas les autres.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Body, FastAPI, Header, HTTPException

from app import alerts, archive, inbound
from app.payt_api import PaytUploadError, upload_csv
from app.settings import Tenant, settings
from app.transform import Report, build_export

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Import Fairmoove → Payt", docs_url=None, redoc_url=None)


@dataclass
class ProcessResult:
    report: Report
    filename: str
    sent: bool
    error: str | None
    csv_text: str
    stamp: str


def process_workbooks(
    clients_bytes: bytes,
    documents_bytes: bytes,
    sources: dict[str, bytes],
    tenant: Tenant,
) -> ProcessResult:
    """Fusionne, contrôle, transmet à l'API Payt du client et archive.

    Peut lever ValueError si un classeur n'a pas la bonne structure.
    """
    csv_text, report = build_export(
        clients_bytes,
        documents_bytes,
        administration_code=tenant.administration_code,
        previous_row_count=archive.read_last_row_count(tenant.name),
    )
    now = datetime.now(UTC)
    filename = now.strftime(settings.filename_pattern)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    sent, error = False, None

    if not report.is_blocked:
        try:
            upload_csv(
                csv_text,
                filename,
                import_url=settings.import_url,
                api_token=tenant.api_token,
                import_token=tenant.import_token,
                administration_code=tenant.administration_code,
            )
            sent = True
        except PaytUploadError as exc:
            error = str(exc)
        archive.save_run(tenant.name, stamp, csv_text, sources, report.row_count)

    return ProcessResult(report, filename, sent, error, csv_text, stamp)


def _handle_email(msg: inbound.InboundEmail) -> str:
    """Route l'email vers le bon client (par destinataire) puis le traite."""
    tenant = settings.tenant_for(msg.recipients)
    if tenant is None:
        logger.warning(
            "Email pour un destinataire inconnu ignoré : %s",
            ", ".join(sorted(msg.recipients)) or "(aucun)",
        )
        return "skipped"
    if msg.sender not in tenant.allowed_senders:
        logger.warning("[%s] expéditeur non autorisé ignoré : %s", tenant.name, msg.sender)
        return "skipped"

    fallback_name = datetime.now(UTC).strftime(settings.filename_pattern)
    try:
        clients_b, documents_b, cname, dname = inbound.identify_files(msg.attachments)
    except ValueError as exc:
        alerts.send_error(tenant, msg.sender, fallback_name, str(exc))
        logger.info("[%s] email de %s rejeté : %s", tenant.name, msg.sender, exc)
        return "rejected"

    try:
        result = process_workbooks(
            clients_b, documents_b, {cname: clients_b, dname: documents_b}, tenant
        )
    except ValueError as exc:
        alerts.send_error(tenant, msg.sender, fallback_name, str(exc))
        logger.info("[%s] email de %s rejeté (structure) : %s", tenant.name, msg.sender, exc)
        return "rejected"

    alerts.send_report(
        tenant, msg.sender, result.report, result.filename, result.error, result.sent
    )
    logger.info(
        "[%s] email de %s traité : %s (%d lignes, envoyé=%s)",
        tenant.name, msg.sender, result.filename, result.report.row_count, result.sent,
    )
    return "processed"


def _run_poll(token: str) -> dict[str, int]:
    if not settings.inbound_enabled:
        raise HTTPException(
            503, "Réception non configurée (POLL_TOKEN / MAILBOX / TENANTS_JSON)."
        )
    if not (settings.poll_token and secrets.compare_digest(token, settings.poll_token)):
        raise HTTPException(401, "Jeton invalide.")

    return inbound.poll_inbox(
        settings.imap_host,
        settings.imap_port,
        settings.mailbox_user,
        settings.mailbox_password,
        _handle_email,
    )


@app.get("/health", include_in_schema=False)
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "tenants": [t.name for t in settings.tenants],
        "issues": settings.check(),
    }


@app.post("/poll-inbox", include_in_schema=False)
def poll_inbox_endpoint(
    token: str = "", x_poll_token: str = Header(default="")
) -> dict[str, object]:
    """Déclenché par le cron : relève chaque boîte et traite les imports."""
    return _run_poll(token or x_poll_token)


@app.post("/", include_in_schema=False)
async def cron_entrypoint(payload: dict = Body(default={})) -> dict[str, object]:
    """Point d'entrée du cron Scaleway (envoie ses args en corps JSON)."""
    token = payload.get("token", "") if isinstance(payload, dict) else ""
    return _run_poll(token)
