# Based Maker Backend (FastAPI + Bot)

Endpoints:
- `GET /status`
- `GET /logs?since=N`
- `POST /start` (JSON: ticker, amount_per_level, min_spread, ttl, maker_only, testnet)
- `POST /stop`

Env requeridos en Railway:
- `AGENT_PRIVATE_KEY` = 0x...  (API/agent wallet autorizada)
- `ALLOW_ORIGINS` = https://tu-dominio.com,https://www.tu-dominio.com (CORS)

Procfile: `web: uvicorn webui.app:app --host 0.0.0.0 --port $PORT`
