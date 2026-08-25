"""Envoi par email du rapport d'un import (best-effort).

Deux usages : notifier l'équipe (`ALERT_TO`) après un envoi via la page web, et
répondre à l'expéditeur d'un email d'import. Best-effort : si le SMTP n'est pas
configuré ou tombe, on journalise sans faire échouer le traitement.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.settings import settings
from app.transform import Report

logger = logging.getLogger(__name__)


def _status_label(report: Report, error: str | None, sent: bool) -> str:
    if report.is_blocked:
        return "BLOQUÉ — rien envoyé"
    if error:
        return "ÉCHEC envoi"
    if report.warnings:
        return "envoyé (avertissements)"
    return "envoyé" if sent else "non envoyé"


def _body(report: Report, filename: str, error: str | None, sent: bool) -> str:
    lines = [
        f"Fichier : {filename}",
        f"Lignes : {report.row_count}",
        f"Débiteurs : {report.debtor_count}",
        f"Encours total : {report.total_open_amount:.2f} EUR",
        "",
    ]
    if report.is_blocked:
        lines.append("⚠️ RIEN N'A ÉTÉ ENVOYÉ — contrôles bloquants :")
        lines += [f"  - {m}" for m in report.blocking]
        lines.append("")
    elif error:
        lines += ["⚠️ Envoi à l'API Payt en échec :", f"  {error}", ""]
    elif sent:
        lines += ["Envoi à l'API Payt : OK", ""]
    if report.warnings:
        lines.append("Avertissements :")
        lines += [f"  - {w}" for w in report.warnings]
        lines.append("")
    lines.append(
        "Rappel : Payt traite les imports une fois par jour (~01h00). "
        "Vérifier l'onglet Import de Payt le lendemain."
    )
    return "\n".join(lines)


def _send(to: str, subject: str, body: str) -> bool:
    if not settings.replies_enabled:
        logger.info("SMTP non configuré : email « %s » non envoyé", subject)
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.alert_from
    message["To"] = to
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)
        logger.info("Email envoyé à %s", to)
        return True
    except Exception:  # noqa: BLE001 - l'email ne doit jamais casser le traitement
        logger.exception("Envoi de l'email à %s en échec", to)
        return False


def send_report(
    to: str, report: Report, filename: str, error: str | None, sent: bool
) -> None:
    """Envoie le rapport d'import à `to`. Ne lève jamais."""
    subject = f"[Fairmoove → Payt] {filename} — {_status_label(report, error, sent)}"
    _send(to, subject, _body(report, filename, error, sent))


def send_error(to: str, filename: str, message: str) -> None:
    """Signale un email d'import inexploitable (pièces jointes, format…)."""
    _send(to, f"[Fairmoove → Payt] {filename} — REJETÉ", message)


def send_summary(report: Report, filename: str, error: str | None) -> None:
    """Notifie l'équipe (ALERT_TO) après un envoi via la page web. Ne lève jamais."""
    if not settings.alerts_enabled:
        logger.info("Alerte mail non configurée : récapitulatif non envoyé")
        return
    send_report(settings.alert_to, report, filename, error, sent=error is None)
