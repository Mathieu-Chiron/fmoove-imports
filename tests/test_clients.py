"""Tests des fonctions pures de l'outil multi-clients (scripts/clients.py)."""

import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "clients", pathlib.Path(__file__).parents[1] / "scripts" / "clients.py"
)
clients = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clients)


def test_parse_env_tolere_commentaires_espaces_et_guillemets():
    env = clients.parse_env(
        '# commentaire\n'
        'APP_USER = fairmoove \n'
        'APP_PASSWORD="a&b c"\n'
        '\n'
        'PAYT_SFTP_PASSWORD=\'tok\'\n'
    )
    assert env == {
        "APP_USER": "fairmoove",
        "APP_PASSWORD": "a&b c",
        "PAYT_SFTP_PASSWORD": "tok",
    }


def test_validate_client_name_accepte_un_nom_valide():
    clients.validate_client_name("acme-2")  # ne lève pas


@pytest.mark.parametrize("bad", ["ACME", "a b", "é", "", "-x", "trop/lent"])
def test_validate_client_name_rejette_les_noms_invalides(bad):
    with pytest.raises(SystemExit):
        clients.validate_client_name(bad)
