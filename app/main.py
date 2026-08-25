"""Import Fairmoove → Payt.

Le CSV fusionné est transmis à l'API d'import de Payt. Deux déclencheurs partagent
le même traitement : la page web (dépôt manuel des 2 Excel) et la boîte email
relevée par un cron (`inbound`). L'envoi est automatique dès que les contrôles
bloquants passent ; les avertissements partent dans le rapport.
"""

from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import (
    Body,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app import alerts, archive, inbound
from app.payt_api import PaytUploadError, upload_csv
from app.settings import settings
from app.transform import Report, build_export

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024

app = FastAPI(title="Import Fairmoove → Payt", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    user_ok = secrets.compare_digest(credentials.username, settings.app_user)
    password_ok = secrets.compare_digest(credentials.password, settings.app_password)
    if not (user_ok and password_ok and settings.app_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Accès refusé",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


async def _read(upload: UploadFile, label: str) -> bytes:
    if not upload.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, f"Le fichier « {label} » doit être un classeur Excel.")
    content = await upload.read()
    if not content:
        raise HTTPException(400, f"Le fichier « {label} » est vide.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"Le fichier « {label} » dépasse 20 Mo.")
    return content


@dataclass
class ProcessResult:
    report: Report
    filename: str
    sent: bool
    error: str | None
    csv_text: str
    stamp: str


def process_workbooks(
    clients_bytes: bytes, documents_bytes: bytes, sources: dict[str, bytes]
) -> ProcessResult:
    """Fusionne, contrôle, transmet à Payt et archive. Cœur partagé web + email.

    Peut lever ValueError si un classeur n'a pas la bonne structure.
    """
    csv_text, report = build_export(
        clients_bytes,
        documents_bytes,
        administration_code=settings.administration_code,
        previous_row_count=archive.read_last_row_count(),
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
                api_token=settings.api_token,
                import_token=settings.import_token,
                administration_code=settings.administration_code,
            )
            sent = True
        except PaytUploadError as exc:
            error = str(exc)
        archive.save_run(stamp, csv_text, sources, report.row_count)

    return ProcessResult(report, filename, sent, error, csv_text, stamp)


def _handle_inbound_email(msg: inbound.InboundEmail) -> str:
    """Traite un email d'import. Renvoie le statut (marque l'email « lu »)."""
    if msg.sender not in settings.allowed_senders:
        logger.warning("Email d'un expéditeur non autorisé ignoré : %s", msg.sender)
        return "skipped"

    fallback_name = datetime.now(UTC).strftime(settings.filename_pattern)
    try:
        clients_b, documents_b, cname, dname = inbound.identify_files(msg.attachments)
    except ValueError as exc:
        alerts.send_error(msg.sender, fallback_name, str(exc))
        logger.info("Email de %s rejeté : %s", msg.sender, exc)
        return "rejected"

    try:
        result = process_workbooks(
            clients_b, documents_b, {cname: clients_b, dname: documents_b}
        )
    except ValueError as exc:
        alerts.send_error(msg.sender, fallback_name, str(exc))
        logger.info("Email de %s rejeté (structure) : %s", msg.sender, exc)
        return "rejected"

    alerts.send_report(
        msg.sender, result.report, result.filename, result.error, result.sent
    )
    logger.info(
        "Email de %s traité : %s (%d lignes, envoyé=%s)",
        msg.sender, result.filename, result.report.row_count, result.sent,
    )
    return "processed"


def _run_poll(token: str) -> dict[str, int]:
    if not settings.inbound_enabled:
        raise HTTPException(503, "Réception par email non configurée.")
    if not (settings.poll_token and secrets.compare_digest(token, settings.poll_token)):
        raise HTTPException(401, "Jeton invalide.")
    if settings.check():
        raise HTTPException(500, "Configuration Payt incomplète.")
    return inbound.poll_inbox(settings, _handle_inbound_email)


@app.get("/health", include_in_schema=False)
def health() -> dict[str, object]:
    return {"status": "ok", "missing_config": settings.check()}


@app.get("/", response_class=HTMLResponse)
def home(request: Request, _: str = Depends(authenticate)) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html", {"missing_config": settings.check()}
    )


@app.post("/import", response_class=HTMLResponse)
async def run_import(
    request: Request,
    clients: UploadFile = File(...),
    documents: UploadFile = File(...),
    _: str = Depends(authenticate),
) -> HTMLResponse:
    missing = settings.check()
    if missing:
        raise HTTPException(500, f"Configuration incomplète : {', '.join(missing)}.")

    clients_bytes = await _read(clients, "base clients")
    documents_bytes = await _read(documents, "tous documents")

    try:
        result = process_workbooks(
            clients_bytes,
            documents_bytes,
            {clients.filename: clients_bytes, documents.filename: documents_bytes},
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not result.report.is_blocked:
        alerts.send_summary(result.report, result.filename, result.error)

    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "report": result.report,
            "filename": result.filename,
            "sent": result.sent,
            "error": result.error,
            "csv_b64": base64.b64encode(result.csv_text.encode("utf-8")).decode(),
        },
    )


@app.post("/poll-inbox", include_in_schema=False)
def poll_inbox_endpoint(
    token: str = "", x_poll_token: str = Header(default="")
) -> dict[str, int]:
    """Déclenché par le cron : relève la boîte email et traite les imports."""
    return _run_poll(token or x_poll_token)


@app.post("/", include_in_schema=False)
async def cron_entrypoint(payload: dict = Body(default={})) -> dict[str, int]:
    """Point d'entrée du cron Scaleway (envoie ses args en corps JSON)."""
    token = payload.get("token", "") if isinstance(payload, dict) else ""
    return _run_poll(token)
