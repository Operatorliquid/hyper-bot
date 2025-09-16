# webui/app.py
import os, sys, threading, logging
from typing import Optional, List
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Permitir importar desde /src
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from src.maker_bot import MakerBot, BotArgs, HLConfig, ExchangeAdapter  # tu bot y adapter

# ---------- FastAPI ----------
app = FastAPI()

# CORS: dominios de tu front (Hostinger), vienen por env ALLOW_ORIGINS
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*")
origins = [o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Auth (token) para /start y /stop ----------
AUTH = os.getenv("WEBUI_AUTH_TOKEN", "").strip()

@app.middleware("http")
async def require_token(request, call_next):
    if AUTH and request.url.path in ("/start", "/stop"):
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {AUTH}":
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    return await call_next(request)

# ---------- Estado global (sesión única) ----------
BOT_THREAD: Optional[threading.Thread] = None
BOT_INST: Optional[MakerBot] = None
LOGS: List[str] = []
LOGS_MAX = 2000
LOG_LOCK = threading.Lock()

# Capturar logs del bot en memoria
bot_logger = logging.getLogger("maker_bot")
class ListHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        with LOG_LOCK:
            LOGS.append(msg)
            if len(LOGS) > LOGS_MAX:
                del LOGS[: len(LOGS) - LOGS_MAX]
bot_handler = ListHandler()
bot_handler.setFormatter(logging.Formatter("%Y-%m-%d %H:%M:%S %(levelname)s %(message)s"))
bot_logger.addHandler(bot_handler)
bot_logger.setLevel(logging.INFO)

# ---------- Payload ----------
class StartPayload(BaseModel):
    ticker: str
    amount_per_level: float
    min_spread: float
    ttl: float
    maker_only: bool = True
    testnet: bool = False
    # BYO-agent: el usuario puede enviar su agent key en el body
    agent_private_key: Optional[str] = None

# ---------- Endpoints ----------
@app.get("/status")
def status():
    running = BOT_THREAD is not None and BOT_THREAD.is_alive()
    return {"running": running}

@app.get("/logs")
def get_logs(since: int = 0):
    with LOG_LOCK:
        items = LOGS[since:]
        nxt = since + len(items)
    return {"next": nxt, "lines": items}

def _run_bot(p: StartPayload):
    global BOT_INST
    try:
        # 1) Elegir clave del agente: primero la que viene del usuario; si no, la de env (opcional)
        agent_pk = (p.agent_private_key or os.getenv("AGENT_PRIVATE_KEY", "")).strip()
        if not agent_pk:
            bot_logger.error("Falta agent_private_key (en el body o env AGENT_PRIVATE_KEY)")
            return

        # 2) Configurar adapter en modo agente
        cfg = HLConfig(
            private_key=None,        # no usamos HL_PRIVATE_KEY
            use_testnet=p.testnet,
            use_agent=True,
            agent_private_key=agent_pk,
        )
        adapter = ExchangeAdapter(cfg)

        # 3) Args del bot
        args = BotArgs(
            ticker=p.ticker,
            amount_per_level=p.amount_per_level,
            min_spread=p.min_spread,
            maker_only=p.maker_only,
            ttl=p.ttl,
            use_testnet=p.testnet,
            use_agent=True,
            agent_private_key=agent_pk,
        )

        # 4) Iniciar bot
        bot = MakerBot(adapter, args)
        BOT_INST = bot
        bot.resolve_coin()
        bot.start_ws()
        bot.loop()  # se detiene cuando bot.stop_event.set()
    except Exception as e:
        bot_logger.error(f"[WEB] bot crashed: {e}")
    finally:
        BOT_INST = None
        bot_logger.info("[WEB] bot stopped")

@app.post("/start")
def start_bot(p: StartPayload):
    global BOT_THREAD, BOT_INST
    if BOT_THREAD and BOT_THREAD.is_alive():
        return JSONResponse({"ok": False, "error": "bot ya está corriendo"}, status_code=400)
    with LOG_LOCK:
        LOGS.clear()
    BOT_THREAD = threading.Thread(target=_run_bot, args=(p,), daemon=True)
    BOT_THREAD.start()
    return {"ok": True}

@app.post("/stop")
def stop_bot():
    global BOT_THREAD, BOT_INST
    if BOT_INST is not None:
        try:
            BOT_INST.stop_event.set()  # tu MakerBot ya lo tiene
        except Exception:
            pass
    return {"ok": True}
