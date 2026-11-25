"""Async Telegram notifier and kill-switch command helper."""

from __future__ import annotations

import httpx

from meowth.config import Settings
from meowth.storage.db import TradingDatabase


class TelegramNotifier:
    """Lightweight async Telegram Bot API client for trade alerts and kill switches."""

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_settings(cls, settings: Settings) -> TelegramNotifier:
        token = (
            settings.telegram_bot_token.get_secret_value()
            if settings.telegram_bot_token
            else None
        )
        return cls(bot_token=token, chat_id=settings.telegram_chat_id)

    @property
    def enabled(self) -> bool:
        return bool(self._bot_token and self._chat_id)

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._bot_token}/{method}"

    async def send_message(self, text: str) -> bool:
        """Send an alert message. Returns False if Telegram is not configured."""
        if not self.enabled:
            return False

        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.post(
                self._api_url("sendMessage"),
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
            return True
        finally:
            if self._owns_client:
                await client.aclose()

    def handle_command(self, text: str, db: TradingDatabase) -> str:
        """Handle manual control commands (/pause, /resume, /status) against DuckDB state."""
        cmd = text.strip().lower().split()[0] if text.strip() else ""
        if cmd == "/pause":
            db.set_state("manual_pause", "true")
            return "⏸️ Kill switch activado: nuevas entradas pausadas."
        if cmd == "/resume":
            db.set_state("manual_pause", "false")
            return "▶️ Sistema reanudado: nuevas entradas habilitadas."
        if cmd == "/status":
            paused = db.get_state("manual_pause", "false") == "true"
            spend = db.get_daily_llm_spend_usd()
            trades_count = db.count_rows("trades")
            rejections_count = db.count_rows("rejections")
            state_label = "PAUSADO" if paused else "ACTIVO"
            return (
                f"Estado: {state_label}\n"
                f"Trades registrados: {trades_count}\n"
                f"Rechazos registrados: {rejections_count}\n"
                f"Gasto LLM hoy: ${spend:.4f}"
            )
        return "Comandos disponibles: /status, /pause, /resume"
