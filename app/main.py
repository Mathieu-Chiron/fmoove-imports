"""Import Fairmoove → Payt.

Le client dépose ses deux exports FMS, l'application fusionne, contrôle et
transmet le CSV à l'API d'import de Payt. L'envoi est automatique dès que les contrôles
bloquants passent ; les avertissements partent par mail.
"""

from __future__ import annotations

import base64
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app import alerts, archive
from app.payt_api import PaytUploadError, upload_csv
from app.settings import settings
from app.transform import build_export

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
        csv_text, report = build_export(
            clients_bytes,
            documents_bytes,
            administration_code=settings.administration_code,
            previous_row_count=archive.read_last_row_count(),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

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

        archive.save_run(
            stamp,
            csv_text,
            {clients.filename: clients_bytes, documents.filename: documents_bytes},
            report.row_count,
        )
        alerts.send_summary(report, filename, error)

    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "report": report,
            "filename": filename,
            "sent": sent,
            "error": error,
            "csv_b64": base64.b64encode(csv_text.encode("utf-8")).decode(),
        },
    )
