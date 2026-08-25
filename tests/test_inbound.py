"""Tests de la réception par email : identification, parsing, handler, endpoint.

Aucun serveur IMAP/SMTP réel : le handler est testé en direct et `poll_inbox`
est remplacé par un double pour les tests d'endpoint.
"""

from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from app import inbound, main
from app.settings import settings
from tests.test_transform import client, doc, workbooks


def _raw_email(sender: str, files: dict[str, bytes]) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["Subject"] = "Import du jour"
    msg.set_content("Voir pièces jointes.")
    for name, content in files.items():
        msg.add_attachment(
            content, maintype="application", subtype="vnd.ms-excel", filename=name
        )
    return msg.as_bytes()


# --- identification par contenu ----------------------------------------------

def test_identifie_les_deux_fichiers_quel_que_soit_l_ordre():
    cl, dc = workbooks([client()], [doc()])
    for atts in ({"a.xlsx": cl, "b.xlsx": dc}, {"z.xlsx": dc, "y.xlsx": cl}):
        clients_b, documents_b, _, _ = inbound.identify_files(atts)
        assert clients_b == cl
        assert documents_b == dc


def test_rejette_si_pas_exactement_deux_excel():
    cl, _ = workbooks([client()], [doc()])
    with pytest.raises(ValueError, match="2 fichiers"):
        inbound.identify_files({"a.xlsx": cl})


def test_rejette_si_fichiers_non_identifiables():
    cl, _ = workbooks([client()], [doc()])
    with pytest.raises(ValueError, match="identifier"):
        inbound.identify_files({"a.xlsx": cl, "b.xlsx": cl})  # deux « clients »


# --- parsing d'un email brut --------------------------------------------------

def test_parse_message_extrait_expediteur_sujet_et_pieces_jointes():
    cl, dc = workbooks([client()], [doc()])
    raw = _raw_email("Le Client <CLIENT@corp.test>", {"clients.xlsx": cl, "docs.xlsx": dc})

    msg = inbound.parse_message(raw)

    assert msg.sender == "client@corp.test"  # normalisé en minuscules
    assert set(msg.attachments) == {"clients.xlsx", "docs.xlsx"}


# --- handler ------------------------------------------------------------------

@pytest.fixture
def configured(monkeypatch):
    for attr, value in {
        "app_password": "pw",
        "administration_code": "FAIRMOOVE",
        "api_token": "t",
        "import_token": "it",
        "imap_host": "imap.test",
        "imap_user": "u",
        "imap_password": "p",
        "poll_token": "secret-poll",
        "inbound_allowlist": "client@corp.test",
    }.items():
        monkeypatch.setattr(settings, attr, value)
    monkeypatch.setattr(main.archive, "read_last_row_count", lambda: None)
    monkeypatch.setattr(main.archive, "save_run", lambda *a, **k: None)


@pytest.fixture
def spies(monkeypatch):
    calls = {"upload": [], "report": [], "error": []}
    monkeypatch.setattr(main, "upload_csv", lambda *a, **k: calls["upload"].append(k))
    monkeypatch.setattr(main.alerts, "send_report", lambda *a, **k: calls["report"].append(a))
    monkeypatch.setattr(main.alerts, "send_error", lambda *a, **k: calls["error"].append(a))
    return calls


def _email(sender, factures=None):
    cl, dc = workbooks([client()], factures or [doc()])
    return inbound.InboundEmail(
        sender=sender, subject="x", attachments={"a.xlsx": cl, "b.xlsx": dc}
    )


def test_expediteur_non_autorise_est_ignore(configured, spies):
    status = main._handle_inbound_email(_email("inconnu@ext.test"))

    assert status == "skipped"
    assert spies["upload"] == []
    assert spies["report"] == [] and spies["error"] == []


def test_email_valide_est_traite_et_envoye(configured, spies):
    status = main._handle_inbound_email(_email("client@corp.test"))

    assert status == "processed"
    assert len(spies["upload"]) == 1
    assert spies["report"] and spies["report"][0][0] == "client@corp.test"


def test_pieces_jointes_invalides_sont_rejetees(configured, spies):
    cl, _ = workbooks([client()], [doc()])
    msg = inbound.InboundEmail("client@corp.test", "x", {"seul.xlsx": cl})

    status = main._handle_inbound_email(msg)

    assert status == "rejected"
    assert spies["upload"] == []
    assert spies["error"]  # un mail de rejet est parti


def test_controle_bloquant_repond_mais_n_envoie_rien(configured, spies):
    # Facture rattachée à un client absent => contrôle bloquant.
    status = main._handle_inbound_email(
        _email("client@corp.test", factures=[doc(Client="SOCIETE INCONNUE")])
    )

    assert status == "processed"  # traité (mail marqué lu)
    assert spies["upload"] == []  # mais rien envoyé à Payt
    assert spies["report"]  # rapport avec les raisons du blocage


# --- endpoints ----------------------------------------------------------------

def test_poll_inbox_exige_le_bon_jeton(configured, monkeypatch):
    monkeypatch.setattr(main.inbound, "poll_inbox", lambda s, h: {"seen": 0})
    api = TestClient(main.app)

    assert api.post("/poll-inbox", params={"token": "mauvais"}).status_code == 401
    ok = api.post("/poll-inbox", params={"token": "secret-poll"})
    assert ok.status_code == 200
    assert ok.json()["seen"] == 0


def test_cron_entrypoint_lit_le_jeton_dans_le_corps(configured, monkeypatch):
    monkeypatch.setattr(main.inbound, "poll_inbox", lambda s, h: {"seen": 3})
    api = TestClient(main.app)

    r = api.post("/", json={"token": "secret-poll"})
    assert r.status_code == 200
    assert r.json()["seen"] == 3
