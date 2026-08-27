"""Import Fairmoove → Payt.

Le client dépose ses deux exports FMS ; l'application fusionne, contrôle et
affiche un **aperçu** du CSV. Rien n'est transmis à Payt tant que le client n'a
pas **confirmé** l'envoi. L'aperçu porte le CSV généré (champ caché) que la
confirmation renvoie tel quel — le CSV envoyé est exactement celui prévisualisé.
"""

from __future__ import annotations

import base64
import csv
import io
import logging
import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import archive, detect
from app.payt_api import PaytUploadError, upload_csv
from app.settings import settings
from app.transform import build_export

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PREVIEW_ROWS = 15

app = FastAPI(title="Import Fairmoove → Payt", docs_url=None, redoc_url=None)
_here = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_here / "static")), name="static")
templates = Jinja2Templates(directory=str(_here / "templates"))
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


async def _collect(files: list[UploadFile]) -> dict[str, bytes]:
    """Lit et valide chaque fichier déposé (Excel, non vide, < 20 Mo)."""
    contents: dict[str, bytes] = {}
    for upload in files:
        name = upload.filename or "(sans nom)"
        if not name.lower().endswith((".xlsx", ".xls")):
            raise ValueError(f"« {name} » n'est pas un classeur Excel (.xlsx).")
        data = await upload.read()
        if not data:
            raise ValueError(f"« {name} » est vide.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"« {name} » dépasse 20 Mo.")
        contents[name] = data
    return contents


def _index_with_error(request: Request, message: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"missing_config": settings.check(), "error": message},
        status_code=400,
    )


def _preview_rows(csv_text: str, limit: int = PREVIEW_ROWS):
    """En-têtes, premières lignes (limitées), total, et si c'est tronqué."""
    rows = list(csv.reader(io.StringIO(csv_text), delimiter=";"))
    if not rows:
        return [], [], 0, False
    headers, data = rows[0], rows[1:]
    return headers, data[:limit], len(data), len(data) > limit


def _require_config() -> None:
    missing = settings.check()
    if missing:
        raise HTTPException(500, f"Configuration incomplète : {', '.join(missing)}.")


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
    files: list[UploadFile] = File(...),
    _: str = Depends(authenticate),
) -> HTMLResponse:
    """Identifie les 2 exports, fusionne et contrôle, puis affiche l'aperçu.

    Une seule zone d'upload : l'ordre et les noms n'importent pas, les fichiers
    sont reconnus par leur contenu. N'envoie rien à Payt.
    """
    _require_config()
    try:
        contents = await _collect(files)
        clients_bytes, documents_bytes, _cn, _dn = detect.identify_files(contents)
        csv_text, report = build_export(
            clients_bytes,
            documents_bytes,
            administration_code=settings.administration_code,
            previous_row_count=archive.read_last_row_count(),
        )
    except ValueError as exc:
        return _index_with_error(request, str(exc))

    filename = datetime.now(UTC).strftime(settings.filename_pattern)
    headers, preview_rows, total_rows, truncated = _preview_rows(csv_text)

    return templates.TemplateResponse(
        request,
        "preview.html",
        {
            "report": report,
            "filename": filename,
            "csv_b64": base64.b64encode(csv_text.encode("utf-8")).decode(),
            "headers": headers,
            "preview_rows": preview_rows,
            "total_rows": total_rows,
            "truncated": truncated,
        },
    )


@app.post("/send", response_class=HTMLResponse)
def send(
    request: Request,
    csv_b64: str = Form(...),
    filename: str = Form(...),
    row_count: int = Form(0),
    _: str = Depends(authenticate),
) -> HTMLResponse:
    """Envoie à Payt le CSV validé à l'aperçu. Appelé par la confirmation."""
    _require_config()
    try:
        csv_text = base64.b64decode(csv_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "CSV invalide.") from exc

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    sent, error = False, None
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

    archive.save_run(stamp, csv_text, {}, row_count)

    return templates.TemplateResponse(
        request,
        "sent.html",
        {"sent": sent, "error": error, "filename": filename, "row_count": row_count},
    )
