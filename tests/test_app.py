"""Tests HTTP : auth, zone d'upload unique + détection, aperçu, confirmation."""

import os
import re

import pytest
from fastapi.testclient import TestClient

os.environ.update(
    APP_USER="fairmoove",
    APP_PASSWORD="motdepasse-test",
    PAYT_ADMINISTRATION_CODE="FAIRMOOVE",
    PAYT_SFTP_HOST="sftp.paytsoftware.test",
    PAYT_SFTP_USER="ftp_demo",
    PAYT_SFTP_PASSWORD="secret",
)

from app import main  # noqa: E402
from tests.test_transform import client, doc, workbooks  # noqa: E402

AUTH = ("fairmoove", "motdepasse-test")


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(main.archive, "read_last_row_count", lambda: None)
    monkeypatch.setattr(main.archive, "save_run", lambda *a, **k: None)
    return TestClient(main.app)


@pytest.fixture
def uploaded(monkeypatch):
    """Capture ce qui aurait été envoyé à l'API Payt."""
    calls = []
    monkeypatch.setattr(
        main, "upload_csv", lambda csv_text, filename, **kw: calls.append((filename, csv_text))
    )
    return calls


def upload(factures=None, names=("clients.xlsx", "documents.xlsx")):
    """Les 2 fichiers, envoyés dans le champ unique « files »."""
    cl, dc = workbooks([client()], factures or [doc()])
    return [("files", (names[0], cl)), ("files", (names[1], dc))]


def _field(html, name):
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"champ caché « {name} » introuvable dans l'aperçu"
    return match.group(1)


# --- authentification ---------------------------------------------------------

def test_refuse_sans_authentification(api):
    assert api.get("/").status_code == 401


def test_affiche_le_formulaire(api):
    response = api.get("/", auth=AUTH)
    assert response.status_code == 200
    assert "Glissez-déposez" in response.text
    assert "/static/style.css" in response.text
    assert "/static/fairmoove-logo.png" in response.text


def test_sert_le_css_sans_authentification(api):
    response = api.get("/static/style.css")
    assert response.status_code == 200
    assert "--brand" in response.text


# --- zone unique + détection --------------------------------------------------

def test_apercu_naffiche_le_csv_sans_rien_envoyer(api, uploaded):
    response = api.post("/import", auth=AUTH, files=upload())

    assert response.status_code == 200
    assert uploaded == []
    assert "Aperçu du CSV" in response.text
    assert "FAIRMOOVE" in response.text
    assert "Confirmer et envoyer à Payt" in response.text


def test_identifie_les_fichiers_quel_que_soit_l_ordre(api):
    cl, dc = workbooks([client()], [doc()])
    # documents en premier, clients ensuite, noms neutres
    files = [("files", ("z.xlsx", dc)), ("files", ("a.xlsx", cl))]

    response = api.post("/import", auth=AUTH, files=files)

    assert response.status_code == 200
    assert "Aperçu du CSV" in response.text


def test_refuse_si_pas_deux_fichiers(api, uploaded):
    cl, _ = workbooks([client()], [doc()])
    response = api.post("/import", auth=AUTH, files=[("files", ("seul.xlsx", cl))])

    assert response.status_code == 400
    assert "exactement 2" in response.text
    assert uploaded == []


def test_refuse_si_fichiers_non_identifiables(api, uploaded):
    cl, _ = workbooks([client()], [doc()])
    response = api.post(
        "/import", auth=AUTH,
        files=[("files", ("a.xlsx", cl)), ("files", ("b.xlsx", cl))],  # deux « clients »
    )

    assert response.status_code == 400
    assert "identifier" in response.text
    assert uploaded == []


def test_refuse_un_fichier_non_excel(api, uploaded):
    _, dc = workbooks([client()], [doc()])
    response = api.post(
        "/import", auth=AUTH,
        files=[("files", ("notes.txt", b"bonjour")), ("files", ("d.xlsx", dc))],
    )

    assert response.status_code == 400
    assert "Excel" in response.text
    assert uploaded == []


def test_apercu_bloque_ne_propose_pas_l_envoi(api, uploaded):
    response = api.post("/import", auth=AUTH, files=upload(factures=[doc(Client="INCONNU")]))

    assert response.status_code == 200
    assert uploaded == []
    assert "Rien n'a été envoyé" in response.text
    assert 'name="csv_b64"' not in response.text


# --- confirmation (POST /send) ------------------------------------------------

def test_confirmer_envoie_a_payt_le_csv_previsualise(api, uploaded):
    preview = api.post("/import", auth=AUTH, files=upload()).text
    csv_b64, filename = _field(preview, "csv_b64"), _field(preview, "filename")

    response = api.post(
        "/send", auth=AUTH,
        data={"csv_b64": csv_b64, "filename": filename, "row_count": "1"},
    )

    assert response.status_code == 200
    assert len(uploaded) == 1
    sent_filename, sent_csv = uploaded[0]
    assert sent_filename == filename
    assert "FAIRMOOVE" in sent_csv
    assert "déposé chez Payt" in response.text


def test_send_exige_l_authentification(api):
    assert api.post("/send", data={"csv_b64": "x", "filename": "f.csv"}).status_code == 401


def test_signale_lechec_de_lenvoi(api, monkeypatch):
    preview = api.post("/import", auth=AUTH, files=upload()).text
    csv_b64, filename = _field(preview, "csv_b64"), _field(preview, "filename")

    def boom(*args, **kwargs):
        raise main.PaytUploadError("connexion refusée")

    monkeypatch.setattr(main, "upload_csv", boom)
    response = api.post(
        "/send", auth=AUTH,
        data={"csv_b64": csv_b64, "filename": filename, "row_count": "1"},
    )

    assert response.status_code == 200
    assert "connexion refusée" in response.text
