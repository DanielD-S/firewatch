"""Canales de notificación: consola, Telegram, WhatsApp (Meta Cloud API) y webhook.

Las credenciales se leen de variables de entorno nombradas en config.json.
Un canal sin credenciales queda desactivado sin error.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import requests

from .alerts import Alert

log = logging.getLogger(__name__)


class Notifier:
    name = "base"

    def send(self, alert: Alert) -> bool:  # pragma: no cover - interfaz
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, alert: Alert) -> bool:
        print("=" * 72)
        print(alert.message)
        print("=" * 72)
        return True


@dataclass
class TelegramNotifier(Notifier):
    token: str
    chat_id: str
    name = "telegram"

    def send(self, alert: Alert) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        r = requests.post(url, json={"chat_id": self.chat_id, "text": alert.message, "disable_web_page_preview": True}, timeout=30)
        if r.status_code != 200:
            log.warning("Telegram respondió %s: %s", r.status_code, r.text[:200])
            return False
        return True


@dataclass
class WhatsAppNotifier(Notifier):
    """WhatsApp Business Cloud API (Meta). Requiere número de prueba o
    aprobado y, fuera de la ventana de 24 h, una plantilla aprobada."""
    token: str
    phone_id: str
    to: str
    name = "whatsapp"

    def send(self, alert: Alert) -> bool:
        url = f"https://graph.facebook.com/v20.0/{self.phone_id}/messages"
        ok = True
        for dest in [d.strip() for d in self.to.split(",") if d.strip()]:
            body = {"messaging_product": "whatsapp", "to": dest, "type": "text",
                    "text": {"preview_url": False, "body": alert.message[:4000]}}
            r = requests.post(url, headers={"Authorization": f"Bearer {self.token}"}, json=body, timeout=30)
            if r.status_code >= 300:
                log.warning("WhatsApp respondió %s: %s", r.status_code, r.text[:200])
                ok = False
        return ok


@dataclass
class WebhookNotifier(Notifier):
    url: str
    name = "webhook"

    def send(self, alert: Alert) -> bool:
        payload = {"id": alert.id, "sector_id": alert.sector_id, "sector_name": alert.sector_name,
                   "level": alert.level, "title": alert.title, "message": alert.message, "evidence": alert.evidence}
        r = requests.post(self.url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, timeout=30)
        if r.status_code >= 300:
            log.warning("Webhook respondió %s", r.status_code)
            return False
        return True


def build_notifiers(cfg_notify: dict) -> list[Notifier]:
    out: list[Notifier] = []
    if cfg_notify.get("console", True):
        out.append(ConsoleNotifier())
    tg = cfg_notify.get("telegram", {})
    if tg.get("enabled"):
        tok, chat = os.environ.get(tg.get("token_env", ""), ""), os.environ.get(tg.get("chat_id_env", ""), "")
        if tok and chat:
            out.append(TelegramNotifier(tok, chat))
    wa = cfg_notify.get("whatsapp", {})
    if wa.get("enabled"):
        tok = os.environ.get(wa.get("token_env", ""), "")
        pid = os.environ.get(wa.get("phone_id_env", ""), "")
        to = os.environ.get(wa.get("to_env", ""), "")
        if tok and pid and to:
            out.append(WhatsAppNotifier(tok, pid, to))
    wh = cfg_notify.get("webhook", {})
    if wh.get("enabled"):
        url = os.environ.get(wh.get("url_env", ""), "")
        if url:
            out.append(WebhookNotifier(url))
    return out


def dispatch(alerts: list[Alert], notifiers: list[Notifier], store=None) -> None:
    for a in alerts:
        any_ok = False
        for n in notifiers:
            try:
                if n.send(a):
                    any_ok = True
            except Exception as e:  # noqa: BLE001
                log.warning("Notificador %s falló: %s", n.name, e)
        if any_ok and store is not None:
            store.mark_notified(a.id)
