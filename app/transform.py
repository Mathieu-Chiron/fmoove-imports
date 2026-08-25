"""Fusion des deux exports FMS vers le CSV d'import Payt.

Entrée  : le classeur « base clients » et le classeur « tous documents ».
Sortie  : le texte CSV prêt à déposer chez Payt, plus un rapport de contrôle.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

# Colonnes du CSV Payt, dans l'ordre. Les obligatoires d'abord.
CSV_COLUMNS = [
    "administration_code",
    "debtor_code",
    "debtor_company_name",
    "debtor_is_company",
    "debtor_email",
    "debtor_post_street_1",
    "debtor_post_street_2",
    "debtor_post_postalcode",
    "debtor_post_city",
    "debtor_post_country_code",
    "debtor_language_code",
    "invoice_number",
    "invoice_description",
    "invoice_date",
    "invoice_due_date",
    "invoice_total_amount_inc_vat",
    "invoice_open_amount_inc_vat",
    "invoice_currency_code",
]

REQUIRED_COLUMNS = [
    "administration_code",
    "debtor_code",
    "debtor_company_name",
    "debtor_post_street_1",
    "debtor_post_postalcode",
    "debtor_post_city",
    "debtor_post_country_code",
    "invoice_number",
    "invoice_date",
    "invoice_due_date",
    "invoice_total_amount_inc_vat",
    "invoice_open_amount_inc_vat",
]

CLIENT_REQUIRED_SOURCE = [
    "Référence", "Raison sociale", "Adresse", "Code postal", "Ville", "Pays",
]
DOC_REQUIRED_SOURCE = [
    "Référence", "Client", "Date", "Echéance", "Etat", "Montant TTC", "Montant dû",
]

COUNTRIES = {
    "FRANCE": "FR", "BELGIQUE": "BE", "ESPAGNE": "ES", "PORTUGAL": "PT",
    "PAYS-BAS": "NL", "LUXEMBOURG": "LU", "SUISSE": "CH", "ALLEMAGNE": "DE",
    "ITALIE": "IT", "GRECE": "GR", "AUTRICHE": "AT", "ROYAUME-UNI": "GB",
    "IRLANDE": "IE", "POLOGNE": "PL", "DANEMARK": "DK", "SUEDE": "SE",
}

# Délais en jours ; le tuple ("eom", n) signifie « n jours puis fin de mois ».
PAYMENT_TERMS = {
    "IMMEDIAT": 0,
    "A RECEPTION DE LA FACTURE": 0,
    "30 JOURS": 30,
    "60 JOURS": 60,
    "90 JOURS": 90,
    "30 JOURS FIN DE MOIS": ("eom", 30),
    "60 JOURS FIN DE MOIS": ("eom", 60),
}
DEFAULT_TERM_DAYS = 30  # « Plan de paiement », « A convenir », valeur absente

VOLUME_DROP_THRESHOLD = 0.30


@dataclass
class Report:
    """Résultat des contrôles. `blocking` non vide interdit l'envoi."""

    row_count: int = 0
    debtor_count: int = 0
    total_open_amount: float = 0.0
    missing_emails: int = 0
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking)


def normalize(value) -> str:
    """Clé de rapprochement : sans accents, sans casse, espaces normalisés."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text).strip().upper()


def _clean(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text in {"nan", "NaT", "None"} else text


def _due_date(invoice_date, term):
    key = normalize(term)
    rule = PAYMENT_TERMS.get(key, DEFAULT_TERM_DAYS)
    if isinstance(rule, tuple):
        shifted = invoice_date + timedelta(days=rule[1])
        return (shifted + pd.offsets.MonthEnd(0)).normalize()
    return invoice_date + timedelta(days=rule)


def _require_columns(df: pd.DataFrame, expected: list[str], label: str) -> None:
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise ValueError(
            f"Le fichier « {label} » ne ressemble pas à un export FMS : "
            f"colonne(s) introuvable(s) : {', '.join(missing)}."
        )


def _read_documents(content: bytes) -> pd.DataFrame:
    book = pd.ExcelFile(io.BytesIO(content))
    frames = []
    for sheet in ("Factures", "Avoirs"):
        if sheet not in book.sheet_names:
            continue
        df = pd.read_excel(book, sheet)
        df = df.loc[:, ~df.columns.astype(str).str.endswith(".1")]
        if not df.empty:
            frames.append(df)
    if not frames:
        raise ValueError(
            "Le fichier « tous documents » ne contient ni onglet Factures ni onglet Avoirs."
        )
    return pd.concat(frames, ignore_index=True)


def build_export(
    clients_file: bytes,
    documents_file: bytes,
    administration_code: str,
    previous_row_count: int | None = None,
) -> tuple[str, Report]:
    report = Report()

    clients = pd.read_excel(io.BytesIO(clients_file), dtype=str)
    _require_columns(clients, CLIENT_REQUIRED_SOURCE, "base clients")

    documents = _read_documents(documents_file)
    _require_columns(documents, DOC_REQUIRED_SOURCE, "tous documents")

    clients["_key"] = clients["Raison sociale"].map(normalize)
    duplicated = sorted(set(clients.loc[clients["_key"].duplicated(keep=False), "_key"]))
    unique_clients = clients.drop_duplicates("_key", keep="first")

    documents["Montant dû"] = pd.to_numeric(documents["Montant dû"], errors="coerce").fillna(0)
    documents["Montant TTC"] = pd.to_numeric(documents["Montant TTC"], errors="coerce").fillna(0)
    open_docs = documents[
        (documents["Etat"].map(normalize) != "BROUILLON") & (documents["Montant dû"] != 0)
    ].copy()
    open_docs["Date"] = pd.to_datetime(open_docs["Date"], format="%d/%m/%Y", errors="coerce")
    open_docs["_key"] = open_docs["Client"].map(normalize)

    orphans = sorted(set(open_docs["_key"]) - set(unique_clients["_key"]))
    if orphans:
        shown = ", ".join(orphans[:5])
        suffix = f" (et {len(orphans) - 5} autre(s))" if len(orphans) > 5 else ""
        report.blocking.append(
            f"Facture(s) rattachée(s) à un client absent de la base : {shown}{suffix}."
        )

    merged = open_docs.merge(unique_clients, on="_key", how="left", suffixes=("_doc", "_cli"))

    export = pd.DataFrame({
        "administration_code": administration_code,
        "debtor_code": merged.get("Référence_cli"),
        "debtor_company_name": merged.get("Raison sociale"),
        "debtor_is_company": merged.get("Type", pd.Series(dtype=str)).map(
            lambda v: "false" if normalize(v) == "PARTICULIER" else "true"
        ),
        "debtor_email": merged.get("Email principale"),
        "debtor_post_street_1": merged.get("Adresse"),
        "debtor_post_street_2": merged.get("Complément d'adresse"),
        "debtor_post_postalcode": merged.get("Code postal", pd.Series(dtype=str)).map(
            lambda v: re.sub(r"\s+", "", _clean(v))
        ),
        "debtor_post_city": merged.get("Ville"),
        "debtor_post_country_code": merged.get("Pays", pd.Series(dtype=str)).map(
            lambda v: COUNTRIES.get(normalize(v), "")
        ),
        "debtor_language_code": "fr",
        "invoice_number": merged.get("Référence_doc"),
        "invoice_description": merged.get("Objet"),
        "invoice_date": merged["Date"].dt.strftime("%Y-%m-%d"),
        "invoice_due_date": [
            "" if pd.isna(d) else _due_date(d, t).strftime("%Y-%m-%d")
            for d, t in zip(merged["Date"], merged["Echéance"], strict=True)
        ],
        "invoice_total_amount_inc_vat": merged["Montant TTC"].map(lambda v: f"{v:.2f}"),
        "invoice_open_amount_inc_vat": merged["Montant dû"].map(lambda v: f"{v:.2f}"),
        "invoice_currency_code": "EUR",
    }, columns=CSV_COLUMNS)

    for column in CSV_COLUMNS:
        export[column] = export[column].map(_clean)

    report.row_count = len(export)
    report.debtor_count = export["debtor_code"].nunique()
    report.total_open_amount = round(
        sum(float(v) for v in export["invoice_open_amount_inc_vat"] if v), 2
    )
    report.missing_emails = int((export["debtor_email"] == "").sum())

    if report.row_count == 0:
        report.blocking.append(
            "Aucune facture ouverte : le fichier serait vide et solderait tout l'encours chez Payt."
        )

    for column in REQUIRED_COLUMNS:
        empty = int((export[column] == "").sum())
        if empty:
            report.blocking.append(f"{empty} ligne(s) sans valeur pour « {column} ».")

    invalid_postcodes = export[
        (export["debtor_post_country_code"] == "FR")
        & (~export["debtor_post_postalcode"].str.fullmatch(r"\d{5}"))
    ]
    if len(invalid_postcodes):
        report.blocking.append(
            f"{len(invalid_postcodes)} code(s) postal(aux) français invalide(s)."
        )

    used_duplicates = sorted(set(merged["_key"]) & set(duplicated))
    if used_duplicates:
        report.warnings.append(
            "Raison(s) sociale(s) en doublon dans la base clients, rattachement incertain : "
            + ", ".join(used_duplicates)
            + "."
        )

    if report.missing_emails:
        report.warnings.append(
            f"{report.missing_emails} ligne(s) sans email : relance par mail impossible."
        )

    if previous_row_count:
        drop = (previous_row_count - report.row_count) / previous_row_count
        if drop > VOLUME_DROP_THRESHOLD:
            report.warnings.append(
                f"Chute de volume de {drop:.0%} par rapport au dernier envoi "
                f"({previous_row_count} → {report.row_count} lignes)."
            )

    buffer = io.StringIO()
    # QUOTE_MINIMAL : on ne quote que les champs contenant le séparateur, un
    # guillemet ou un saut de ligne. En-têtes, montants et dates restent nus,
    # comme les exemples de la doc Payt — un import qui quote tout (y compris
    # l'en-tête) n'est pas reconnu par Payt et crée 0 enregistrement sans erreur.
    export.to_csv(
        buffer, index=False, sep=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n"
    )
    return buffer.getvalue(), report
