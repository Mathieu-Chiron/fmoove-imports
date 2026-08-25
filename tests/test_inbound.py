"""Tests de la réception par email, multi-clients.

Aucun serveur IMAP/SMTP réel : IMAP4_SSL est remplacé par un faux, et les envois
sont espionnés. On vérifie l'identification, le routage par client, l'isolation.
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
        imap_host="imap.test", imap_port=993,
        smtp_host="smtp.test", smtp_port=587,
        mailbox_user="fairmoove@md", mailbox_password="pw",
        from_addr="fairmoove@md",
        administration_code="FAIRMOOVE", api_token="tok", import_token="imp",
        allowed_senders={"compta@fairmoove.fr"},
    )
    base.update(over)
    return Tenant(**base)


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
        inbound.identify_files({"a.xlsx": cl, "b.xlsx": cl})


# --- parsing d'un email brut --------------------------------------------------

def test_parse_message_extrait_expediteur_et_pieces_jointes():
    cl, dc = workbooks([client()], [doc()])
    raw = _raw_email("Le Client <CLIENT@corp.test>", {"c.xlsx": cl, "d.xlsx": dc})

    msg = inbound.parse_message(raw)

    assert msg.sender == "client@corp.test"
    assert set(msg.attachments) == {"c.xlsx", "d.xlsx"}


# --- handler par client -------------------------------------------------------

@pytest.fixture
def spies(monkeypatch):
    calls = {"upload": [], "report": [], "error": []}
    monkeypatch.setattr(main, "upload_csv", lambda *a, **k: calls["upload"].append(k))
    monkeypatch.setattr(main.alerts, "send_report", lambda *a, **k: calls["report"].append(a))
    monkeypatch.setattr(main.alerts, "send_error", lambda *a, **k: calls["error"].append(a))
    monkeypatch.setattr(main.archive, "read_last_row_count", lambda k: None)
    monkeypatch.setattr(main.archive, "save_run", lambda *a, **k: None)
    return calls


def _email(sender, factures=None):
    cl, dc = workbooks([client()], factures or [doc()])
    return inbound.InboundEmail(
        sender=sender, subject="x", attachments={"a.xlsx": cl, "b.xlsx": dc}
    )


def test_expediteur_non_autorise_est_ignore(spies):
    status = main._handle_email(make_tenant(), _email("inconnu@ext.test"))

    assert status == "skipped"
    assert spies["upload"] == [] and spies["report"] == []


def test_email_valide_utilise_les_creds_payt_du_client(spies):
    tenant = make_tenant(administration_code="CLI2", api_token="T2", import_token="I2")

    status = main._handle_email(tenant, _email("compta@fairmoove.fr"))

    assert status == "processed"
    assert len(spies["upload"]) == 1
    sent_kwargs = spies["upload"][0]
    assert sent_kwargs["administration_code"] == "CLI2"
    assert sent_kwargs["api_token"] == "T2"
    assert sent_kwargs["import_token"] == "I2"
    assert spies["report"][0][1] == "compta@fairmoove.fr"  # réponse à l'expéditeur


def test_pieces_jointes_invalides_sont_rejetees(spies):
    cl, _ = workbooks([client()], [doc()])
    msg = inbound.InboundEmail("compta@fairmoove.fr", "x", {"seul.xlsx": cl})

    status = main._handle_email(make_tenant(), msg)

    assert status == "rejected"
    assert spies["upload"] == [] and spies["error"]


def test_controle_bloquant_repond_mais_n_envoie_rien(spies):
    status = main._handle_email(
        make_tenant(), _email("compta@fairmoove.fr", factures=[doc(Client="INCONNU")])
    )

    assert status == "processed"
    assert spies["upload"] == []
    assert spies["report"]


# --- boucle IMAP (poll_inbox) -------------------------------------------------

def test_poll_inbox_releve_marque_lu_et_compte(monkeypatch):
    cl, dc = workbooks([client()], [doc()])
    raws = {
        b"1": _raw_email("compta@fairmoove.fr", {"a.xlsx": cl, "b.xlsx": dc}),
        b"2": _raw_email("spam@ext.test", {"a.xlsx": cl, "b.xlsx": dc}),
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
    assert captured["imap"].stored == [b"1", b"2"]  # les deux marqués lus


# --- endpoints & isolation ----------------------------------------------------

@pytest.fixture
def two_tenants(monkeypatch):
    monkeypatch.setattr(settings, "poll_token", "secret-poll")
    monkeypatch.setattr(
        settings, "tenants",
        [
            make_tenant(name="Fairmoove", imap_host="imap.test"),
            make_tenant(name="Client2", imap_host="imap2.test", mailbox_user="c2@md"),
        ],
    )


def test_poll_endpoint_exige_le_bon_jeton(two_tenants, monkeypatch):
    monkeypatch.setattr(main.inbound, "poll_inbox", lambda *a, **k: {"seen": 0})
    api = TestClient(main.app)

    assert api.post("/poll-inbox", params={"token": "faux"}).status_code == 401
    ok = api.post("/poll-inbox", params={"token": "secret-poll"})
    assert ok.status_code == 200
    assert set(ok.json()) == {"Fairmoove", "Client2"}  # les 2 clients relevés


def test_un_client_en_echec_n_arrete_pas_les_autres(two_tenants, monkeypatch):
    def poll(host, *a, **k):
        if host == "imap.test":  # boîte de Fairmoove
            raise OSError("IMAP indisponible")
        return {"seen": 1, "processed": 1}

    monkeypatch.setattr(main.inbound, "poll_inbox", poll)
    api = TestClient(main.app)

    body = api.post("/poll-inbox", params={"token": "secret-poll"}).json()

    assert "error" in body["Fairmoove"]
    assert body["Client2"]["processed"] == 1


def test_cron_entrypoint_lit_le_jeton_dans_le_corps(two_tenants, monkeypatch):
    monkeypatch.setattr(main.inbound, "poll_inbox", lambda *a, **k: {"seen": 0})
    api = TestClient(main.app)

    r = api.post("/", json={"token": "secret-poll"})
    assert r.status_code == 200
