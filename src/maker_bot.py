# src/maker_bot.py
from __future__ import annotations
import os, sys, json, time, argparse, logging, threading
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, Any, List

from dotenv import load_dotenv
import websocket  # websocket-client

from .adapter import ExchangeAdapter, HLConfig

# ---- logging simple
log = logging.getLogger("maker_bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAINNET_WS = "wss://api.hyperliquid.xyz/ws"
TESTNET_WS  = "wss://api.hyperliquid-testnet.xyz/ws"

# ----------------- WS L2Book -----------------
class OrderBookWS:
    def __init__(self, coin_getter, use_testnet: bool = False):
        self.coin_getter = coin_getter
        self.ws_url = TESTNET_WS if use_testnet else MAINNET_WS
        self.ws = None
        self.connected = False
        self.bids: List[Any] = []
        self.asks: List[Any] = []

    def _subscribe(self):
        coin = self.coin_getter()  # ej. @142 o UBTC/USDC (el adapter lo resolvió)
        sub = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}
        self.ws.send(json.dumps(sub))
        log.info(f"[WS] Subscribed l2Book {coin}")

    def on_open(self, ws):
        self.connected = True
        log.info("Websocket connected")
        self._subscribe()

    def on_message(self, ws, msg):
        try:
            data = json.loads(msg)
            if data.get("channel") == "l2Book":
                lvls = data.get("data", {}).get("levels")
                if isinstance(lvls, list) and len(lvls) >= 2:
                    self.bids, self.asks = lvls[0], lvls[1]
        except Exception as e:
            log.warning(f"[WS] parse error: {e}")

    def on_error(self, ws, err):
        self.connected = False
        log.error(f"[WS] error: {err}")

    def on_close(self, ws, code, reason):
        self.connected = False
        log.warning(f"[WS] closed {code} {reason}")

    def start(self):
        websocket.enableTrace(False)
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self.on_open, on_message=self.on_message,
            on_error=self.on_error, on_close=self.on_close
        )
        t = threading.Thread(target=lambda: self.ws.run_forever(ping_interval=30, ping_timeout=10), daemon=True)
        t.start()
        # esperar conexión
        for _ in range(60):
            if self.connected:
                log.info("[WS] ready")
                return
            time.sleep(0.25)
        raise RuntimeError("WS no conectó")

    def stop(self):
        try:
            if self.ws:
                self.ws.close()
        finally:
            self.connected = False

    @staticmethod
    def _px(entry) -> float:
        # entry puede ser {'px': '...','sz': '...'} o ['px','sz']
        if isinstance(entry, dict):
            return float(entry["px"])
        return float(entry[0])

    def best_prices(self) -> Tuple[Optional[float], Optional[float]]:
        bid = self._px(self.bids[0]) if self.bids else None
        ask = self._px(self.asks[0]) if self.asks else None
        return bid, ask

# ----------------- BOT -----------------
@dataclass
class BotArgs:
    ticker: str
    amount_per_level: float   # USD por orden (~notional)
    min_spread: float         # % (por ejemplo 0.05 = 0.05%)
    maker_only: bool
    ttl: float                # segundos para cancelar resting
    use_testnet: bool
    use_agent: bool
    agent_private_key: Optional[str]

class MakerBot:
    def __init__(self, adapter: ExchangeAdapter, args: BotArgs):
        self.h = adapter
        self.args = args
        self.coin: Optional[str] = None
        self.ws: Optional[OrderBookWS] = None
        self.resting: Dict[Any, float] = {}  # oid (int|str) -> t0

    def resolve_coin(self):
        self.coin = self.h.resolve_spot_coin(self.args.ticker)
        logging.info(f"[INIT] ticker={self.args.ticker} -> coin={self.coin}")

    def start_ws(self):
        self.ws = OrderBookWS(lambda: self.coin, self.h.cfg.use_testnet)
        self.ws.start()

    # helpers
    @staticmethod
    def _spread_pct(bid: float, ask: float) -> float:
        return (ask - bid) / bid * 100.0 if bid and ask else 0.0

    @staticmethod
    def _extract_oid_like(container: Any, fallback: Any = None) -> Optional[Any]:
        """Devuelve un posible OID desde distintos formatos."""
        if isinstance(container, dict):
            if "oid" in container:
                return container.get("oid")
        elif isinstance(container, (int, str)):
            return container
        return fallback

    @staticmethod
    def _extract_status_and_oid(res: dict) -> Tuple[str, Optional[Any]]:
        """
        Devuelve ('filled'|'resting'|'error', oid|None).
        Soporta: 'resting', 'open', 'opened', 'placed', 'working', 'live', 'accepted', y 'filled'.
        """
        try:
            if res.get("status") != "ok":
                return "error", None

            data = res.get("response", {}).get("data", {})
            statuses = data.get("statuses", [])
            if not statuses or not isinstance(statuses, list):
                return "error", None

            st0 = statuses[0]

            # error explícito (sin OID)
            if "error" in st0:
                return "error", None

            # filled (con OID posible)
            if "filled" in st0:
                oid = MakerBot._extract_oid_like(st0["filled"], st0.get("oid"))
                return "filled", oid

            # variantes "orden abierta"
            for key in ("resting", "open", "opened", "placed", "working", "live", "accepted"):
                if key in st0:
                    oid = MakerBot._extract_oid_like(st0[key], st0.get("oid"))
                    if oid is not None:
                        return "resting", oid

            # último recurso: si viene un 'oid' toplevel, lo tratamos como resting
            oid = st0.get("oid")
            if oid is not None:
                return "resting", oid

            return "error", None
        except Exception:
            return "error", None

    @staticmethod
    def _valid_oid(oid: Any) -> bool:
        """OID válido si es int>0, o str no vacío distinto de 'filled'."""
        if isinstance(oid, int):
            return oid > 0
        if isinstance(oid, str):
            s = oid.strip().lower()
            return (s != "") and (s != "filled")
        return False

    @staticmethod
    def _coerce_oid_for_cancel(oid: Any) -> Any:
        """Convierte '12345' -> 12345; deja ints y otras strings como están."""
        if isinstance(oid, str) and oid.isdigit():
            return int(oid)
        return oid

    def _place_limit_usd(self, side: str, px: float, usd: float) -> Optional[Any]:
        size = usd / px if px > 0 else 0.0
        res = self.h.place_limit(self.coin, side, size, px)
        status, oid = self._extract_status_and_oid(res)
        if status == "filled":
            return "filled"
        if status == "resting" and self._valid_oid(oid):
            log.info(f"[RESTING] track {side.upper()} oid={oid}")
            return oid  # devolvemos OID para manejar TTL
        return None

    def loop(self):
        last_status = 0.0
        while True:
            try:
                bid, ask = self.ws.best_prices() if self.ws else (None, None)
                if not bid or not ask:
                    time.sleep(0.2)
                    continue

                spread = self._spread_pct(bid, ask)
                if spread < self.args.min_spread:
                    time.sleep(0.25)
                    # status periódico
                    now = time.time()
                    if now - last_status > 10:
                        log.info(f"[BOOK] bid={bid:.6f} ask={ask:.6f} spread={spread:.4f}%")
                        last_status = now
                    continue

                # precios
                if self.args.maker_only:
                    eps = 1e-6
                    buy_px = bid * (1 - eps)   # no cruza
                    sell_px = ask * (1 + eps)  # no cruza
                else:
                    buy_px = ask               # taker
                    sell_px = bid              # taker

                # coloca 1 buy + 1 sell (~USD por orden)
                oid_b = self._place_limit_usd("buy",  buy_px, self.args.amount_per_level)
                oid_s = self._place_limit_usd("sell", sell_px, self.args.amount_per_level)

                now = time.time()
                if self.args.maker_only:
                    # guardar OIDs resting para cancelar por TTL (si son válidos)
                    if self._valid_oid(oid_b):
                        self.resting[oid_b] = now
                    if self._valid_oid(oid_s):
                        self.resting[oid_s] = now

                    # cancelar por TTL
                    to_cancel = [oid for oid, t0 in list(self.resting.items()) if now - t0 >= self.args.ttl]
                    for oid in to_cancel:
                        try:
                            if self._valid_oid(oid):
                                coid = self._coerce_oid_for_cancel(oid)
                                _ = self.h.cancel(self.coin, coid)
                                log.info(f"[TTL] ORDEN CANCELADA {oid}")
                        except Exception as e:
                            log.warning(f"[TTL] cancel error {oid}: {e}")
                        finally:
                            self.resting.pop(oid, None)
                else:
                    # en modo taker, si una quedó resting, la cancelamos rápido (comportamiento del bot original)
                    for oid in [oid_b, oid_s]:
                        if self._valid_oid(oid):
                            try:
                                coid = self._coerce_oid_for_cancel(oid)
                                _ = self.h.cancel(self.coin, coid)
                            except Exception:
                                pass

                # status periódico
                if now - last_status > 10:
                    log.info(f"[BOOK] bid={bid:.6f} ask={ask:.6f} spread={spread:.4f}%")
                    last_status = now

                time.sleep(0.2)

            except KeyboardInterrupt:
                log.info("Stop por usuario (Ctrl+C)")
                break
            except Exception as e:
                log.error(f"loop error: {e}")
                time.sleep(1.0)

# ----------------- CLI -----------------
def load_env_defaults():
    load_dotenv()
    return {
        "PRIVATE_KEY": os.getenv("HL_PRIVATE_KEY", ""),
        "TICKER": os.getenv("HL_TICKER", "UBTC/USDC"),
        "AMOUNT_PER_LEVEL": os.getenv("HL_GRID_AMOUNT_PER_LEVEL", "5"),
        "MIN_SPREAD": os.getenv("HL_GRID_MIN_SPREAD", "0.05"),  # %
        "TTL": os.getenv("HL_TTL", "20"),                       # s
        "USE_TESTNET": os.getenv("HL_USE_TESTNET", "false").lower() == "true",
        "USE_AGENT": os.getenv("HL_USE_AGENT", "false").lower() == "true",
        "AGENT_PRIVATE_KEY": os.getenv("HL_AGENT_PRIVATE_KEY", ""),
    }

def parse_args():
    env = load_env_defaults()
    p = argparse.ArgumentParser(description="Based Maker (spot) con maker-only opcional")
    p.add_argument("--ticker", "--symbol", dest="ticker", default=env["TICKER"])
    p.add_argument("--amount-per-level", type=float, default=float(env["AMOUNT_PER_LEVEL"]))
    p.add_argument("--min-spread", type=float, default=float(env["MIN_SPREAD"]),
                   help="Spread mínimo en % para operar (ej 0.05 = 0.05%)")
    p.add_argument("--maker-only", action="store_true", help="Evita cruzar (fees maker).")
    p.add_argument("--ttl", type=float, default=float(env["TTL"]),
                   help="Segundos para cancelar órdenes resting en maker-only.")
    p.add_argument("--testnet", action="store_true", default=env["USE_TESTNET"])
    p.add_argument("--use-agent", action="store_true", default=env["USE_AGENT"])
    p.add_argument("--agent-private-key", default=env["AGENT_PRIVATE_KEY"])
    return p.parse_args()

def main():
    args = parse_args()
    priv = os.getenv("HL_PRIVATE_KEY", "").strip()
    if not priv:
        print("Falta HL_PRIVATE_KEY en .env")
        sys.exit(1)

    cfg = HLConfig(
        private_key=priv,
        use_testnet=args.testnet,
        use_agent=args.use_agent,
        agent_private_key=args.agent_private_key or None,
    )
    adapter = ExchangeAdapter(cfg)

    bot = MakerBot(
        adapter,
        BotArgs(
            ticker=args.ticker,
            amount_per_level=args.amount_per_level,
            min_spread=args.min_spread,
            maker_only=args.maker_only,
            ttl=args.ttl,
            use_testnet=args.testnet,
            use_agent=args.use_agent,
            agent_private_key=args.agent_private_key or None,
        ),
    )

    bot.resolve_coin()
    bot.start_ws()
    bot.loop()

if __name__ == "__main__":
    main()
