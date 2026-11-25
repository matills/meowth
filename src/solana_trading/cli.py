"""CLI entrypoint for database initialization, config validation, and backups."""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading

import httpx
import structlog

from solana_trading.agents.analyst import AnalystAgent
from solana_trading.agents.scout import ScoutAgent
from solana_trading.agents.supervisor import SupervisorAgent
from solana_trading.alerts.telegram import TelegramNotifier
from solana_trading.config import Settings
from solana_trading.dashboard.server import create_dashboard_server
from solana_trading.data.dexscreener import DexScreenerClient
from solana_trading.data.ingestor import MarketDataIngestor
from solana_trading.data.jupiter import LAMPORTS_PER_SOL, WSOL_MINT, JupiterQuoteClient
from solana_trading.data.solana_rpc import OnChainTokenInspector
from solana_trading.data.watchlist import ESTABLISHED_SOLANA_MINTS, USDC_MINT
from solana_trading.engine import TradingEngine
from solana_trading.execution.paper import PaperExecutor
from solana_trading.llm.providers import build_llm_client
from solana_trading.risk.manager import RiskManager
from solana_trading.storage.db import TradingDatabase
from solana_trading.strategies.implementations import MomentumBreakoutStrategy

logger = structlog.get_logger(__name__)


def cmd_init_db(settings: Settings) -> int:
    with TradingDatabase(settings.duckdb_path) as db:
        db.init_schema()
        tables = db.list_tables()
    logger.info("duckdb_initialized", path=str(settings.duckdb_path), tables=tables)
    print(f"DuckDB listo en {settings.duckdb_path} ({len(tables)} tablas: {', '.join(tables)})")
    return 0


def cmd_backup_parquet(settings: Settings) -> int:
    with TradingDatabase(settings.duckdb_path, read_only=True) as db:
        target = db.export_parquet_backup(settings.parquet_backup_dir)
    logger.info("parquet_backup_completed", target=str(target))
    print(f"Backup Parquet generado en {target}")
    return 0


def cmd_check_config(settings: Settings) -> int:
    print(f"Modo: {settings.trading_mode.value}")
    print(f"DuckDB: {settings.duckdb_path}")
    print(f"RPC: {settings.solana_rpc_url}")
    print(f"Jupiter: {settings.jupiter_api_url}")
    print(f"Telegram configurado: {'sí' if settings.telegram_enabled else 'no'}")
    print(
        f"LLM: provider={settings.llm_provider}, "
        f"daily_budget=${settings.llm_daily_budget_usd:.2f}"
    )
    print(
        f"Riesgo: max_pos={settings.risk_max_position_pct:.0%}, "
        f"max_open={settings.risk_max_open_positions}, "
        f"max_exp={settings.risk_max_total_exposure_pct:.0%}, "
        f"max_impact={settings.risk_max_price_impact_pct:.0%}"
    )
    return 0


def cmd_scout_scan(settings: Settings, mints: list[str] | None = None) -> int:
    async def _run() -> tuple[int, int]:
        with TradingDatabase(settings.duckdb_path) as db:
            db.init_schema()
            dex = DexScreenerClient(base_url=settings.dexscreener_api_url)
            if mints:
                target_mints = mints
            else:
                boosted = await dex.fetch_latest_boosted_solana_mints()
                target_mints = list(dict.fromkeys(ESTABLISHED_SOLANA_MINTS + boosted))
            scout = ScoutAgent(
                settings=settings,
                db=db,
                dexscreener=dex,
                inspector=OnChainTokenInspector(settings.solana_rpc_url),
                jupiter=JupiterQuoteClient(base_url=settings.jupiter_api_url),
            )
            approved, rejected = await scout.scan_mints(target_mints)
            return len(approved), len(rejected)

    ok_count, rej_count = asyncio.run(_run())
    print(f"Scout completado: {ok_count} aprobados, {rej_count} rechazados")
    return 0


def cmd_ingest_once(settings: Settings) -> int:
    async def _run() -> int:
        with TradingDatabase(settings.duckdb_path) as db:
            db.init_schema()
            ingestor = MarketDataIngestor(
                db=db,
                dexscreener=DexScreenerClient(base_url=settings.dexscreener_api_url),
            )
            return await ingestor.ingest_mints()

    count = asyncio.run(_run())
    print(f"Ingesta completada: {count} tokens actualizados (ticks + velas 1m/5m)")
    return 0


def cmd_test_connections(settings: Settings) -> int:
    async def _run() -> int:
        failures = 0

        # 1. Solana RPC (Helius / QuickNode / public)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    settings.solana_rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []},
                )
                r.raise_for_status()
                slot = r.json().get("result")
            print(f"[OK] Solana RPC conectado (slot={slot})")
        except Exception as exc:
            failures += 1
            print(f"[ERROR] Solana RPC: {exc}")

        # 2. DexScreener
        try:
            dex = DexScreenerClient(base_url=settings.dexscreener_api_url)
            pairs = await dex.fetch_token_pairs(ESTABLISHED_SOLANA_MINTS[:2])
            print(f"[OK] DexScreener conectado ({len(pairs)} pares recibidos)")
        except Exception as exc:
            failures += 1
            print(f"[ERROR] DexScreener: {exc}")

        # 3. Jupiter Quote API
        try:
            jup = JupiterQuoteClient(base_url=settings.jupiter_api_url)
            q = await jup.get_quote(WSOL_MINT, USDC_MINT, int(0.1 * LAMPORTS_PER_SOL))
            print(f"[OK] Jupiter conectado (impact={q.price_impact_pct:.3%})")
        except Exception as exc:
            failures += 1
            print(f"[ERROR] Jupiter ({settings.jupiter_api_url}): {exc}")

        # 4. Telegram
        notifier = TelegramNotifier.from_settings(settings)
        if notifier.enabled:
            try:
                await notifier.send_message("✅ Solana Paper Trading: conexión de prueba OK.")
                print("[OK] Telegram conectado (mensaje de prueba enviado)")
            except Exception as exc:
                failures += 1
                print(f"[ERROR] Telegram: {exc}")
        else:
            print("[SKIP] Telegram no configurado en .env")

        # 5. LLM
        with TradingDatabase(settings.duckdb_path) as db:
            db.init_schema()
            llm = build_llm_client(settings, db)
            if llm is None:
                print("[SKIP] LLM desactivado (LLM_PROVIDER=none)")
            else:
                try:
                    resp = await llm.complete_json(
                        role="healthcheck",
                        system_prompt='Responde SOLO JSON: {"status": "ok"}',
                        user_prompt="ping",
                        use_cache=False,
                    )
                    print(
                        f"[OK] LLM ({settings.llm_provider}/{settings.llm_model}) "
                        f"respondió: {resp.content}"
                    )
                except Exception as exc:
                    failures += 1
                    print(f"[ERROR] LLM ({settings.llm_provider}): {exc}")

        return 1 if failures else 0

    return asyncio.run(_run())


def cmd_run_paper(
    settings: Settings,
    interval: int = 60,
    dashboard_port: int | None = 8080,
    dashboard_host: str = "127.0.0.1",
) -> int:
    async def _run() -> None:
        with TradingDatabase(settings.duckdb_path) as db:
            db.init_schema()
            server = None
            if dashboard_port:
                server = create_dashboard_server(db, host=dashboard_host, port=dashboard_port)
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                print(f"Dashboard activo en http://{dashboard_host}:{dashboard_port}")

            dex = DexScreenerClient(base_url=settings.dexscreener_api_url)
            jup = JupiterQuoteClient(base_url=settings.jupiter_api_url)
            llm = build_llm_client(settings, db)
            engine = TradingEngine(
                settings=settings,
                db=db,
                scout=ScoutAgent(
                    settings=settings,
                    db=db,
                    dexscreener=dex,
                    inspector=OnChainTokenInspector(settings.solana_rpc_url),
                    jupiter=jup,
                ),
                analyst=AnalystAgent(db=db, llm=llm),
                ingestor=MarketDataIngestor(db=db, dexscreener=dex),
                strategy=MomentumBreakoutStrategy(),
                risk=RiskManager(settings),
                executor=PaperExecutor(settings=settings, db=db, jupiter=jup),
                notifier=TelegramNotifier.from_settings(settings),
            )
            try:
                await engine.run_forever(
                    interval_seconds=interval,
                    seed_mints=ESTABLISHED_SOLANA_MINTS,
                )
            finally:
                if server is not None:
                    server.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nMotor detenido por el usuario.")
    return 0


def cmd_dashboard(settings: Settings, host: str = "0.0.0.0", port: int = 8080) -> int:
    with TradingDatabase(settings.duckdb_path) as db:
        db.init_schema()
        server = create_dashboard_server(db, host=host, port=port)
        print(f"Dashboard escuchando en http://{host}:{port}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
    return 0


def cmd_supervisor_review(settings: Settings) -> int:
    async def _run() -> str:
        with TradingDatabase(settings.duckdb_path) as db:
            db.init_schema()
            llm = build_llm_client(settings, db)
            report = await SupervisorAgent(db=db, llm=llm).generate_review()
            return report.summary

    print(asyncio.run(_run()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solana-trading")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="Initialize the local DuckDB schema")
    sub.add_parser("backup-parquet", help="Export all DuckDB tables to Parquet")
    sub.add_parser("check-config", help="Validate and print current configuration")
    scout_p = sub.add_parser("scout-scan", help="Discover and filter Solana candidate tokens")
    scout_p.add_argument("--mints", nargs="*", help="Optional explicit mint addresses to evaluate")
    sub.add_parser("ingest-once", help="Poll approved tokens into ticks and 1m/5m candles")
    sub.add_parser("test-connections", help="Verify RPC, DexScreener, Jupiter, Telegram, and LLM")
    run_p = sub.add_parser("run-paper", help="Run the 24/7 paper trading engine")
    run_p.add_argument("--interval", type=int, default=60, help="Loop interval in seconds")
    run_p.add_argument(
        "--dashboard-port",
        type=int,
        default=8080,
        help="Port for embedded Lightweight Charts dashboard (0 to disable)",
    )
    run_p.add_argument("--dashboard-host", default="127.0.0.1")
    dash_p = sub.add_parser("dashboard", help="Serve the Lightweight Charts web dashboard")
    dash_p.add_argument("--host", default="0.0.0.0")
    dash_p.add_argument("--port", type=int, default=8080)
    sub.add_parser("supervisor-review", help="Generate a Supervisor LLM review report")

    args = parser.parse_args(argv)
    settings = Settings()

    if args.command == "init-db":
        return cmd_init_db(settings)
    if args.command == "backup-parquet":
        return cmd_backup_parquet(settings)
    if args.command == "check-config":
        return cmd_check_config(settings)
    if args.command == "scout-scan":
        return cmd_scout_scan(settings, mints=args.mints)
    if args.command == "ingest-once":
        return cmd_ingest_once(settings)
    if args.command == "test-connections":
        return cmd_test_connections(settings)
    if args.command == "run-paper":
        port = args.dashboard_port if args.dashboard_port > 0 else None
        return cmd_run_paper(
            settings,
            interval=args.interval,
            dashboard_port=port,
            dashboard_host=args.dashboard_host,
        )
    if args.command == "dashboard":
        return cmd_dashboard(settings, host=args.host, port=args.port)
    if args.command == "supervisor-review":
        return cmd_supervisor_review(settings)
    return 1


if __name__ == "__main__":
    sys.exit(main())
