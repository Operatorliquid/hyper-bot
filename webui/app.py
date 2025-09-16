# webui/app.py
import os, sys, threading, logging
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Permite importar "src"
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from src.maker_bot import MakerBot, BotArgs, HLConfig, ExchangeAdapter  # usa tu código existente

# ---------- FastAPI ----------
app = FastAPI()

# CORS: permití tu dominio del front (Hostinger)
ALLOW_ORIGINS = os.getenv("ALLOW_ORIGINS", "*")
origins = [o.strip() for o in ALLOW_ORIGINS.split(",")] if ALLOW_ORIGINS else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Estado global simple ----------
BOT_THREAD: Optional[threading.Thread] = None
BOT_INST: Optional[MakerBot] = None
LOGS: List[str] = []
LOGS_MAX = 2000
LOG_LOCK = threading.Lock()

# Capturamos logs del bot
bot_logger = logging.getLogger("maker_bot")
class ListHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        with LOG_LOCK:
            LOGS.append(msg)
            if len(LOGS) > LOGS_MAX:
                del LOGS[: len(LOGS) - LOGS_MAX]
bot_handler = ListHandler()
bot_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
bot_logger.addHandler(bot_handler)
bot_logger.setLevel(logging.INFO)

# ---------- Entrada ----------
class StartPayload(BaseModel):
    ticker: str
    amount_per_level: float
    min_spread: float
    ttl: float
    maker_only: bool = True
    testnet: bool = False
    # Agent mode: la clave viene por ENV (AGENT_PRIVATE_KEY)

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
        agent_pk = (os.getenv("AGENT_PRIVATE_KEY", "")).strip()
        if not agent_pk:
            bot_logger.error("Falta AGENT_PRIVATE_KEY en variables de entorno")
            return

        cfg = HLConfig(
            private_key=None,       # agent-only
            use_testnet=p.testnet,
            use_agent=True,
            agent_private_key=agent_pk,
        )
        adapter = ExchangeAdapter(cfg)
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

        bot = MakerBot(adapter, args)
        BOT_INST = bot
        bot.resolve_coin()
        bot.start_ws()
        bot.loop()   # se corta cuando bot.stop_event.set()
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
            BOT_INST.stop_event.set()  # requiere que tu MakerBot tenga stop_event
        except Exception:
            pass
    return {"ok": True}
