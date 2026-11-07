"""Notificacion opcional de la corrida nocturna (fase 8).

El payload se prueba con un transporte inyectado: los tests no tocan la red. La
regla que se fija es que un fallo de canal se reporta y **nunca** levanta, porque
la corrida ya escribio sus informes.
"""

from __future__ import annotations

from swing.adapters.notify import (
    Notification,
    TelegramNotifier,
    WebhookNotifier,
    notifiers_from_env,
    send_all,
)

#: Variables de entorno de los dos canales.
ENV_VARS = ("SWING_NOTIFY_WEBHOOK", "SWING_TELEGRAM_BOT_TOKEN", "SWING_TELEGRAM_CHAT_ID")


def _clear_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class _Transport:
    """Transporte falso: guarda las llamadas (url, payload)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, payload: dict[str, object]) -> None:
        self.calls.append((url, payload))


def test_webhook_manda_title_y_body(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("SWING_NOTIFY_WEBHOOK", "https://example.com/hook")
    transport = _Transport()
    notifiers = notifiers_from_env(transport=transport)
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], WebhookNotifier)
    errors = send_all(notifiers, Notification(title="Corrida", body="hola"))
    assert errors == []
    url, payload = transport.calls[0]
    assert url == "https://example.com/hook"
    assert payload == {"title": "Corrida", "body": "hola"}


def test_telegram_arma_la_url_del_bot_y_junta_el_titulo(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("SWING_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("SWING_TELEGRAM_CHAT_ID", "999")
    transport = _Transport()
    notifiers = notifiers_from_env(transport=transport)
    assert len(notifiers) == 1
    assert isinstance(notifiers[0], TelegramNotifier)
    send_all(notifiers, Notification(title="Corrida", body="hola"))
    url, payload = transport.calls[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert payload["chat_id"] == "999"
    assert payload["text"] == "Corrida\n\nhola"


def test_sin_canales_no_es_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    transport = _Transport()
    assert notifiers_from_env(transport=transport) == []
    assert send_all([], Notification(title="x", body="y")) == []


def test_un_canal_que_falla_no_tumba_el_resto(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("SWING_NOTIFY_WEBHOOK", "https://example.com/hook")

    def _boom(url: str, payload: dict[str, object]) -> None:
        raise OSError("sin red")

    notifiers = notifiers_from_env(transport=_boom)
    errors = send_all(notifiers, Notification(title="Corrida", body="hola"))
    assert len(errors) == 1
    assert "sin red" in errors[0]
