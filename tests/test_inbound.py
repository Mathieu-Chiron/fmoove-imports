"""Tests de la réception par email en catch-all (routage par destinataire).

Aucun serveur IMAP/SMTP réel : IMAP4_SSL est remplacé par un faux et les envois
sont espionnés. On vérifie l'identification, le routage par adresse, l'isolation.
"""

from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from app import inbound, main
from app.settings import Tenant, settings
from tests.test_transform import client, doc, workbooks


def make_tenant(**over) -> Tenant:
    base = dict(
        name="Fairmoove",
        inbox_address="fairmoove@md",
        administration_code="FAIRMOOVE", api_token="tok", import_token="imp",
        allowed_senders={"compta@fairmoove.fr"},
        from_addr="",
    )
    base.update(over)
    return Tenant(**base)


def _raw_email(sender: str, to: str, files: dict[str, bytes]) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
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
        inbound.identify_files({"a.xlsx": cl, "b.xlsx": cl})


# --- parsing : expéditeur + destinataires ------------------------------------

def test_parse_message_extrait_expediteur_destinataires_et_pj():
    cl, dc = workbooks([client()], [doc()])
    raw = _raw_email("Client <CLIENT@corp.test>", "Fairmoove <FAIRMOOVE@md>",
                     {"c.xlsx": cl, "d.xlsx": dc})

    msg = inbound.parse_message(raw)

    assert msg.sender == "client@corp.test"
    assert "fairmoove@md" in msg.recipients  # normalisé, sert au routage
    assert set(msg.attachments) == {"c.xlsx", "d.xlsx"}


# --- routage + handler --------------------------------------------------------

@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(settings, "poll_token", "secret-poll")
    monkeypatch.setattr(settings, "imap_host", "imap.test")
    monkeypatch.setattr(settings, "mailbox_user", "catchall@md")
    monkeypatch.setattr(settings, "mailbox_password", "pw")
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "mailbox_from", "catchall@md")
    monkeypatch.setattr(settings, "tenants", [make_tenant()])


@pytest.fixture
def spies(monkeypatch):
    calls = {"upload": [], "report": [], "error": []}
    monkeypatch.setattr(main, "upload_csv", lambda *a, **k: calls["upload"].append(k))
    monkeypatch.setattr(main.alerts, "send_report", lambda *a, **k: calls["report"].append(a))
    monkeypatch.setattr(main.alerts, "send_error", lambda *a, **k: calls["error"].append(a))
    monkeypatch.setattr(main.archive, "read_last_row_count", lambda k: None)
    monkeypatch.setattr(main.archive, "save_run", lambda *a, **k: None)
    return calls


def _email(sender, to="fairmoove@md", factures=None):
    cl, dc = workbooks([client()], factures or [doc()])
    return inbound.InboundEmail(
        sender=sender, subject="x",
        attachments={"a.xlsx": cl, "b.xlsx": dc}, recipients={to},
    )


def test_destinataire_inconnu_est_ignore(registry, spies):
    status = main._handle_email(_email("compta@fairmoove.fr", to="autre@md"))

    assert status == "skipped"
    assert spies["upload"] == []


def test_expediteur_non_autorise_est_ignore(registry, spies):
    status = main._handle_email(_email("inconnu@ext.test"))

    assert status == "skipped"
    assert spies["upload"] == []


def test_email_valide_route_et_utilise_les_creds_du_client(registry, spies, monkeypatch):
    monkeypatch.setattr(
        settings, "tenants",
        [make_tenant(name="Client2", inbox_address="client2@md",
                     administration_code="CLI2", api_token="T2", import_token="I2",
                     allowed_senders={"compta@client2.fr"})],
    )

    status = main._handle_email(_email("compta@client2.fr", to="client2@md"))

    assert status == "processed"
    assert len(spies["upload"]) == 1
    sent = spies["upload"][0]
    assert sent["administration_code"] == "CLI2"
    assert sent["api_token"] == "T2"
    assert spies["report"][0][1] == "compta@client2.fr"


def test_pieces_jointes_invalides_sont_rejetees(registry, spies):
    cl, _ = workbooks([client()], [doc()])
    msg = inbound.InboundEmail("compta@fairmoove.fr", "x", {"seul.xlsx": cl}, {"fairmoove@md"})

    status = main._handle_email(msg)

    assert status == "rejected"
    assert spies["upload"] == [] and spies["error"]


def test_controle_bloquant_repond_mais_n_envoie_rien(registry, spies):
    status = main._handle_email(
        _email("compta@fairmoove.fr", factures=[doc(Client="INCONNU")])
    )

    assert status == "processed"
    assert spies["upload"] == []
    assert spies["report"]


# --- boucle IMAP (poll_inbox) -------------------------------------------------

def test_poll_inbox_releve_marque_lu_et_compte(monkeypatch):
    cl, dc = workbooks([client()], [doc()])
    raws = {
        b"1": _raw_email("compta@fairmoove.fr", "fairmoove@md", {"a.xlsx": cl, "b.xlsx": dc}),
        b"2": _raw_email("spam@ext.test", "fairmoove@md", {"a.xlsx": cl, "b.xlsx": dc}),
    }

    class FakeIMAP:
        def __init__(self, host, port):
            self.stored = []

        def login(self, u, p): ...
        def select(self, box): ...
        def search(self, charset, criterion):
            return "OK", [b"1 2"]

        def fetch(self, num, part):
            return "OK", [(b"header", raws[num])]

        def store(self, num, flags, value):
            self.stored.append(num)

        def logout(self): ...

    captured = {}

    def fake_imap(host, port):
        captured["imap"] = FakeIMAP(host, port)
        return captured["imap"]

    monkeypatch.setattr(inbound.imaplib, "IMAP4_SSL", fake_imap)

    def handler(msg):
        return "processed" if msg.sender == "compta@fairmoove.fr" else "skipped"

    summary = inbound.poll_inbox("imap.test", 993, "u", "p", handler)

    assert summary["seen"] == 2
    assert summary["processed"] == 1
    assert summary["skipped"] == 1
    assert captured["imap"].stored == [b"1", b"2"]


# --- endpoints ----------------------------------------------------------------

def test_poll_endpoint_exige_le_bon_jeton(registry, monkeypatch):
    monkeypatch.setattr(main.inbound, "poll_inbox", lambda *a, **k: {"seen": 0})
    api = TestClient(main.app)

    assert api.post("/poll-inbox", params={"token": "faux"}).status_code == 401
    ok = api.post("/poll-inbox", params={"token": "secret-poll"})
    assert ok.status_code == 200
    assert ok.json()["seen"] == 0


def test_cron_entrypoint_lit_le_jeton_dans_le_corps(registry, monkeypatch):
    monkeypatch.setattr(main.inbound, "poll_inbox", lambda *a, **k: {"seen": 5})
    api = TestClient(main.app)

    r = api.post("/", json={"token": "secret-poll"})
    assert r.status_code == 200
    assert r.json()["seen"] == 5


def test_echec_de_releve_remonte_une_raison_lisible(registry, monkeypatch):
    def boom(*a, **k):
        raise OSError("IMAP: authentification refusée")

    monkeypatch.setattr(main.inbound, "poll_inbox", boom)
    api = TestClient(main.app, raise_server_exceptions=False)

    r = api.post("/poll-inbox", params={"token": "secret-poll"})
    assert r.status_code == 502
    assert "authentification refusée" in r.json()["detail"]
