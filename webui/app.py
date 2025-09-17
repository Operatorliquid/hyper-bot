# webui/app.py
from __future__ import annotations

import os, sys, json, signal, time, threading, subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="based-maker-webui")

# ---------- CORS (abierto para test; luego restringí ORIGINS/HEADERS/METHODS) ----------
allow_origins = [o.strip() for o in os.getenv("ALLOW_ORIGINS", "*").split(",") if o.strip()]
allow_headers = [h.strip() for h in os.getenv("ALLOW_HEADERS", "*").split(",") if h.strip()]
allow_methods = [m.strip() for m in os.getenv("ALLOW_METHODS", "*").split(",") if m.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins or ["*"],
    allow_credentials=False,                 # importante con "*"
    allow_methods=allow_methods or ["*"],
    allow_headers=allow_headers or ["*"],
)

WEB_TOKEN = os.getenv("WEBUI_AUTH_TOKEN", "").strip()

# ---------- Estado subproceso + logs en memoria ----------
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
        LOGS[:] = LOGS[-MAX_LOGS:]

def _reader_thread(stream):
    for raw in iter(stream.readline, ""):
        if not raw:
            break
        _append_log(raw.rstrip("\n"))
    try:
        stream.close()
    except Exception:
        pass
    _append_log("[STDOUT] closed")

def _start_reader(proc: subprocess.Popen):
    t = threading.Thread(target=_reader_thread, args=(proc.stdout,), daemon=True)
    t.start()

def is_running() -> bool:
    with BOT_LOCK:
        return BOT_PROC is not None and BOT_PROC.poll() is None

# ---------- Auth (acepta header o ?token=...) ----------
async def auth_dep(request: Request, token: Optional[str] = Query(default=None)):
    if request.method == "OPTIONS":
        return
    hdr = request.headers.get("Authorization", "")
    xhdr = request.headers.get("X-Auth-Token", "")
    bearer = hdr.split("Bearer ", 1)[1].strip() if hdr.startswith("Bearer ") else ""
    provided = token or bearer or xhdr
    if not WEB_TOKEN or provided != WEB_TOKEN:
        raise HTTPException(401, "unauthorized")

# ---------- Lanzar / parar bot ----------
def start_bot(payload: dict):
    global BOT_PROC
    with BOT_LOCK:
        if is_running():
            raise RuntimeError("bot ya está corriendo")

        ticker      = str(payload.get("ticker", "UBTC/USDC"))
        amount      = float(payload.get("amount_per_level", 5))
        min_spread  = float(payload.get("min_spread", 0.05))
        ttl         = float(payload.get("ttl", 20))
        maker_only  = bool(payload.get("maker_only", False))
        testnet     = bool(payload.get("testnet", False))
        agent_pk    = str(payload.get("agent_private_key", "")).strip()

        if not agent_pk or not agent_pk.startswith("0x") or len(agent_pk) != 66:
            raise ValueError("agent_private_key inválida")

        cmd = [
            sys.executable, "-m", "src.maker_bot",
            "--ticker", ticker,
            "--amount-per-level", str(amount),
            "--min-spread", str(min_spread),
            "--ttl", str(ttl),
            "--agent-private-key", agent_pk,
            "--use-agent",
        ]
        if maker_only: cmd.append("--maker-only")
        if testnet:    cmd.append("--testnet")

        base_dir = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.setdefault("HL_PRIVATE_KEY", "")  # no se usa con --use-agent

        BOT_PROC = subprocess.Popen(
            cmd, cwd=str(base_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        _append_log(f"[WEB] launch: {' '.join(cmd)}")
        _start_reader(BOT_PROC)

def stop_bot(timeout: float = 8.0):
    global BOT_PROC
    with BOT_LOCK:
        if BOT_PROC is None:
            return False
        if BOT_PROC.poll() is not None:
            BOT_PROC = None
            return True
        try: BOT_PROC.send_signal(signal.SIGINT)
        except Exception: pass

    t0 = time.time()
    while time.time() - t0 < timeout:
        with BOT_LOCK:
            if BOT_PROC is None or BOT_PROC.poll() is not None:
                BOT_PROC = None
                return True
        time.sleep(0.2)

    with BOT_LOCK:
        try: BOT_PROC.terminate()
        except Exception: pass
    time.sleep(1.5)
    with BOT_LOCK:
        if BOT_PROC and BOT_PROC.poll() is None:
            try: BOT_PROC.kill()
            except Exception: pass
        BOT_PROC = None
    return True

# ---------- Rutas ----------
@app.get("/")
async def root():
    return {"ok": True, "service": "based-maker-webui", "ts": int(time.time())}

@app.get("/status")
async def status():
    return {"running": is_running()}

@app.get("/logs")
async def logs(since: int = 0):
    # simple: devuelve todo y el next
    return {"next": LOG_NEXT, "lines": LOGS}

# <<< ÚNICO CAMBIO: /start acepta JSON y text/plain con JSON >>>
@app.post("/start", dependencies=[Depends(auth_dep)])
async def start(request: Request):
    try:
        ct = request.headers.get("content-type", "")
        if "application/json" in ct:
            payload = await request.json()
        else:
            raw = await request.body()
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            try:
                payload = json.loads(raw or "{}")
            except Exception:
                return JSONResponse(
                    status_code=422,
                    content={"ok": False, "error": "Body debe ser JSON (string)"},
                )

        start_bot(payload)
        return {"ok": True}
    except Exception as e:
        _append_log(f"[WEB][START][ERR] {e}")
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

@app.post("/stop", dependencies=[Depends(auth_dep)])
async def stop():
    try:
        ok = stop_bot()
        return {"ok": ok}
    except Exception as e:
        _append_log(f"[WEB][STOP][ERR] {e}")
        return JSONResponse(status_code=400, content={"ok": False, "error": str(e)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "webui.app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("DEV_RELOAD", "0") == "1")
    )
