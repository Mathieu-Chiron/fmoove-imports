"""Tests de la couche de dépôt via l'API d'import Payt.

Aucun appel réseau réel : httpx.post est remplacé par un double qui capture ou
simule la réponse. On vérifie le contrat d'appel (URL, en-tête d'auth, corps
base64) et la remontée d'erreur.
"""

import base64
import json

import httpx
import pytest

from app import payt_api
from app.payt_api import PaytUploadError, upload_csv

URL = "https://backend.paytsoftware.test/import/files/csv"


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


def _args(**over):
    base = dict(
        import_url=URL,
        api_token="static-token",
        import_token="import-token",
        administration_code="FAIRMOOVE",
    )
    return {**base, **over}


def test_poste_le_csv_en_base64_sans_saut_de_ligne(monkeypatch):
    captured = {}

    def fake_post(url, **kw):
        captured["url"] = url
        captured.update(kw)
        return FakeResponse(200, "OK")

    monkeypatch.setattr(payt_api.httpx, "post", fake_post)

    upload_csv("administration_code;debtor_code\r\nFAIRMOOVE;C-1\r\n", "20260825.csv", **_args())

    assert captured["url"] == URL
    body = captured["json"]
    assert body["administration_code"] == "FAIRMOOVE"
    assert body["import_token"] == "import-token"
    assert body["file"]["filename"] == "20260825.csv"

    b64 = body["file"]["base64_data"]
    assert "\n" not in b64 and "\r" not in b64  # JSON n'accepte pas de saut de ligne
    assert base64.b64decode(b64).decode().startswith("administration_code;debtor_code")


def test_envoie_le_token_dans_l_entete_authorization(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        payt_api.httpx, "post",
        lambda url, **kw: captured.update(kw) or FakeResponse(200),
    )

    upload_csv("x", "f.csv", **_args())

    assert captured["headers"]["Authorization"] == "Bearer static-token"


def test_leve_une_erreur_si_reponse_non_2xx(monkeypatch):
    monkeypatch.setattr(
        payt_api.httpx, "post", lambda url, **kw: FakeResponse(422, "champ manquant")
    )

    with pytest.raises(PaytUploadError, match="422"):
        upload_csv("x", "f.csv", **_args())


def test_leve_une_erreur_si_reseau_indisponible(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("connexion refusée")

    monkeypatch.setattr(payt_api.httpx, "post", boom)

    with pytest.raises(PaytUploadError, match="Payt"):
        upload_csv("x", "f.csv", **_args())


def test_le_corps_est_du_json_serialisable(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        payt_api.httpx, "post",
        lambda url, **kw: captured.update(kw) or FakeResponse(200),
    )

    upload_csv("x", "f.csv", **_args())

    json.dumps(captured["json"])  # ne doit pas lever
