# webui/app.py
from __future__ import annotations

import os
import sys
import json
import signal
import time
import threading
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# -----------------------------------------------------------------------------
# CORS configurable por variables de entorno (Railway → Variables)
# -----------------------------------------------------------------------------
app = FastAPI()

allow_origins = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",") if o.strip()]
allow_headers = [h.strip() for h in os.getenv("ALLOW_HEADERS", "*").split(",") if h.strip()]
allow_methods = [m.strip() for m in os.getenv("ALLOW_METHODS", "*").split(",") if m.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins or ["*"],
    allow_credentials=True,
    allow_methods=allow_methods or ["*"],
    allow_headers=allow_headers or ["*"],
)

# -----------------------------------------------------------------------------
# Auth: token simple para /start y /stop. NO bloquea OPTIONS (preflight)
# -----------------------------------------------------------------------------
WEB_TOKEN = os.getenv("WEBUI_AUTH_TOKEN", "").strip()

async def auth_dep(request: Request):
    # Dejar pasar el preflight
    if request.method == "OPTIONS":
        return
    # Aceptar Authorization: Bearer <token> o X-Auth-Token: <token>
    auth = request.headers.get("Authorization", "")
    xauth = request.headers.get("X-Auth-Token", "")
    bearer = auth.split("Bearer ", 1)[1].strip() if auth.startswith("Bearer ") else ""
    token = bearer or xauth
    if not WEB_TOKEN or token != WEB_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")

# -----------------------------------------------------------------------------
# Estado global del subproceso (bot) y captura de logs
# -----------------------------------------------------------------------------
BOT_PROC: Optional[subprocess.Popen] = None
BOT_LOCK = threading.Lock()

LOGS: List[str] = []
LOG_NEXT = 0
MAX_LOGS = 4000

def _append_log(line: str):
    global LOGS, LOG_NEXT
    line = line.rstrip("\n")
    LOGS.append(line)
    LOG_NEXT += 1
    if len(LOGS) > MAX_LOGS:
        # recorta para no crecer indefinidamente
        excess = len(LOGS) - MAX_LOGS
        LOGS = LOGS[excess:]
        # LOG_NEXT sigue contando globalmente; el cliente usa 'since' y 'next'

def _reader_thread(stream, tag: str):
    for raw in iter(stream.readline, ""):
        if not raw:
            break
        _append_log(raw.rstrip("\n"))
    try:
        stream.close()
    except Exception:
        pass
    _append_log(f"[{tag}] closed")

def _start_reader_threads(proc: subprocess.Popen):
    t1 = threading.Thread(target=_reader_thread, args=(proc.stdout, "STDOUT"), daemon=True)
    t1.start()

# -----------------------------------------------------------------------------
# Helpers para lanzar/detener el bot
# -----------------------------------------------------------------------------
def is_running() -> bool:
    with BOT_LOCK:
        return BOT_PROC is not None and BOT_PROC.poll() is None

def start_bot_subprocess(payload: dict):
    """
    Lanza `python -m src.maker_bot` con los flags que venís usando, pasando
    el agente que crea el front (agent_private_key).
    """
    global BOT_PROC
    with BOT_LOCK:
        if is_running():
            raise RuntimeError("bot ya está corriendo")

        # Validación mínima
        ticker = str(payload.get("ticker", "UBTC/USDC"))
        amount = float(payload.get("amount_per_level", 5))
        min_spread = float(payload.get("min_spread", 0.05))
        ttl = float(payload.get("ttl", 20))
        maker_only = bool(payload.get("maker_only", False))
        testnet = bool(payload.get("testnet", False))
        agent_pk = str(payload.get("agent_private_key", "")).strip()

        if not agent_pk or not agent_pk.startswith("0x") or len(agent_pk) != 66:
            raise ValueError("agent_private_key inválida")

        # Comando: python -m src.maker_bot ...
        # Tu maker_bot ya acepta estos flags (— los definiste en argparse):
        # --ticker/--symbol, --amount-per-level, --min-spread, --ttl,
        # --maker-only, --testnet, --use-agent, --agent-private-key
        cmd = [
            sys.executable, "-m", "src.maker_bot",
            "--ticker", ticker,
            "--amount-per-level", str(amount),
            "--min-spread", str(min_spread),
            "--ttl", str(ttl),
            "--agent-private-key", agent_pk,
            "--use-agent",
        ]
        if maker_only:
            cmd.append("--maker-only")
        if testnet:
            cmd.append("--testnet")

        # Working dir = raíz del proyecto (donde está src/)
        base_dir = Path(__file__).resolve().parents[1]

        # IMPORTANTE: no agregamos headers ni nada aquí; eso es en el front
        env = os.environ.copy()
        # Si tu maker_bot todavía chequea HL_PRIVATE_KEY, puedes dejar una vacía
        # o setear alguna (no se usará cuando --use-agent). Idealmente tu bot ya NO lo requiere.
        env.setdefault("HL_PRIVATE_KEY", "")

        BOT_PROC = subprocess.Popen(
            cmd,
            cwd=str(base_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        _append_log(f"[WEB] launch: {' '.join(cmd)}")
        _start_reader_threads(BOT_PROC)

def stop_bot_subprocess(timeout: float = 8.0):
    global BOT_PROC
    with BOT_LOCK:
        if BOT_PROC is None:
            return False
        if BOT_PROC.poll() is not None:
            BOT_PROC = None
            return True

        # Primero, SIGINT (equivale a Ctrl+C) → tu MakerBot captura KeyboardInterrupt
        try:
            BOT_PROC.send_signal(signal.SIGINT)
        except Exception:
            pass

        # Espera un poco
        t0 = time.time()
        while time.time() - t0 < timeout:
            if BOT_PROC.poll() is not None:
                BOT_PROC = None
                return True
            time.sleep(0.2)

        # Segundo intento: SIGTERM
        try:
            BOT_PROC.terminate()
        except Exception:
            pass

        t1 = time.time()
        while time.time() - t1 < 5.0:
            if BOT_PROC.poll() is not None:
                BOT_PROC = None
                return True
            time.sleep(0.2)

        # Último recurso: SIGKILL
        try:
            BOT_PROC.kill()
        except Exception:
            pass
        BOT_PROC = None
        return True

# -----------------------------------------------------------------------------
# Rutas públicas
# -----------------------------------------------------------------------------
@app.get("/status")
async def status():
    return {"running": is_running()}

@app.get("/logs")
async def logs(since: int = 0):
    # Devuelve desde 'since' (índice) hacia adelante
    # El cliente guardará 'next' y vuelve a pedir desde ahí.
    start = max(0, since)
    # next es el siguiente índice global (LOG_NEXT)
    lines = LOGS[start - (LOG_NEXT - len(LOGS)):] if start < LOG_NEXT else []
    return {"next": LOG_NEXT, "lines": lines}

# -----------------------------------------------------------------------------
# Rutas protegidas (auth por token)
# -----------------------------------------------------------------------------
@app.post("/start", dependencies=[Depends(auth_dep)])
async def start(payload: dict = Body(...)):
    try:
        start_bot_subprocess(payload)
        return {"ok": True}
    except Exception as e:
        _append_log(f"[WEB][START][ERR] {e}")
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

@app.post("/stop", dependencies=[Depends(auth_dep)])
async def stop():
    try:
        ok = stop_bot_subprocess()
        return {"ok": ok}
    except Exception as e:
        _append_log(f"[WEB][STOP][ERR] {e}")
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

# -----------------------------------------------------------------------------
# (Opcional) Entrypoint local
# Railway usa tu Procfile: `web: uvicorn webui.app:app --host 0.0.0.0 --port $PORT`
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "webui.app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("DEV_RELOAD", "0") == "1")
    )
