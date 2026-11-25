"""Tests for Telegram alerts and manual kill-switch commands."""

from __future__ import annotations

import httpx

from meowth.alerts.telegram import TelegramNotifier
from meowth.storage.db import TradingDatabase


async def test_telegram_disabled_returns_false() -> None:
    notifier = TelegramNotifier(bot_token=None, chat_id=None)
    assert notifier.enabled is False
    assert await notifier.send_message("hello") is False


async def test_telegram_sends_message_when_configured() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "body": request.content.decode()})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="999", client=client)
        sent = await notifier.send_message("Trade ejecutado")

    assert sent is True
    assert len(captured) == 1
    assert "sendMessage" in str(captured[0]["url"])


def test_telegram_commands_toggle_pause_in_duckdb(db: TradingDatabase) -> None:
    notifier = TelegramNotifier(bot_token=None, chat_id=None)

    reply_pause = notifier.handle_command("/pause", db)
    assert "pausadas" in reply_pause.lower()
    assert db.get_state("manual_pause") == "true"

    reply_status = notifier.handle_command("/status", db)
    assert "PAUSADO" in reply_status

    reply_resume = notifier.handle_command("/resume", db)
    assert "reanudado" in reply_resume.lower()
    assert db.get_state("manual_pause") == "false"
