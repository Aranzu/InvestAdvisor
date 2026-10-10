"""Notificacion opcional de la corrida nocturna (spec v3.1, fase 8).

El envio es **best-effort**: un fallo de red no puede tumbar una corrida que ya
escribio sus informes. Canales, todos opcionales y activados por entorno o por
``.secrets/`` (ignorado por git):

- **Webhook generico:** ``SWING_NOTIFY_WEBHOOK`` (POST JSON ``{title, body}``).
- **Telegram:** ``SWING_TELEGRAM_BOT_TOKEN`` + ``SWING_TELEGRAM_CHAT_ID``.

Sin ningun canal configurado la corrida nocturna igual escribe su resumen a disco;
solo no manda nada. El ``transport`` es inyectable para testear el payload sin red.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

#: Carpeta de respaldo local de secretos. No se versiona (ver ``.gitignore``).
SECRETS_DIR = Path(__file__).resolve().parents[3] / ".secrets"

WEBHOOK_SECRET_FILE = SECRETS_DIR / "notify_webhook.txt"
TELEGRAM_TOKEN_SECRET_FILE = SECRETS_DIR / "telegram_token.txt"
TELEGRAM_CHAT_SECRET_FILE = SECRETS_DIR / "telegram_chat_id.txt"

#: Base de la API de bots de Telegram.
TELEGRAM_API = "https://api.telegram.org"


class NotifyError(RuntimeError):
    """Fallo al enviar una notificacion. El llamador sigue con los demas canales."""


@dataclass(frozen=True, slots=True)
class Notification:
    """Mensaje a notificar: un titulo corto y un cuerpo (Markdown plano)."""

    title: str
    body: str


class Notifier(Protocol):
    """Canal de notificacion."""

    def send(self, notification: Notification) -> None: ...


#: Transporte inyectable: recibe la URL y el body JSON, y no devuelve nada.
Transport = Callable[[str, dict[str, object]], None]


def _post_json(url: str, payload: dict[str, object]) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except (urllib.error.URLError, OSError) as exc:
        raise NotifyError(f"fallo de red al notificar: {exc}") from exc


def _secret(env_name: str, path: Path) -> str | None:
    """Valor desde el entorno o, como ultimo recurso, desde el archivo local."""
    value = os.environ.get(env_name)
    if value and value.strip():
        return value.strip()
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    return None


@dataclass(slots=True)
class WebhookNotifier:
    """POST JSON generico; sirve para Slack/Discord/n8n y similares."""

    url: str = field(repr=False)
    transport: Transport = _post_json

    def send(self, notification: Notification) -> None:
        self.transport(self.url, {"title": notification.title, "body": notification.body})


@dataclass(slots=True)
class TelegramNotifier:
    """Mensaje a un chat de Telegram via bot."""

    token: str = field(repr=False)
    chat_id: str
    transport: Transport = _post_json

    def send(self, notification: Notification) -> None:
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        text = f"{notification.title}\n\n{notification.body}"
        self.transport(
            url,
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )


def notifiers_from_env(*, transport: Transport | None = None) -> list[Notifier]:
    """Canales configurados. Lista vacia si no hay ninguno (no es un error)."""
    post = transport if transport is not None else _post_json
    notifiers: list[Notifier] = []
    webhook = _secret("SWING_NOTIFY_WEBHOOK", WEBHOOK_SECRET_FILE)
    if webhook:
        notifiers.append(WebhookNotifier(url=webhook, transport=post))
    token = _secret("SWING_TELEGRAM_BOT_TOKEN", TELEGRAM_TOKEN_SECRET_FILE)
    chat_id = _secret("SWING_TELEGRAM_CHAT_ID", TELEGRAM_CHAT_SECRET_FILE)
    if token and chat_id:
        notifiers.append(TelegramNotifier(token=token, chat_id=chat_id, transport=post))
    return notifiers


def send_all(notifiers: Sequence[Notifier], notification: Notification) -> list[str]:
    """Envia por todos los canales y devuelve los errores; nunca levanta."""
    errors: list[str] = []
    for notifier in notifiers:
        try:
            notifier.send(notification)
        except NotifyError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(f"fallo inesperado al notificar: {exc}")
    return errors
