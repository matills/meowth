# Meowth

Sistema de agentes de trading 24/7 en Solana, **100% paper trading primero** con cotizaciones reales y reglas de riesgo deterministas.

Ver análisis completo y plan por fases en [docs/analisis-y-plan.md](docs/analisis-y-plan.md).

## Estado actual — Fase 0 (Base)

- Repo Python 3.12 gestionado con `uv` (`uv.lock` para reproducibilidad y seguridad de dependencias).
- Esquema analítico en DuckDB (`tokens`, `ticks`, `candles`, `signals`, `rejections`, `trades`, `llm_calls`, `equity_curve`, `system_state`) con respaldo a Parquet.
- Configuración tipada vía `.env` (`pydantic-settings`).
- Motor determinista de riesgo y kill switches (`RiskManager`).
- Interfaz `LLMClient` con wrapper de presupuesto diario y cache (`BudgetedLLMClient`).
- Contrato común `Executor` para `PaperExecutor` y `LiveExecutor`.
- Cliente de alertas y comandos de control por Telegram (`/status`, `/pause`, `/resume`).
- `Dockerfile` y `docker-compose.yml` listos para escalar a VPS en Fase 3.

## Inicio rápido

```bash
cp .env.example .env
uv sync --extra dev
uv run meowth init-db
uv run meowth check-config
uv run pytest
```
