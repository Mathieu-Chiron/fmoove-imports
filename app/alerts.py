"""Envoi d'un récapitulatif par mail après un dépôt.

Best-effort, comme l'archivage : si le SMTP n'est pas configuré ou tombe, on
journalise sans faire échouer la requête. Le mail part surtout pour porter les
avertissements (emails manquants, homonymes, chute de volume) ou signaler un
échec d'envoi à l'API Payt à l'équipe.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.settings import settings
from app.transform import Report

logger = logging.getLogger(__name__)


def _body(report: Report, filename: str, error: str | None) -> str:
    lines = [
        f"Fichier : {filename}",
        f"Lignes : {report.row_count}",
        f"Débiteurs : {report.debtor_count}",
        f"Encours total : {report.total_open_amount:.2f} EUR",
        "",
    ]
    if error:
        lines += ["⚠️ Envoi à l'API Payt en échec :", f"  {error}", ""]
    else:
        lines += ["Envoi à l'API Payt : OK", ""]
    if report.warnings:
        lines.append("Avertissements :")
        lines += [f"  - {w}" for w in report.warnings]
        lines.append("")
    lines.append(
        "Rappel : Payt relève les fichiers une fois par jour (~01h00). "
        "Vérifier l'onglet Import de Payt le lendemain."
    )
    return "\n".join(lines)


def send_summary(report: Report, filename: str, error: str | None) -> None:
    """Envoie le récapitulatif si le SMTP est configuré. Ne lève jamais."""
    if not settings.alerts_enabled:
        logger.info("Alerte mail non configurée : récapitulatif non envoyé")
        return

    status = "ÉCHEC dépôt" if error else ("avec avertissements" if report.warnings else "OK")
    message = EmailMessage()
    message["Subject"] = f"[Fairmoove → Payt] {filename} — {status}"
    message["From"] = settings.alert_from
    message["To"] = settings.alert_to
    message.set_content(_body(report, filename, error))

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
        logger.info("Récapitulatif envoyé à %s", settings.alert_to)
    except Exception:  # noqa: BLE001 - l'alerte ne doit jamais casser un envoi
        logger.exception("Envoi du récapitulatif en échec")
