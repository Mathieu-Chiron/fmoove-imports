"""Tests de l'identification des 2 exports par leur contenu."""

import pytest

from app.detect import identify_files
from tests.test_transform import client, doc, workbooks


def test_identifie_quel_que_soit_l_ordre():
    cl, dc = workbooks([client()], [doc()])
    for atts in ({"a.xlsx": cl, "b.xlsx": dc}, {"z.xlsx": dc, "y.xlsx": cl}):
        clients_b, documents_b, _, _ = identify_files(atts)
        assert clients_b == cl
        assert documents_b == dc


def test_exige_deux_fichiers_excel():
    cl, _ = workbooks([client()], [doc()])
    with pytest.raises(ValueError, match="exactement 2"):
        identify_files({"a.xlsx": cl})


def test_ignore_les_pieces_jointes_non_excel():
    cl, dc = workbooks([client()], [doc()])
    # note.txt est ignoré : il reste bien 2 fichiers Excel identifiables.
    clients_b, documents_b, _, _ = identify_files(
        {"a.xlsx": cl, "b.xlsx": dc, "note.txt": b"x"}
    )
    assert clients_b == cl and documents_b == dc


def test_rejette_si_non_identifiables():
    cl, _ = workbooks([client()], [doc()])
    with pytest.raises(ValueError, match="identifier"):
        identify_files({"a.xlsx": cl, "b.xlsx": cl})
