#!/usr/bin/env python3
"""Gestion multi-clients : un conteneur Scaleway par client (même image).

Chaque client a un fichier ``~/fmp-clients/<client>.env`` (jamais dans le dépôt) :

    APP_USER=fairmoove
    APP_PASSWORD=<mot de passe fort — donné au client pour la page>
    PAYT_ADMINISTRATION_CODE=<code admin Payt du client>
    PAYT_API_TOKEN=<token statique du client>
    PAYT_IMPORT_TOKEN=<token d'importation du client>

Usage :
    python3 scripts/clients.py deploy <client> [--image web-1.4.1]
    python3 scripts/clients.py list

« deploy » crée le conteneur ``fairmoove-payt-<client>`` s'il n'existe pas, sinon
le met à jour, puis le déploie et affiche son URL. Les secrets sont stockés
chiffrés côté Scaleway ; le fichier local reste la source à conserver.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import time

SCW = "scw"
REGION = "fr-par"
# Namespace Serverless Containers (non sensible).
NAMESPACE_ID = "02f8aee6-fb45-486f-91f2-a79c75b6f729"
DEFAULT_IMAGE = "rg.fr-par.scw.cloud/fairmoove-payt/app:web-1.5.0"
CLIENTS_DIR = pathlib.Path.home() / "fmp-clients"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
REQUIRED = ("APP_PASSWORD", "PAYT_ADMINISTRATION_CODE", "PAYT_API_TOKEN", "PAYT_IMPORT_TOKEN")


def parse_env(text: str) -> dict[str, str]:
    """Parse un fichier KEY=VALUE (tolérant : commentaires, guillemets, espaces)."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def validate_client_name(name: str) -> None:
    if not NAME_RE.match(name):
        sys.exit(f"❌ nom de client invalide : « {name} » (minuscules, chiffres, tirets).")


def load_client(name: str) -> dict[str, str]:
    path = CLIENTS_DIR / f"{name}.env"
    if not path.exists():
        sys.exit(f"❌ fichier introuvable : {path}")
    env = parse_env(path.read_text())
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        sys.exit(f"❌ {path} : manque {', '.join(missing)}")
    if env["APP_PASSWORD"].lower() == "test" or len(env["APP_PASSWORD"]) < 8:
        sys.exit("❌ APP_PASSWORD trop faible (≥ 8 caractères, pas « test »).")
    env.setdefault("APP_USER", "fairmoove")
    return env


def _scw_json(*args: str):
    result = subprocess.run([SCW, *args, "-o", "json"], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("❌ scw : " + (result.stderr or "").strip()[:400])
    return json.loads(result.stdout) if result.stdout.strip() else None


def _find_container(name: str):
    items = _scw_json(
        "container", "container", "list", f"namespace-id={NAMESPACE_ID}", f"region={REGION}"
    ) or []
    return next((c for c in items if c.get("name") == name), None)


def deploy(client: str, image: str) -> None:
    validate_client_name(client)
    env = load_client(client)
    name = f"fairmoove-payt-{client}"
    config = [
        f"environment-variables.APP_USER={env['APP_USER']}",
        f"environment-variables.PAYT_ADMINISTRATION_CODE={env['PAYT_ADMINISTRATION_CODE']}",
        f"secret-environment-variables.APP_PASSWORD={env['APP_PASSWORD']}",
        f"secret-environment-variables.PAYT_API_TOKEN={env['PAYT_API_TOKEN']}",
        f"secret-environment-variables.PAYT_IMPORT_TOKEN={env['PAYT_IMPORT_TOKEN']}",
    ]
    # Proxy à IP statique (Fixie), optionnel — même valeur pour tous les clients.
    if env.get("PAYT_PROXY"):
        config.append(f"secret-environment-variables.PAYT_PROXY={env['PAYT_PROXY']}")
    config.append(f"region={REGION}")

    existing = _find_container(name)
    if existing:
        container_id = existing["id"]
        _scw_json("container", "container", "update", container_id, f"image={image}", *config)
        print(f"✔ conteneur mis à jour : {name}")
    else:
        created = _scw_json(
            "container", "container", "create",
            f"namespace-id={NAMESPACE_ID}", f"name={name}", f"image={image}",
            "port=8080", "min-scale=0", "max-scale=1",
            "memory-limit-bytes=1GB", "mvcpu-limit=1000", *config,
        )
        container_id = created["id"]
        print(f"✔ conteneur créé : {name}")

    subprocess.run(
        [SCW, "container", "container", "deploy", container_id, f"region={REGION}"],
        capture_output=True, text=True,
    )

    container = {}
    for _ in range(72):
        container = _scw_json("container", "container", "get", container_id, f"region={REGION}")
        if container.get("status") in ("ready", "error"):
            break
        time.sleep(5)

    print(f"   statut : {container.get('status')}")
    print(f"   URL    : {container.get('public_endpoint', '(en attente)')}")
    print(f"   login  : {env['APP_USER']} / (mot de passe du fichier {client}.env)")


def list_clients() -> None:
    items = _scw_json(
        "container", "container", "list", f"namespace-id={NAMESPACE_ID}", f"region={REGION}"
    ) or []
    rows = [c for c in items if c.get("name", "").startswith("fairmoove-payt")]
    if not rows:
        print("(aucun conteneur client)")
        return
    for c in sorted(rows, key=lambda c: c.get("name", "")):
        print(f"{c.get('name', ''):32} {c.get('status', ''):10} {c.get('public_endpoint', '')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gestion multi-clients Fairmoove → Payt")
    sub = parser.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deploy", help="crée/met à jour + déploie le conteneur d'un client")
    d.add_argument("client", help="nom court du client (minuscules/tirets)")
    d.add_argument("--image", default=DEFAULT_IMAGE, help="image à déployer")
    sub.add_parser("list", help="liste les conteneurs clients et leurs URLs")

    args = parser.parse_args()
    if args.cmd == "deploy":
        deploy(args.client, args.image)
    elif args.cmd == "list":
        list_clients()


if __name__ == "__main__":
    main()
