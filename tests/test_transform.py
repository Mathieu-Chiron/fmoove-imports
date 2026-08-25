"""Tests du moteur de fusion. Écrits avant l'implémentation (TDD).

Aucune donnée réelle ici : les classeurs sont générés à la volée pour éviter
de committer des données personnelles de débiteurs.
"""

import io

import pandas as pd
import pytest

from app.transform import build_export

CLIENT_COLS = [
    "Référence", "Type", "Raison sociale", "Email principale", "Adresse",
    "Complément d'adresse", "Code postal", "Ville", "Pays",
]

DOC_COLS = [
    "Référence", "Client", "Objet", "Date", "Echéance", "Etat",
    "Montant TTC", "Montant dû",
]


def client(**kw):
    base = {
        "Référence": "C-0001", "Type": "Entreprise", "Raison sociale": "ACME SARL",
        "Email principale": "compta@acme.fr", "Adresse": "1 rue des Lilas",
        "Complément d'adresse": None, "Code postal": "69001", "Ville": "LYON",
        "Pays": "France",
    }
    return {**base, **kw}


def doc(**kw):
    base = {
        "Référence": "F-26000001", "Client": "ACME SARL", "Objet": " Prestation",
        "Date": "19/08/2026", "Echéance": "30 jours", "Etat": "En retard",
        "Montant TTC": 1200.0, "Montant dû": 1200.0,
    }
    return {**base, **kw}


def workbooks(clients, factures, avoirs=None):
    """Sérialise les données en deux classeurs xlsx en mémoire."""
    cl = io.BytesIO()
    pd.DataFrame(clients, columns=CLIENT_COLS).to_excel(cl, index=False)

    dc = io.BytesIO()
    with pd.ExcelWriter(dc) as w:
        pd.DataFrame(factures, columns=DOC_COLS).to_excel(w, sheet_name="Factures", index=False)
        pd.DataFrame(avoirs or [], columns=DOC_COLS).to_excel(w, sheet_name="Avoirs", index=False)

    return cl.getvalue(), dc.getvalue()


def rows(csv_text):
    return list(pd.read_csv(io.StringIO(csv_text), sep=";", dtype=str).to_dict("records"))


# --- structure du CSV ---------------------------------------------------------

def test_produit_une_ligne_par_facture_ouverte():
    cl, dc = workbooks([client()], [doc()])
    csv_text, report = build_export(cl, dc, administration_code="FAIRMOOVE")

    assert len(rows(csv_text)) == 1
    assert report.blocking == []


def test_mappe_les_champs_obligatoires():
    cl, dc = workbooks([client()], [doc()])
    csv_text, _ = build_export(cl, dc, administration_code="FAIRMOOVE")
    r = rows(csv_text)[0]

    assert r["administration_code"] == "FAIRMOOVE"
    assert r["debtor_code"] == "C-0001"
    assert r["debtor_company_name"] == "ACME SARL"
    assert r["debtor_post_street_1"] == "1 rue des Lilas"
    assert r["debtor_post_postalcode"] == "69001"
    assert r["debtor_post_city"] == "LYON"
    assert r["debtor_post_country_code"] == "FR"
    assert r["invoice_number"] == "F-26000001"
    assert r["invoice_total_amount_inc_vat"] == "1200.00"
    assert r["invoice_open_amount_inc_vat"] == "1200.00"


def test_convertit_les_dates_en_iso():
    cl, dc = workbooks([client()], [doc(Date="19/08/2026")])
    r = rows(build_export(cl, dc, administration_code="X")[0])[0]

    assert r["invoice_date"] == "2026-08-19"


@pytest.mark.parametrize(
    "echeance,attendu",
    [
        ("30 jours", "2026-09-18"),
        ("60 jours", "2026-10-18"),
        ("A réception de la facture", "2026-08-19"),
        ("Immédiat", "2026-08-19"),
        ("60 jours fin de mois", "2026-10-31"),
        ("Plan de paiement", "2026-09-18"),  # repli sur 30 jours
    ],
)
def test_calcule_la_date_decheance(echeance, attendu):
    cl, dc = workbooks([client()], [doc(Date="19/08/2026", Echéance=echeance)])
    r = rows(build_export(cl, dc, administration_code="X")[0])[0]

    assert r["invoice_due_date"] == attendu


def test_nettoie_les_codes_postaux_et_les_espaces_parasites():
    cl, dc = workbooks([client(**{"Code postal": "68 000"})], [doc(Objet="  Audit  ")])
    r = rows(build_export(cl, dc, administration_code="X")[0])[0]

    assert r["debtor_post_postalcode"] == "68000"
    assert r["invoice_description"] == "Audit"


def test_traduit_les_pays_en_iso():
    cl, dc = workbooks([client(Pays="Suisse", **{"Code postal": "3823"})], [doc()])
    r = rows(build_export(cl, dc, administration_code="X")[0])[0]

    assert r["debtor_post_country_code"] == "CH"


# --- périmètre des documents --------------------------------------------------

def test_exclut_les_factures_soldees_et_les_brouillons():
    cl, dc = workbooks(
        [client()],
        [
            doc(Référence="F-1", Etat="Réglé", **{"Montant dû": 0.0}),
            doc(Référence="F-2", Etat="Brouillon"),
            doc(Référence="F-3", Etat="A régler"),
        ],
    )
    csv_text, _ = build_export(cl, dc, administration_code="X")

    assert [r["invoice_number"] for r in rows(csv_text)] == ["F-3"]


def test_inclut_les_avoirs_en_negatif():
    cl, dc = workbooks(
        [client()],
        [doc()],
        avoirs=[doc(Référence="AV-26000001", **{"Montant TTC": -300.0, "Montant dû": -300.0})],
    )
    csv_text, _ = build_export(cl, dc, administration_code="X")
    avoir = next(r for r in rows(csv_text) if r["invoice_number"] == "AV-26000001")

    assert avoir["invoice_open_amount_inc_vat"] == "-300.00"


# --- contrôles bloquants ------------------------------------------------------

def test_bloque_si_aucune_ligne():
    cl, dc = workbooks([client()], [doc(Etat="Réglé", **{"Montant dû": 0.0})])
    _, report = build_export(cl, dc, administration_code="X")

    assert report.is_blocked
    assert any("vide" in m.lower() for m in report.blocking)


def test_bloque_si_facture_orpheline():
    cl, dc = workbooks([client()], [doc(Client="SOCIETE INCONNUE")])
    _, report = build_export(cl, dc, administration_code="X")

    assert report.is_blocked
    assert any("SOCIETE INCONNUE" in m for m in report.blocking)


def test_bloque_si_champ_obligatoire_manquant():
    cl, dc = workbooks([client(Ville=None)], [doc()])
    _, report = build_export(cl, dc, administration_code="X")

    assert report.is_blocked
    assert any("debtor_post_city" in m for m in report.blocking)


def test_bloque_si_colonne_attendue_absente():
    cl = io.BytesIO()
    pd.DataFrame([{"Nom": "ACME"}]).to_excel(cl, index=False)
    _, dc = workbooks([client()], [doc()])

    with pytest.raises(ValueError, match="colonne"):
        build_export(cl.getvalue(), dc, administration_code="X")


# --- avertissements -----------------------------------------------------------

def test_signale_les_homonymes_sans_bloquer():
    cl, dc = workbooks(
        [client(Référence="C-0001"), client(Référence="C-0002", Adresse="2 rue Neuve")],
        [doc()],
    )
    _, report = build_export(cl, dc, administration_code="X")

    assert not report.is_blocked
    assert any("ACME SARL" in m for m in report.warnings)


def test_signale_les_emails_manquants_sans_bloquer():
    cl, dc = workbooks([client(**{"Email principale": None})], [doc()])
    _, report = build_export(cl, dc, administration_code="X")

    assert not report.is_blocked
    assert report.missing_emails == 1


def test_signale_une_chute_de_volume():
    cl, dc = workbooks([client()], [doc()])
    _, report = build_export(cl, dc, administration_code="X", previous_row_count=10)

    assert not report.is_blocked
    assert any("volume" in m.lower() for m in report.warnings)
