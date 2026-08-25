"""Tests HTTP : authentification, rejet des fichiers invalides, envoi API Payt."""

import os

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
    monkeypatch.setattr(main.alerts, "send_summary", lambda *a, **k: None)
    return TestClient(main.app)


@pytest.fixture
def uploaded(monkeypatch):
    """Capture ce qui aurait été déposé sur le SFTP de Payt."""
    calls = []
    monkeypatch.setattr(
        main, "upload_csv", lambda csv_text, filename, **kw: calls.append((filename, csv_text))
    )
    return calls


def files():
    cl, dc = workbooks([client()], [doc()])
    return {
        "clients": ("clients.xlsx", cl),
        "documents": ("documents.xlsx", dc),
    }


def test_refuse_sans_authentification(api):
    assert api.get("/").status_code == 401


def test_refuse_un_mauvais_mot_de_passe(api):
    assert api.get("/", auth=("fairmoove", "wrong")).status_code == 401


def test_affiche_le_formulaire(api):
    response = api.get("/", auth=AUTH)
    assert response.status_code == 200
    assert "Base clients" in response.text


def test_envoie_le_csv_et_affiche_le_rapport(api, uploaded):
    response = api.post("/import", auth=AUTH, files=files())

    assert response.status_code == 200
    assert len(uploaded) == 1
    filename, csv_text = uploaded[0]
    assert filename.endswith(".csv")
    assert "FAIRMOOVE" in csv_text
    assert "déposé chez Payt" in response.text


def test_nenvoie_rien_si_un_controle_bloque(api, uploaded):
    cl, dc = workbooks([client()], [doc(Client="INCONNU")])
    response = api.post(
        "/import",
        auth=AUTH,
        files={"clients": ("c.xlsx", cl), "documents": ("d.xlsx", dc)},
    )

    assert response.status_code == 200
    assert uploaded == []
    assert "Rien n'a été envoyé" in response.text


def test_rejette_un_fichier_non_excel(api, uploaded):
    response = api.post(
        "/import",
        auth=AUTH,
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
        "/import",
        auth=AUTH,
        files={"clients": ("c.xlsx", wrong.getvalue()), "documents": ("d.xlsx", dc)},
    )

    assert response.status_code == 400
    assert uploaded == []


def test_signale_lechec_du_depot_sftp(api, monkeypatch):
    def boom(*args, **kwargs):
        raise main.PaytUploadError("connexion refusée")

    monkeypatch.setattr(main, "upload_csv", boom)
    response = api.post("/import", auth=AUTH, files=files())

    assert response.status_code == 200
    assert "connexion refusée" in response.text
