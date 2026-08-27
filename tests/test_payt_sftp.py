"""Tests de la livraison SFTP vers Payt (paramiko simulé)."""

import pytest

from app import payt_sftp
from app.payt_sftp import PaytUploadError, upload_csv


class FakeSFTP:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.actions: list[tuple] = []

    def putfo(self, fo, path, confirm=False):
        self.files[path] = fo.read()
        self.actions.append(("put", path))

    def remove(self, path):
        if path not in self.files:
            raise OSError("absent")
        del self.files[path]
        self.actions.append(("remove", path))

    def rename(self, src, dst):
        self.files[dst] = self.files.pop(src)
        self.actions.append(("rename", src, dst))

    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakeClient:
    def __init__(self, sftp, fail_connect=False):
        self._sftp = sftp
        self.fail = fail_connect
        self.closed = False

    def set_missing_host_key_policy(self, policy): ...
    def connect(self, **kw):
        if self.fail:
            raise OSError("connexion refusée")
    def open_sftp(self): return self._sftp
    def close(self): self.closed = True


def _patch(monkeypatch, client):
    monkeypatch.setattr(payt_sftp.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(payt_sftp.paramiko, "AutoAddPolicy", lambda: None)


def _args():
    return dict(host="h", port=22, username="u", password="p", remote_dir="in")


def test_depose_sous_nom_temporaire_puis_renomme(monkeypatch):
    sftp = FakeSFTP()
    _patch(monkeypatch, FakeClient(sftp))

    path = upload_csv("col;col\r\n", "20260827.csv", **_args())

    assert path == "in/20260827.csv"
    assert ("put", "in/20260827.csv.part") in sftp.actions  # écrit d'abord en .part
    assert ("rename", "in/20260827.csv.part", "in/20260827.csv") in sftp.actions
    assert sftp.files["in/20260827.csv"].decode() == "col;col\r\n"


def test_remplace_l_ancien_fichier_s_il_existe(monkeypatch):
    sftp = FakeSFTP()
    sftp.files["in/20260827.csv"] = b"vieux"
    _patch(monkeypatch, FakeClient(sftp))

    upload_csv("neuf", "20260827.csv", **_args())

    assert sftp.files["in/20260827.csv"] == b"neuf"


def test_erreur_de_connexion_leve_paytuploaderror(monkeypatch):
    client = FakeClient(FakeSFTP(), fail_connect=True)
    _patch(monkeypatch, client)

    with pytest.raises(PaytUploadError, match="SFTP"):
        upload_csv("x", "f.csv", **_args())

    assert client.closed  # la connexion est bien refermée
