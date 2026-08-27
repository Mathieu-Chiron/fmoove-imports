"""Tests HTTP : authentification, aperçu (sans envoi), confirmation → envoi Payt."""

import os
import re

import pytest
from fastapi.testclient import TestClient

os.environ.update(
    APP_USER="fairmoove",
    APP_PASSWORD="motdepasse-test",
    PAYT_ADMINISTRATION_CODE="FAIRMOOVE",
    PAYT_API_TOKEN="static-token-test",
    PAYT_IMPORT_TOKEN="import-token-test",
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


def files(factures=None):
    cl, dc = workbooks([client()], factures or [doc()])
    return {"clients": ("clients.xlsx", cl), "documents": ("documents.xlsx", dc)}


def _field(html, name):
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"champ caché « {name} » introuvable dans l'aperçu"
    return match.group(1)


# --- authentification ---------------------------------------------------------

def test_refuse_sans_authentification(api):
    assert api.get("/").status_code == 401


def test_refuse_un_mauvais_mot_de_passe(api):
    assert api.get("/", auth=("fairmoove", "wrong")).status_code == 401


def test_affiche_le_formulaire(api):
    response = api.get("/", auth=AUTH)
    assert response.status_code == 200
    assert "Base clients" in response.text


# --- aperçu (POST /import) ----------------------------------------------------

def test_apercu_naffiche_le_csv_sans_rien_envoyer(api, uploaded):
    response = api.post("/import", auth=AUTH, files=files())

    assert response.status_code == 200
    assert uploaded == []  # rien n'est envoyé au stade de l'aperçu
    assert "Aperçu du CSV" in response.text
    assert "FAIRMOOVE" in response.text  # le contenu du CSV est prévisualisé
    assert "Confirmer et envoyer à Payt" in response.text


def test_apercu_bloque_ne_propose_pas_l_envoi(api, uploaded):
    response = api.post("/import", auth=AUTH, files=files(factures=[doc(Client="INCONNU")]))

    assert response.status_code == 200
    assert uploaded == []
    assert "Rien n'a été envoyé" in response.text
    assert 'name="csv_b64"' not in response.text  # pas de bouton d'envoi si bloqué


def test_rejette_un_fichier_non_excel(api, uploaded):
    response = api.post(
        "/import", auth=AUTH,
        files={"clients": ("notes.txt", b"bonjour"), "documents": ("d.xlsx", b"x")},
    )

    assert response.status_code == 400
    assert uploaded == []


def test_rejette_un_classeur_aux_mauvaises_colonnes(api, uploaded):
    import io

    import pandas as pd

    wrong = io.BytesIO()
    pd.DataFrame([{"Nom": "ACME"}]).to_excel(wrong, index=False)
    _, dc = workbooks([client()], [doc()])

    response = api.post(
        "/import", auth=AUTH,
        files={"clients": ("c.xlsx", wrong.getvalue()), "documents": ("d.xlsx", dc)},
    )

    assert response.status_code == 400
    assert uploaded == []


# --- confirmation (POST /send) ------------------------------------------------

def test_confirmer_envoie_a_payt_le_csv_previsualise(api, uploaded):
    preview = api.post("/import", auth=AUTH, files=files()).text
    csv_b64, filename = _field(preview, "csv_b64"), _field(preview, "filename")

    response = api.post(
        "/send", auth=AUTH,
        data={"csv_b64": csv_b64, "filename": filename, "row_count": "1"},
    )

    assert response.status_code == 200
    assert len(uploaded) == 1
    sent_filename, sent_csv = uploaded[0]
    assert sent_filename == filename
    assert "FAIRMOOVE" in sent_csv  # exactement le CSV prévisualisé
    assert "déposé chez Payt" in response.text


def test_send_exige_l_authentification(api):
    assert api.post("/send", data={"csv_b64": "x", "filename": "f.csv"}).status_code == 401


def test_signale_lechec_de_lenvoi(api, monkeypatch):
    preview = api.post("/import", auth=AUTH, files=files()).text
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
