"""Identification des deux exports FMS par leur contenu.

L'ordre et les noms de fichiers n'ont aucune importance : le fichier « tous
documents » se reconnaît à ses onglets Factures/Avoirs, le fichier « base
clients » à ses colonnes (Raison sociale, Adresse…).
"""

from __future__ import annotations

import io

import pandas as pd

from app.transform import CLIENT_REQUIRED_SOURCE

EXCEL_SUFFIXES = (".xlsx", ".xls")


def _looks_like_documents(content: bytes) -> bool:
    try:
        sheets = pd.ExcelFile(io.BytesIO(content)).sheet_names
    except Exception:  # noqa: BLE001 - classeur illisible => pas ce fichier
        return False
    return any(s in ("Factures", "Avoirs") for s in sheets)


def _looks_like_clients(content: bytes) -> bool:
    try:
        columns = pd.read_excel(io.BytesIO(content), dtype=str, nrows=0).columns
    except Exception:  # noqa: BLE001
        return False
    return set(CLIENT_REQUIRED_SOURCE).issubset(set(columns))


def identify_files(files: dict[str, bytes]) -> tuple[bytes, bytes, str, str]:
    """Renvoie (clients, documents, nom_clients, nom_documents).

    Lève ValueError si on ne trouve pas exactement un fichier de chaque type.
    """
    xlsx = {n: b for n, b in files.items() if n.lower().endswith(EXCEL_SUFFIXES)}
    if len(xlsx) != 2:
        raise ValueError(
            f"Il faut exactement 2 fichiers Excel (.xlsx) — reçu : {len(xlsx)}."
        )

    clients = documents = None
    clients_name = documents_name = ""
    for name, content in xlsx.items():
        if _looks_like_documents(content):
            documents, documents_name = content, name
        elif _looks_like_clients(content):
            clients, clients_name = content, name

    if clients is None or documents is None:
        raise ValueError(
            "Impossible d'identifier les fichiers : il faut un export « base clients » "
            "(colonnes Raison sociale, Adresse…) et un export « tous documents » "
            "(onglets Factures/Avoirs)."
        )
    return clients, documents, clients_name, documents_name
