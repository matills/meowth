# Meowth

Sistema de agentes de trading 24/7 **100% paper-first** (Solana DEX + Crypto CEX en Binance + Acciones USA) con señales deterministas backtesteables, simulador pesimista, base analítica en DuckDB, alertas por Telegram y dashboard web en tiempo real con TradingView Lightweight Charts.

> **Disclaimer:** Este proyecto es experimental y educativo, diseñado para operar en **paper trading** y auditar métricas antes de arriesgar capital real. No constituye asesoramiento financiero.

## Arquitectura

- **Estrategias deterministas (sin IA en señales):** `MomentumBreakoutStrategy` (EMA 9/21/50 + volumen), `MeanReversionStrategy` (RSI con confirmación) y `VwapTrendStrategy` compartidas entre backtest y ejecución en vivo.
- **Agentes modulares:**
  - `Scout`: descubre pares graduados de Solana vía DexScreener, valida seguridad on-chain (`mintAuthority`, `freezeAuthority`, top holders, decimales reales) vía Solana RPC y simula compra/venta en Jupiter Swap v1 (anti-honeypot).
  - `Analyst` & `Supervisor` (LLM opcional): interfaz `LLMClient` enchufable (Gemini, Grok, DeepSeek, Ollama) con cache en DuckDB, tope de gasto diario en USD y fallback automático a reglas deterministas.
  - `RiskManager`: veto final en código puro (máx. 2% por operación, máx. 5 posiciones abiertas, exposición 30%, slippage ≤ 2%, pérdida diaria ≤ 5%, drawdown ≤ 20%, monitor de rug y kill switches).
- **Simuladores pesimistas:** `PaperExecutor` (Solana/Jupiter con demora de confirmación, peor de 2 cotizaciones, fees y fallos simulados) y `MultiAssetPaperExecutor` (Binance Crypto + Acciones USA en USD con bid/ask + comisiones).
- **Almacenamiento & Reportes:** DuckDB *single-writer* con respaldo diario a Parquet, backtester *walk-forward*, soporte para tearsheets de QuantStats y auditoría `gate-check`.
- **Dashboard en vivo:** UI terminal neón embebida con TradingView Lightweight Charts (`http://127.0.0.1:8080`), curva de equity, velas 5m con marcadores BUY/SELL, feed `$ tail -f meowth.log` y botón manual de Kill Switch.

## Instalación rápida

Requisitos: Python 3.12+ y [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/matills/meowth.git
cd meowth
cp .env.example .env
uv sync --extra dev
```

*(Opcional para tearsheets HTML con QuantStats: `uv sync --extra dev --extra backtest`.)*

## Uso

### 1. Inicializar base y verificar conexiones
```bash
uv run meowth init-db
uv run meowth test-connections
```

### 2. Backtest instantáneo sobre velas reales de 5m
```bash
uv run meowth backtest-symbol BTCUSDT --market crypto
uv run meowth backtest-symbol NVDA --market stocks
```

### 3. Correr en paper 24/7 + Dashboard (`http://127.0.0.1:8080`)
- **Modo Multi-Activo (Binance Crypto + Acciones USA en USD):**
  ```bash
  uv run meowth run-multi --markets crypto,stocks --strategy all --interval 60 --dashboard-port 8080
  ```
- **Modo Solana DEX (DexScreener + Helius + Jupiter en SOL):**
  ```bash
  uv run meowth run-paper --interval 60 --dashboard-port 8080
  ```

### 4. Auditar Gate B y generar backup Parquet
```bash
uv run meowth gate-check --db data/meowth_multi.duckdb
uv run meowth backup-parquet
```

## Tests y Linter

```bash
uv run pytest -v
uv run ruff check src tests
```

## Contribuciones

Las PRs son bienvenidas — especialmente nuevas estrategias deterministas en `src/meowth/strategies/`, conectores de mercado en `src/meowth/markets/` y mejoras de backtesting. Ver el documento de diseño original en [docs/analisis-y-plan.md](docs/analisis-y-plan.md).

## Licencia

[MIT](LICENSE)
