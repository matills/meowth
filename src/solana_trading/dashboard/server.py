"""Lightweight HTTP dashboard server (JSON API + TradingView Lightweight Charts UI)."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from solana_trading.storage.db import TradingDatabase

STATIC_DIR = Path(__file__).parent / "static"


def build_dashboard_payload(
    db: TradingDatabase,
    path: str,
    query: dict[str, list[str]],
) -> tuple[int, dict | list]:
    if path == "/api/status":
        return HTTPStatus.OK, {
            "paused": db.get_state("manual_pause", "false") == "true",
            "trades_count": db.count_rows("trades"),
            "rejections_count": db.count_rows("rejections"),
            "daily_llm_spend_usd": db.get_daily_llm_spend_usd(),
            "approved_mints": db.get_approved_mints(),
            "last_data_ts": db.get_state("last_data_ts"),
        }
    if path == "/api/equity":
        return HTTPStatus.OK, db.get_equity_curve(limit=500)
    if path == "/api/trades":
        return HTTPStatus.OK, db.get_recent_trades(limit=100)
    if path == "/api/rejections":
        return HTTPStatus.OK, db.get_recent_rejections(limit=100)
    if path == "/api/candles":
        mint = (query.get("mint") or [""])[0]
        timeframe = (query.get("timeframe") or ["5m"])[0]
        if not mint:
            mints = db.get_approved_mints()
            mint = mints[0] if mints else ""
        if not mint:
            return HTTPStatus.OK, []
        candles = db.get_recent_candles(mint, timeframe=timeframe, limit=300)
        return HTTPStatus.OK, [
            {
                "time": int(c.open_time.timestamp()),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume_usd,
            }
            for c in candles
        ]
    return HTTPStatus.NOT_FOUND, {"error": "Not found"}


def create_dashboard_server(
    db: TradingDatabase,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> ThreadingHTTPServer:
    """Create a ThreadingHTTPServer exposing the dashboard UI and JSON endpoints."""

    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, data: object) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                html_bytes = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html_bytes)))
                self.end_headers()
                self.wfile.write(html_bytes)
                return

            status, payload = build_dashboard_payload(db, parsed.path, parse_qs(parsed.query))
            self._send_json(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/pause":
                db.set_state("manual_pause", "true")
                self._send_json(HTTPStatus.OK, {"paused": True})
                return
            if parsed.path == "/api/resume":
                db.set_state("manual_pause", "false")
                self._send_json(HTTPStatus.OK, {"paused": False})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return ThreadingHTTPServer((host, port), DashboardHandler)
