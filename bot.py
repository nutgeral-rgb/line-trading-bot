import copy
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# =========================================================
# CONFIGURATION
# =========================================================

VERSION = "14.0"
BKK = timezone(timedelta(hours=7))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("portfolio-bot")


def env_int(name, default, minimum, maximum):
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} ต้องอยู่ระหว่าง {minimum} ถึง {maximum}")
    return value


def env_float(name, default, minimum, maximum):
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} มีค่าไม่ถูกต้อง")
    return value


def env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() in {"true", "1", "yes"}


DB_PATH = os.path.abspath(os.getenv("BOT_DB_PATH", "portfolio_bot.sqlite3"))
LEGACY_FILE = os.path.abspath(os.getenv("PORTFOLIO_FILE", "portfolio.json"))
LEGACY_USER_ID = os.getenv("LEGACY_USER_ID", "").strip()

TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

if not TOKEN or not SECRET:
    raise RuntimeError(
        "ต้องตั้งค่า LINE_CHANNEL_ACCESS_TOKEN และ LINE_CHANNEL_SECRET"
    )

INDEX_SYMBOL = os.getenv("MARKET_INDEX_SYMBOL", "^SET.BK").strip().upper()
CACHE_SECONDS = env_int("CACHE_SECONDS", 3600, 60, 86400)
MAX_DATA_AGE_DAYS = env_int("MAX_DATA_AGE_DAYS", 7, 1, 30)

BOARD_LOT = env_int("BOARD_LOT", 100, 1, 10000)
ALLOW_ODD_LOT = env_bool("ALLOW_ODD_LOT", False)

COMMISSION_RATE = env_float("COMMISSION_RATE", 0.00157, 0, 0.1)
VAT_RATE = env_float("VAT_RATE", 0.07, 0, 1)
MIN_COMMISSION = env_float("MIN_COMMISSION", 0, 0, 10000)
SLIPPAGE = env_float("SLIPPAGE", 0.002, 0, 0.05)

MIN_AVG_VALUE = env_float("MIN_AVG_VALUE", 10000000, 0, 1e12)
MIN_ATR_PCT = env_float("MIN_ATR_PCT", 1.2, 0, 100)
MAX_ATR_PCT = env_float("MAX_ATR_PCT", 12, 0.1, 100)
MAX_EXTENSION_PCT = env_float("MAX_EXTENSION_PCT", 12, 0, 100)
MIN_BREADTH = env_float("MIN_BREADTH", 50, 0, 100)
MIN_COVERAGE = env_float("MIN_COVERAGE", 0.8, 0.1, 1)
MAX_SECTOR_WEIGHT = env_float("MAX_SECTOR_WEIGHT", 0.35, 0.01, 1)
MIN_NET_RR = env_float("MIN_NET_RR", 1.5, 0.1, 10)

SIGNAL_HORIZON = env_int("SIGNAL_HORIZON", 15, 1, 120)
MAX_ENTRY_GAP = env_float("MAX_ENTRY_GAP", 0.02, 0, 0.2)
TIME_STOP_DAYS = env_int("TIME_STOP_DAYS", 15, 1, 120)

ENABLE_SCHEDULER = env_bool("ENABLE_SCHEDULER", True)
MAX_TEXT = 4000

RISK_PROFILES = {
    "conservative": {
        "label": "ระมัดระวัง",
        "risk_per_trade": 0.01,
        "max_total_risk": 0.04,
        "max_positions": 3,
        "max_weight": 0.25,
        "min_score": 70,
    },
    "balanced": {
        "label": "สมดุล",
        "risk_per_trade": 0.02,
        "max_total_risk": 0.07,
        "max_positions": 4,
        "max_weight": 0.35,
        "min_score": 60,
    },
    "aggressive": {
        "label": "เชิงรุก",
        "risk_per_trade": 0.03,
        "max_total_risk": 0.12,
        "max_positions": 5,
        "max_weight": 0.45,
        "min_score": 55,
    },
}

DEFAULT_RISK_MODE = os.getenv("RISK_MODE", "balanced").lower()
if DEFAULT_RISK_MODE not in RISK_PROFILES:
    raise ValueError("RISK_MODE ต้องเป็น conservative, balanced หรือ aggressive")

FALLBACK_SYMBOLS = """
ADVANC AOT AWC BANPU BBL BDMS BH BJC BTS CBG
CENTEL COM7 CPALL CPAXT CPF CPN CRC DELTA EA EGCO
GLOBAL GPSC GULF HMPRO ITC IVL KBANK KKP KTB KTC
LH MINT MTC OR OSP PTT PTTEP PTTGC RATCH SAWAD
SCB SCC SCGP TCAP TIDLOR TISCO TOP TRUE TU WHA
""".split()

# กลุ่มเพื่อใช้วิเคราะห์ภายใน ไม่ใช่การยืนยันหมวดอุตสาหกรรมทางการ
SECTOR_GROUPS = {
    "สื่อสาร": "ADVANC TRUE",
    "ธนาคาร": "BBL KBANK KKP KTB SCB TISCO",
    "พาณิชย์": "BJC COM7 CPALL CPAXT CRC GLOBAL HMPRO",
    "พลังงานและสาธารณูปโภค": (
        "BANPU EA EGCO GPSC GULF OR PTT PTTEP RATCH TOP"
    ),
    "อาหารและเครื่องดื่ม": "CBG CPF ITC OSP TU",
    "อสังหาริมทรัพย์": "AWC CPN LH WHA",
    "การแพทย์": "BDMS BH",
    "เงินทุน": "KTC MTC SAWAD TCAP TIDLOR",
    "ขนส่ง": "AOT BTS",
    "ท่องเที่ยวและบริการ": "CENTEL MINT",
    "ปิโตรเคมี": "IVL PTTGC",
    "วัสดุก่อสร้าง": "SCC",
    "บรรจุภัณฑ์": "SCGP",
    "อิเล็กทรอนิกส์": "DELTA",
}


# =========================================================
# UTILITIES
# =========================================================

def now_bkk():
    return datetime.now(BKK)


def utc_text():
    return datetime.now(timezone.utc).isoformat()


def symbol_name(symbol):
    return str(symbol).removesuffix(".BK")


def normalize_symbol(value):
    symbol = str(value).strip().upper()
    if not symbol or not re.fullmatch(r"[A-Z0-9^&._-]{1,30}", symbol):
        raise ValueError("ชื่อหุ้นไม่ถูกต้อง")
    if not symbol.startswith("^") and not symbol.endswith(".BK"):
        symbol += ".BK"
    return symbol


def unique_symbols(values):
    return list(dict.fromkeys(normalize_symbol(v) for v in values))


raw_universe = os.getenv("SET50_SYMBOLS", "").replace(",", " ").split()
UNIVERSE = unique_symbols(raw_universe or FALLBACK_SYMBOLS)
SECTOR_MAP = {
    normalize_symbol(symbol): sector
    for sector, symbols in SECTOR_GROUPS.items()
    for symbol in symbols.split()
}

STRATEGY_ID = VERSION + "-" + hashlib.sha256(
    json.dumps(
        {
            "universe": UNIVERSE,
            "sectors": SECTOR_MAP,
            "profiles": RISK_PROFILES,
            "min_breadth": MIN_BREADTH,
            "min_value": MIN_AVG_VALUE,
            "atr": [MIN_ATR_PCT, MAX_ATR_PCT],
            "extension": MAX_EXTENSION_PCT,
            "horizon": SIGNAL_HORIZON,
            "slippage": SLIPPAGE,
            "fees": [COMMISSION_RATE, VAT_RATE, MIN_COMMISSION],
            "max_entry_gap": MAX_ENTRY_GAP,
        },
        sort_keys=True,
    ).encode()
).hexdigest()[:10]


def finite_number(value, name="ตัวเลข", minimum=0, maximum=1e12):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} ต้องเป็นตัวเลข")
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} อยู่นอกช่วงที่อนุญาต")
    return result


def positive_number(value, name):
    result = finite_number(value, name)
    if result <= 0:
        raise ValueError(f"{name} ต้องมากกว่า 0")
    return result


def positive_integer(value, name, maximum=1000000000):
    text = str(value)
    if not re.fullmatch(r"\d+", text):
        raise ValueError(f"{name} ต้องเป็นจำนวนเต็มบวก")
    result = int(text)
    if not 1 <= result <= maximum:
        raise ValueError(f"{name} อยู่นอกช่วงที่อนุญาต")
    return result


def parse_time(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def commission(value, config=None):
    if value <= 0:
        return 0.0
    cfg = config or {
        "rate": COMMISSION_RATE,
        "vat": VAT_RATE,
        "minimum": MIN_COMMISSION,
    }
    return round(
        max(value * cfg["rate"], cfg["minimum"]) * (1 + cfg["vat"]), 2
    )


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def split_text(text):
    chunks = []
    current = ""
    for line in str(text).splitlines():
        while len(line) > MAX_TEXT:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:MAX_TEXT])
            line = line[MAX_TEXT:]
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > MAX_TEXT:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or ["ไม่มีข้อมูล"]


line_api = LineBotApi(TOKEN)
line_handler = WebhookHandler(SECRET)
app = Flask(__name__)


def push_text(user_id, text):
    for chunk in split_text(text):
        line_api.push_message(user_id, TextSendMessage(text=chunk))


# =========================================================
# DATABASE
# =========================================================

class StorageError(RuntimeError):
    pass


def connect_db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


@contextmanager
def transaction():
    connection = connect_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise StorageError("ฐานข้อมูลไม่พร้อมใช้งาน") from error
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def default_portfolio():
    return {
        "schema_version": VERSION,
        "cash": 10000.0,
        "initial_capital": 10000.0,
        "contributions_total": 0.0,
        "withdrawals_total": 0.0,
        "realized_pnl_total": 0.0,
        "fees_total": 0.0,
        "peak_net_pnl": 0.0,
        "risk_mode": DEFAULT_RISK_MODE,
        "plan_start_equity": 10000.0,
        "plan_start_date": utc_text(),
        "financial_plan": {
            "target_value": 20000.0,
            "target_months": 3,
            "max_portfolio_drawdown_pct": 0.20,
            "trading_paused_at_value": 8000.0,
        },
        "stocks": {},
        "watchlist": UNIVERSE.copy(),
        "transactions": [],
    }


def normalize_portfolio(raw):
    if not isinstance(raw, dict):
        raise StorageError("รูปแบบพอร์ตไม่ถูกต้อง ไม่ได้รีเซ็ตข้อมูล")

    result = default_portfolio()
    result.update(copy.deepcopy(raw))

    try:
        for key in [
            "cash", "initial_capital", "contributions_total",
            "withdrawals_total", "fees_total", "plan_start_equity",
        ]:
            result[key] = finite_number(result[key], key)

        result["realized_pnl_total"] = finite_number(
            result["realized_pnl_total"], "realized_pnl_total", -1e12
        )

        if "peak_net_pnl" not in raw:
            base = (
                result["initial_capital"]
                + result["contributions_total"]
                - result["withdrawals_total"]
            )
            old_peak = finite_number(
                raw.get("high_water_mark", max(base, 0)),
                "high_water_mark",
            )
            result["peak_net_pnl"] = max(0.0, old_peak - base)

        result["peak_net_pnl"] = finite_number(
            result["peak_net_pnl"], "peak_net_pnl"
        )

        if result["risk_mode"] not in RISK_PROFILES:
            result["risk_mode"] = DEFAULT_RISK_MODE

        if not isinstance(result["stocks"], dict):
            raise ValueError("stocks ต้องเป็น object")
        if not isinstance(result["transactions"], list):
            raise ValueError("transactions ต้องเป็น list")
        if not all(isinstance(t, dict) for t in result["transactions"]):
            raise ValueError("รูปแบบธุรกรรมไม่ถูกต้อง")
        if not isinstance(result["watchlist"], list):
            raise ValueError("watchlist ต้องเป็น list")

        result["watchlist"] = unique_symbols(result["watchlist"])

        stocks = {}
        for symbol, old in result["stocks"].items():
            if not isinstance(old, dict):
                raise ValueError("รูปแบบหุ้นในพอร์ตไม่ถูกต้อง")
            info = copy.deepcopy(old)
            quantity = positive_integer(info.get("shares"), "จำนวนหุ้น")
            average = finite_number(info.get("average_cost", 0), "ต้นทุน")
            total = finite_number(
                info.get("total_cost", average * quantity), "ต้นทุนรวม"
            )
            info.update({
                "shares": quantity,
                "total_cost": total,
                "average_cost": total / quantity,
                "stop_loss": finite_number(
                    info.get("stop_loss", 0), "Stop"
                ),
                "target_price": finite_number(
                    info.get("target_price", 0), "เป้าหมาย"
                ),
                "r_unit": finite_number(info.get("r_unit", 0), "R"),
                "highest_price": finite_number(
                    info.get("highest_price", average), "ราคาสูงสุด"
                ),
                # ไม่เติมวันที่ซื้อปัจจุบันให้ข้อมูลเก่าที่ไม่มีวันที่
                "opened_at": info.get("opened_at"),
            })
            stocks[normalize_symbol(symbol)] = info
        result["stocks"] = stocks

        plan = default_portfolio()["financial_plan"]
        if not isinstance(result["financial_plan"], dict):
            raise ValueError("แผนการเงินไม่ถูกต้อง")
        plan.update(result["financial_plan"])
        plan["target_value"] = positive_number(
            plan["target_value"], "เป้าหมาย"
        )
        plan["target_months"] = positive_integer(
            plan["target_months"], "จำนวนเดือน", 1200
        )
        plan["max_portfolio_drawdown_pct"] = finite_number(
            plan["max_portfolio_drawdown_pct"], "Drawdown", 0.001, 1
        )
        plan["trading_paused_at_value"] = finite_number(
            plan["trading_paused_at_value"], "มูลค่าหยุดซื้อ"
        )
        result["financial_plan"] = plan
    except (ValueError, TypeError, KeyError) as error:
        raise StorageError(
            f"ข้อมูลพอร์ตไม่ถูกต้อง: {error} — ไม่ได้รีเซ็ตข้อมูล"
        ) from error

    result["schema_version"] = VERSION
    return result


def read_portfolio_in_transaction(connection, user_id):
    row = connection.execute(
        "SELECT data FROM portfolios WHERE user_id=?", (user_id,)
    ).fetchone()
    if row:
        return normalize_portfolio(json.loads(row["data"]))

    portfolio = default_portfolio()
    connection.execute(
        "INSERT INTO portfolios(user_id, data) VALUES (?, ?)",
        (user_id, json_text(portfolio)),
    )
    return portfolio


def save_portfolio(connection, user_id, portfolio):
    connection.execute(
        "UPDATE portfolios SET data=? WHERE user_id=?",
        (json_text(normalize_portfolio(portfolio)), user_id),
    )


def get_portfolio(user_id):
    with transaction() as connection:
        return read_portfolio_in_transaction(connection, user_id)


def mutate_portfolio(user_id, event_id, callback):
    """
    อ่าน-แก้-บันทึกใน transaction เดียว
    callback ต้องไม่มีการโหลดข้อมูลจากเครือข่าย
    """
    with transaction() as connection:
        if event_id:
            previous = connection.execute(
                "SELECT user_id, reply FROM processed_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if previous:
                if previous["user_id"] != user_id:
                    raise StorageError("รหัสเหตุการณ์ไม่ตรงกับผู้ใช้")
                return "รายการนี้บันทึกไว้แล้ว ไม่ได้ทำซ้ำ\n" + previous["reply"]

        portfolio = read_portfolio_in_transaction(connection, user_id)
        reply = callback(portfolio)
        save_portfolio(connection, user_id, portfolio)

        if event_id:
            connection.execute(
                """
                INSERT INTO processed_events(event_id, user_id, reply, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, user_id, reply, utc_text()),
            )
        return reply


def initialize_database():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with closing(connect_db()) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portfolios (
                user_id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                reply TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                data TEXT NOT NULL,
                UNIQUE(user_id, signal_key)
            );
            CREATE INDEX IF NOT EXISTS signals_user_status
                ON signals(user_id, status);
            CREATE TABLE IF NOT EXISTS price_cache (
                symbol TEXT PRIMARY KEY,
                fetched_at REAL NOT NULL,
                data TEXT NOT NULL
            );
        """)
        connection.commit()

    # ย้าย JSON เดิมครั้งเดียว โดยไม่ลบหรือแก้ไฟล์ต้นฉบับ
    with transaction() as connection:
        done = connection.execute(
            "SELECT value FROM meta WHERE key='legacy_migration'"
        ).fetchone()
        if done:
            return

        if os.path.exists(LEGACY_FILE):
            try:
                with open(LEGACY_FILE, "r", encoding="utf-8") as file:
                    old_data = json.load(file)
            except (OSError, json.JSONDecodeError) as error:
                raise StorageError(
                    "อ่าน JSON เดิมไม่สำเร็จ หยุดย้ายข้อมูลโดยไม่รีเซ็ตพอร์ต"
                ) from error

            if not isinstance(old_data, dict):
                raise StorageError("JSON เดิมต้องเป็น object")

            if "cash" in old_data or "stocks" in old_data:
                if not LEGACY_USER_ID:
                    raise StorageError(
                        "พบพอร์ตเก่าแบบไม่มีเจ้าของ "
                        "ต้องตั้ง LEGACY_USER_ID ของเจ้าของก่อนย้ายข้อมูล"
                    )
                old_data = {LEGACY_USER_ID: old_data}

            for user_id, raw in old_data.items():
                if not isinstance(user_id, str) or not user_id:
                    raise StorageError("เจ้าของพอร์ตเก่าไม่ถูกต้อง")
                portfolio = normalize_portfolio(raw)
                old_signals = portfolio.pop("signal_history", [])

                existing = connection.execute(
                    "SELECT 1 FROM portfolios WHERE user_id=?", (user_id,)
                ).fetchone()
                if existing:
                    raise StorageError(
                        "มีพอร์ตในฐานข้อมูลแล้ว ไม่ย้าย JSON ทับอัตโนมัติ"
                    )

                connection.execute(
                    "INSERT INTO portfolios(user_id, data) VALUES (?, ?)",
                    (user_id, json_text(portfolio)),
                )

                if not isinstance(old_signals, list):
                    raise StorageError("ประวัติสัญญาณเก่าไม่ถูกต้อง")

                for item in old_signals:
                    if not isinstance(item, dict):
                        raise StorageError("รายการสัญญาณเก่าไม่ถูกต้อง")
                    signal_id = uuid.uuid4().hex
                    payload = copy.deepcopy(item)
                    payload["legacy"] = True
                    payload["note"] = (
                        "ไม่มีเวลาส่งและกติกาประเมินครบ "
                        "จึงไม่นับรวมกับสถิติรุ่นใหม่"
                    )
                    connection.execute(
                        """
                        INSERT INTO signals
                        (id, user_id, signal_key, symbol, status, data)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            signal_id, user_id, "legacy-" + signal_id,
                            str(item.get("symbol", "")), "LEGACY",
                            json_text(payload),
                        ),
                    )

        connection.execute(
            "INSERT INTO meta(key, value) VALUES ('legacy_migration', ?)",
            (utc_text(),),
        )


initialize_database()


# =========================================================
# MARKET DATA
# =========================================================

# แคชของ yfinance เป็นข้อมูลชั่วคราว ไม่ใช่ข้อมูลพอร์ต
yf_cache_dir = tempfile.mkdtemp(prefix="portfolio_yf_")
try:
    yf.set_tz_cache_location(yf_cache_dir)
except Exception:
    logger.warning("ตั้งตำแหน่งแคช yfinance ไม่สำเร็จ")

download_lock = threading.Lock()
retry_after = {}
provider_pause_until = 0.0


def normalize_daily_frame(frame):
    if frame is None or frame.empty:
        return None

    required = ["Open", "High", "Low", "Close", "Volume"]
    if not set(required).issubset(frame.columns):
        return None

    columns = required + [
        name for name in ["Dividends", "Stock Splits"] if name in frame.columns
    ]
    data = frame[columns].copy()
    index = pd.DatetimeIndex(pd.to_datetime(data.index))

    if index.tz is not None:
        index = index.tz_convert("Asia/Bangkok").tz_localize(None)

    data.index = index.normalize()
    data = data.sort_index()
    data = data[~data.index.duplicated(keep="last")]

    for column in columns:
        data[column] = pd.to_numeric(data[column], errors="coerce")

    # yf.download อาจจัดแนว index รวมแล้วเกิดแถวว่างของบางหุ้น
    data = data.dropna(subset=["Close"])
    data = data.replace([float("inf"), -float("inf")], float("nan"))

    if data.empty:
        return None
    if data[required].isna().any().any():
        return None

    valid = (
        (data["Open"] > 0)
        & (data["High"] > 0)
        & (data["Low"] > 0)
        & (data["Close"] > 0)
        & (data["Volume"] >= 0)
        & (data["High"] >= data["Low"])
        & (data["High"] >= data["Open"])
        & (data["High"] >= data["Close"])
        & (data["Low"] <= data["Open"])
        & (data["Low"] <= data["Close"])
    )
    if not valid.all():
        return None

    for column in ["Dividends", "Stock Splits"]:
        if column in data.columns:
            data[column] = data[column].fillna(0.0)

    return data


def extract_frame(downloaded, symbol, count):
    if downloaded is None or downloaded.empty:
        return None

    if isinstance(downloaded.columns, pd.MultiIndex):
        frame = None
        for level in range(downloaded.columns.nlevels):
            if symbol in downloaded.columns.get_level_values(level):
                frame = downloaded.xs(
                    symbol, axis=1, level=level
                ).copy()
                break
        if frame is None:
            return None
    elif count == 1:
        frame = downloaded.copy()
    else:
        return None

    return normalize_daily_frame(frame)


def encode_frame(frame):
    payload = frame.to_dict(orient="split")
    payload["index"] = [
        timestamp.strftime("%Y-%m-%d") for timestamp in frame.index
    ]
    return json_text(payload)


def decode_frame(text):
    payload = json.loads(text)
    return normalize_daily_frame(
        pd.DataFrame(
            payload["data"],
            columns=payload["columns"],
            index=pd.to_datetime(payload["index"]),
        )
    )


def completed_bars(frame, at=None):
    if frame is None or frame.empty:
        return None

    at = at or now_bkk()
    today = pd.Timestamp(at.date())

    # เป็นเวลาตัดข้อมูลของบอต ไม่ใช่ปฏิทินตลาดอย่างเป็นทางการ
    if (at.hour, at.minute) < (18, 0):
        result = frame[frame.index < today]
    else:
        result = frame[frame.index <= today]

    return result.copy() if not result.empty else None


def get_market_frames(symbols):
    """
    ล็อกทั้งรอบดาวน์โหลดเพื่อไม่ให้หลายงานขอข้อมูลซ้ำพร้อมกัน
    ไม่เขียนทับแคชที่ดีด้วยข้อมูลว่าง และไม่ลองซ้ำทันที
    """
    global provider_pause_until

    requested = unique_symbols(symbols)
    results = {}

    with download_lock:
        now = time.time()
        missing = []

        with closing(connect_db()) as connection:
            for symbol in requested:
                row = connection.execute(
                    "SELECT fetched_at, data FROM price_cache WHERE symbol=?",
                    (symbol,),
                ).fetchone()

                cached = None
                if row and now - row["fetched_at"] <= CACHE_SECONDS:
                    try:
                        cached = decode_frame(row["data"])
                    except (ValueError, KeyError, TypeError):
                        cached = None

                if cached is not None:
                    results[symbol] = cached
                elif now >= retry_after.get(symbol, 0):
                    missing.append(symbol)

        if now < provider_pause_until:
            return {
                symbol: bars
                for symbol, frame in results.items()
                if (bars := completed_bars(frame)) is not None
            }

        for offset in range(0, len(missing), 8):
            chunk = missing[offset:offset + 8]
            downloaded = None
            rate_limited = False

            try:
                downloaded = yf.download(
                    tickers=chunk,
                    period="2y",
                    interval="1d",
                    auto_adjust=False,
                    actions=True,
                    group_by="ticker",
                    threads=False,
                    progress=False,
                    timeout=20,
                )
            except Exception as error:
                error_name = type(error).__name__
                rate_limited = (
                    "RateLimit" in error_name
                    or "429" in str(error)
                    or "Too Many Requests" in str(error)
                )
                logger.warning("ดาวน์โหลดราคาไม่สำเร็จ: %s", error_name)

            failed = 0
            for symbol in chunk:
                try:
                    frame = extract_frame(downloaded, symbol, len(chunk))
                except (ValueError, TypeError, KeyError, IndexError):
                    frame = None

                if frame is None:
                    failed += 1
                    retry_after[symbol] = time.time() + 900
                    continue

                results[symbol] = frame
                retry_after.pop(symbol, None)
                with transaction() as connection:
                    connection.execute(
                        """
                        INSERT INTO price_cache(symbol, fetched_at, data)
                        VALUES (?, ?, ?)
                        ON CONFLICT(symbol) DO UPDATE SET
                            fetched_at=excluded.fetched_at,
                            data=excluded.data
                        """,
                        (symbol, time.time(), encode_frame(frame)),
                    )

            # yfinance บางกรณีคืนข้อมูลว่างแทนการ raise
            if rate_limited or failed == len(chunk):
                provider_pause_until = time.time() + 900
                logger.warning("พักการเรียกราคา 15 นาที เพราะดาวน์โหลดล้มเหลว")
                break

            if offset + 8 < len(missing):
                time.sleep(1.0)

    return {
        symbol: bars
        for symbol, frame in results.items()
        if (bars := completed_bars(frame)) is not None
    }


# =========================================================
# INDICATORS AND SHARED MARKET CONTEXT
# =========================================================

def rsi_wilder(close, period=14):
    changes = close.diff().dropna().tolist()
    if len(changes) < period:
        return None

    gain = sum(max(x, 0.0) for x in changes[:period]) / period
    loss = sum(max(-x, 0.0) for x in changes[:period]) / period

    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period

    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def snapshot(frame):
    if frame is None or len(frame) < 90:
        return None

    close = frame["Close"]
    price = float(close.iloc[-1])
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    macd = (
        close.ewm(span=12, adjust=False).mean()
        - close.ewm(span=26, adjust=False).mean()
    )
    histogram = macd - macd.ewm(span=9, adjust=False).mean()

    previous = close.shift(1)
    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - previous).abs(),
            (frame["Low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = float(true_range.rolling(14).mean().iloc[-1])
    if not math.isfinite(atr) or atr <= 0:
        return None

    prior20 = frame.iloc[-21:-1]
    average_volume = float(prior20["Volume"].mean())
    volume_ratio = (
        float(frame["Volume"].iloc[-1]) / average_volume
        if average_volume > 0 else 0.0
    )

    weekly = close.resample("W-FRI").last().dropna()
    weekly = weekly[weekly.index <= frame.index[-1]]
    weekly_up = None
    if len(weekly) >= 26:
        weekly_ema = weekly.ewm(span=20, adjust=False).mean()
        weekly_up = bool(
            weekly.iloc[-1] > weekly_ema.iloc[-1]
            and weekly_ema.iloc[-1] >= weekly_ema.iloc[-2]
        )

    middle = close.rolling(20).mean()
    deviation = close.rolling(20).std(ddof=0)
    upper = middle + 2 * deviation
    lower = middle - 2 * deviation
    bandwidth = (upper - lower) / middle
    threshold = (
        bandwidth.rolling(120, min_periods=60)
        .quantile(0.2)
        .shift(1)
    )
    squeezed = bandwidth <= threshold

    last = frame.iloc[-1]
    candle_range = float(last["High"] - last["Low"])
    strong_close = bool(
        candle_range > 0
        and (float(last["Close"]) - float(last["Low"])) / candle_range >= 0.65
        and float(last["Close"]) > float(last["Open"])
    )

    squeeze_release = bool(
        squeezed.iloc[-6:-1].any()
        and price > float(upper.iloc[-2])
        and bandwidth.iloc[-1] > bandwidth.iloc[-2]
        and volume_ratio >= 1.3
        and strong_close
    )

    return {
        "data_date": frame.index[-1].strftime("%Y-%m-%d"),
        "price": price,
        "ema20": float(ema20.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "weekly_up": weekly_up,
        "rsi": rsi_wilder(close),
        "atr": atr,
        "atr_pct": atr / price * 100,
        "macd_hist": float(histogram.iloc[-1]),
        "macd_improving": bool(histogram.iloc[-1] > histogram.iloc[-2]),
        "volume_ratio": volume_ratio,
        "average_volume": average_volume,
        "average_value": float(
            (prior20["Close"] * prior20["Volume"]).mean()
        ),
        "prior_high20": float(prior20["High"].max()),
        "prior_low10": float(frame["Low"].iloc[-11:-1].min()),
        "ret20": (price / float(close.iloc[-21]) - 1) * 100,
        "extension": (price / float(ema20.iloc[-1]) - 1) * 100,
        "strong_close": strong_close,
        "squeeze": bool(squeezed.iloc[-1]),
        "squeeze_release": squeeze_release,
        "last_low": float(last["Low"]),
    }


def has_actions(frame):
    return (
        frame is not None
        and {"Dividends", "Stock Splits"}.issubset(frame.columns)
    )


def corporate_event(frame):
    if not has_actions(frame):
        return False
    return bool(
        (frame["Dividends"].fillna(0) != 0).any()
        or (frame["Stock Splits"].fillna(0) != 0).any()
    )


def market_context(frames):
    snapshots = {}
    for symbol, frame in frames.items():
        snap = snapshot(frame)
        if snap:
            snapshots[symbol] = snap

    dates = [
        snapshots[s]["data_date"]
        for s in UNIVERSE if s in snapshots
    ]
    asof = max(dates) if dates else None
    sample = {
        symbol: snapshots[symbol]
        for symbol in UNIVERSE
        if symbol in snapshots and snapshots[symbol]["data_date"] == asof
    }

    coverage = len(sample) / len(UNIVERSE) if UNIVERSE else 0
    breadth = (
        100 * sum(s["price"] > s["ema50"] for s in sample.values()) / len(sample)
        if sample else None
    )

    index = snapshots.get(INDEX_SYMBOL)
    index_valid = bool(index and index["data_date"] == asof)

    issues = []
    if len(sample) < 20:
        issues.append("หุ้นอ้างอิงที่ใช้ได้ต่ำกว่า 20 ตัว")
    if coverage < MIN_COVERAGE:
        issues.append("ข้อมูลหุ้นอ้างอิงไม่ครบตามเกณฑ์")
    if not index_valid:
        issues.append("ข้อมูลดัชนีขาดหายหรือคนละวันกับหุ้น")
    if asof is None:
        issues.append("ไม่มีข้อมูลวันที่อ้างอิง")
    elif (
        now_bkk().date() - datetime.fromisoformat(asof).date()
    ).days > MAX_DATA_AGE_DAYS:
        issues.append("ข้อมูลเก่าเกินเกณฑ์")

    groups = defaultdict(list)
    for symbol, snap in sample.items():
        sector = SECTOR_MAP.get(symbol)
        if sector:
            groups[sector].append(snap["ret20"])

    sector_returns = {
        sector: float(pd.Series(values).median())
        for sector, values in groups.items()
        if len(values) >= 3
    }
    leaders = sorted(
        [sector for sector, value in sector_returns.items() if value > 0],
        key=lambda sector: sector_returns[sector],
        reverse=True,
    )[:3]

    positive = bool(
        not issues
        and index_valid
        and index["price"] > index["ema50"]
        and breadth is not None
        and breadth >= MIN_BREADTH
    )

    return {
        "asof": asof,
        "snapshots": snapshots,
        "index": index if index_valid else None,
        "coverage": coverage,
        "sample_count": len(sample),
        "breadth": breadth,
        "leaders": leaders,
        "issues": issues,
        "positive": positive,
    }


def relative_strength(frame, index_frame, asof):
    if frame is None or index_frame is None:
        return None
    common = pd.concat(
        [
            frame["Close"].rename("stock"),
            index_frame["Close"].rename("index"),
        ],
        axis=1,
        join="inner",
    ).dropna()

    if len(common) < 21 or common.index[-1].strftime("%Y-%m-%d") != asof:
        return None

    stock_return = (
        common["stock"].iloc[-1] / common["stock"].iloc[-21] - 1
    ) * 100
    index_return = (
        common["index"].iloc[-1] / common["index"].iloc[-21] - 1
    ) * 100
    return float(stock_return - index_return)


def analyse_symbol(symbol, frames, context, minimum_score):
    snap = context["snapshots"].get(symbol)
    if not snap:
        return None

    frame = frames[symbol]
    sector = SECTOR_MAP.get(symbol, "ไม่ระบุกลุ่ม")
    rs = relative_strength(
        frame, frames.get(INDEX_SYMBOL), snap["data_date"]
    )

    daily_up = snap["price"] > snap["ema20"] > snap["ema50"]
    setup = None

    if daily_up:
        if (
            snap["price"] > snap["prior_high20"]
            and snap["volume_ratio"] >= 1.3
            and snap["strong_close"]
        ):
            setup = "BREAKOUT"
        elif (
            snap["last_low"] <= snap["ema20"] * 1.015
            and snap["price"] >= snap["ema20"]
            and snap["macd_improving"]
            and snap["strong_close"]
            and 40 <= snap["rsi"] <= 65
        ):
            setup = "PULLBACK"
        elif (
            snap["macd_hist"] > 0
            and snap["macd_improving"]
            and snap["strong_close"]
            and 52 <= snap["rsi"] <= 72
        ):
            setup = "MOMENTUM"

    score = 0
    reasons = []

    tests = [
        (daily_up, 20, "ราคายืนเหนือ EMA20 และ EMA50"),
        (snap["weekly_up"] is True, 15, "แนวโน้มสัปดาห์ที่จบแล้วเป็นบวก"),
        (
            rs is not None and rs > 0, 15,
            f"ผลตอบแทนเทียบดัชนี 20 วัน: {rs:+.1f} จุดเปอร์เซ็นต์"
            if rs is not None else "ยังไม่มีค่าเทียบดัชนี",
        ),
        (sector in context["leaders"], 10, "กลุ่มเด่นในชุดหุ้นอ้างอิง"),
        (
            snap["macd_hist"] > 0 and snap["macd_improving"],
            10, "MACD เป็นบวกและเพิ่มขึ้น",
        ),
        (50 <= snap["rsi"] <= 70, 5, "RSI อยู่ในช่วงที่กำหนด"),
        (
            snap["volume_ratio"] >= 1.3, 10,
            f"Volume {snap['volume_ratio']:.2f} เท่าของค่าเฉลี่ย",
        ),
        (setup is not None, 10, "พบรูปแบบเข้าซื้อตามกฎ"),
        (snap["strong_close"], 5, "แท่งเทียนปิดแข็งแกร่ง"),
    ]

    for passed, points, reason in tests:
        if passed:
            score += points
            reasons.append(reason)

    if snap["squeeze_release"]:
        reasons.append("พบการเบรกขึ้นหลัง Squeeze พร้อม Volume")
    elif snap["squeeze"]:
        reasons.append("พบ Squeeze แต่ยังไม่ยืนยันว่าจะขึ้น")

    stop = round(
        min(snap["prior_low10"] * 0.995, snap["price"] - 1.5 * snap["atr"]),
        2,
    )
    target = round(snap["price"] + 2 * (snap["price"] - stop), 2)

    blockers = list(context["issues"])
    if not context["positive"] and not context["issues"]:
        blockers.append("ภาวะตลาดไม่ผ่านเกณฑ์เปิดสถานะใหม่")
    if snap["data_date"] != context["asof"]:
        blockers.append("วันที่ข้อมูลหุ้นไม่ตรงวันอ้างอิง")
    if snap["weekly_up"] is not True:
        blockers.append("Weekly ไม่ผ่านหรือข้อมูลไม่พอ")
    if rs is None:
        blockers.append("ยังเปรียบเทียบกับดัชนีไม่ได้")
    if not setup:
        blockers.append("ยังไม่มีรูปแบบเข้าซื้อ")
    if snap["average_value"] < MIN_AVG_VALUE:
        blockers.append("สภาพคล่องต่ำกว่าเกณฑ์")
    if not MIN_ATR_PCT <= snap["atr_pct"] <= MAX_ATR_PCT:
        blockers.append("ATR อยู่นอกช่วงที่กำหนด")
    if snap["extension"] > MAX_EXTENSION_PCT:
        blockers.append("ราคาห่าง EMA20 มากเกินไป")
    if snap["rsi"] >= 80:
        blockers.append("RSI สูงเกินเกณฑ์")
    if score < minimum_score:
        blockers.append(f"คะแนนต่ำกว่า {minimum_score}")
    if (
        stop <= 0 or stop >= snap["price"]
        or (snap["price"] - stop) / snap["price"] > 0.10
    ):
        blockers.append("ระยะ Stop ไม่เหมาะกับเกณฑ์นี้")
    if not has_actions(frame):
        blockers.append("ข้อมูลปันผล/แตกหุ้นไม่ครบ")
    elif corporate_event(frame.tail(5)):
        blockers.append("มีปันผลหรือแตกหุ้นช่วงล่าสุด ต้องตรวจปรับข้อมูล")

    return {
        **snap,
        "symbol": symbol,
        "sector": sector,
        "rs": rs,
        "setup": setup,
        "score": score,
        "stop": stop,
        "target": target,
        "reasons": reasons,
        "blockers": list(dict.fromkeys(blockers)),
    }


# =========================================================
# PORTFOLIO VALUATION AND RISK
# =========================================================

def latest_price(frame):
    if frame is None or frame.empty:
        return None
    age = (now_bkk().date() - frame.index[-1].date()).days
    if age > MAX_DATA_AGE_DAYS:
        return None
    price = float(frame["Close"].iloc[-1])
    return price if math.isfinite(price) and price > 0 else None


def holding_corporate_warning(info, frame):
    if not has_actions(frame):
        return "ไม่มีข้อมูลปันผล/แตกหุ้นครบถ้วน"
    if not info.get("opened_at"):
        return "ไม่มีวันที่เปิดสถานะเดิม ต้องตรวจประวัติด้วยตนเอง"

    try:
        opened = parse_time(info["opened_at"]).astimezone(BKK).date()
    except (ValueError, TypeError):
        return "วันที่เปิดสถานะเดิมไม่ถูกต้อง"

    if corporate_event(frame[frame.index.date >= opened]):
        return "พบปันผล/แตกหุ้นหลังเปิดสถานะ ต้องตรวจต้นทุน จำนวนหุ้น และแผน"
    return None


def portfolio_state(portfolio, frames):
    equity = portfolio["cash"]
    stock_value = 0.0
    open_risk = 0.0
    sector_values = defaultdict(float)
    issues = []
    risk_issues = []

    for symbol, info in portfolio["stocks"].items():
        frame = frames.get(symbol)
        price = latest_price(frame)
        if price is None:
            issues.append(f"{symbol_name(symbol)} ไม่มีราคาที่ใช้ประเมินได้")
            continue

        value = price * info["shares"]
        stock_value += value
        sector_values[SECTOR_MAP.get(symbol, "ไม่ระบุกลุ่ม")] += value

        warning = holding_corporate_warning(info, frame)
        if warning:
            risk_issues.append(f"{symbol_name(symbol)}: {warning}")

        stop = info.get("stop_loss", 0)
        if stop <= 0:
            risk_issues.append(f"{symbol_name(symbol)} ยังไม่มี Stop")
            open_risk += value
        elif price <= stop:
            risk_issues.append(f"{symbol_name(symbol)} เข้าเงื่อนไข Stop เดิม")
        else:
            open_risk += (
                price - stop * (1 - SLIPPAGE)
            ) * info["shares"]
            open_risk += commission(stop * info["shares"])

    if issues:
        return {
            "equity": None,
            "stock_value": None,
            "issues": issues,
            "risk_issues": risk_issues,
            "sector_values": sector_values,
            "open_risk": None,
        }

    equity += stock_value
    base = (
        portfolio["initial_capital"]
        + portfolio["contributions_total"]
        - portfolio["withdrawals_total"]
    )
    net_pnl = equity - base
    peak_pnl = max(portfolio["peak_net_pnl"], net_pnl, 0.0)
    peak_equity = base + peak_pnl
    drawdown = (
        max(0.0, (peak_pnl - net_pnl) / peak_equity)
        if peak_equity > 0 else 0.0
    )

    plan = portfolio["financial_plan"]
    paused = (
        equity <= plan["trading_paused_at_value"]
        or drawdown >= plan["max_portfolio_drawdown_pct"]
    )

    return {
        "equity": equity,
        "stock_value": stock_value,
        "net_pnl": net_pnl,
        "peak_net_pnl": peak_pnl,
        "drawdown": drawdown,
        "paused": paused,
        "issues": [],
        "risk_issues": risk_issues,
        "sector_values": sector_values,
        "open_risk": open_risk,
    }


def refresh_peak(user_id, frames):
    # ใช้ข้อมูลพอร์ตล่าสุดใน transaction โดยไม่เรียกเครือข่ายในนี้
    with transaction() as connection:
        portfolio = read_portfolio_in_transaction(connection, user_id)
        state = portfolio_state(portfolio, frames)
        if state["equity"] is not None and not state["risk_issues"]:
            if state["peak_net_pnl"] > portfolio["peak_net_pnl"]:
                portfolio["peak_net_pnl"] = state["peak_net_pnl"]
                save_portfolio(connection, user_id, portfolio)
        return portfolio, state


def risk_profile(portfolio, state):
    profile = dict(RISK_PROFILES[portfolio["risk_mode"]])
    multiplier = 1.0

    drawdown = state.get("drawdown", 0)
    if drawdown >= 0.15:
        multiplier *= 0.5
    elif drawdown >= 0.08:
        multiplier *= 0.7

    # เป็น streak ของรายการขาย ไม่ใช่รอบเทรดเต็ม
    streak = 0
    for event in reversed(portfolio["transactions"]):
        if event.get("type") != "SELL":
            continue
        if float(event.get("realized_pnl", 0)) < 0:
            streak += 1
        else:
            break

    if streak >= 3:
        multiplier *= 0.5
    elif streak == 2:
        multiplier *= 0.7

    profile["risk_per_trade"] *= multiplier
    profile["max_total_risk"] *= multiplier
    return profile


def trade_economics(entry, stop, target, quantity, costs=None, slippage=None):
    slippage = SLIPPAGE if slippage is None else slippage
    entry_fill = entry * (1 + slippage)
    stop_fill = stop * (1 - slippage)
    target_fill = target * (1 - slippage)

    cost = entry_fill * quantity + commission(entry_fill * quantity, costs)
    stop_net = stop_fill * quantity - commission(stop_fill * quantity, costs)
    target_net = target_fill * quantity - commission(target_fill * quantity, costs)

    return cost, max(0.0, cost - stop_net), target_net - cost


def allocate_candidates(results, portfolio, state, profile, limit):
    if state["equity"] is None or state["paused"] or state["risk_issues"]:
        return []

    slots = max(0, profile["max_positions"] - len(portfolio["stocks"]))
    cash = portfolio["cash"]
    open_risk = state["open_risk"]
    sector_values = defaultdict(float, state["sector_values"])
    equity = state["equity"]
    selected = []

    for result in sorted(results, key=lambda r: r["score"], reverse=True):
        if len(selected) >= min(limit, slots):
            break
        if result["blockers"] or result["symbol"] in portfolio["stocks"]:
            continue

        sector_room = max(
            0.0,
            equity * MAX_SECTOR_WEIGHT - sector_values[result["sector"]],
        )
        money_limit = min(
            cash, equity * profile["max_weight"], sector_room
        )
        risk_limit = min(
            equity * profile["risk_per_trade"],
            max(0.0, equity * profile["max_total_risk"] - open_risk),
        )

        cap = min(
            int(money_limit / (result["price"] * (1 + SLIPPAGE))),
            int(result["average_volume"] * 0.005),
        )
        step = (
            1 if ALLOW_ODD_LOT and cap < BOARD_LOT else BOARD_LOT
        )

        low, high = 0, max(0, cap // step)
        while low < high:
            middle = (low + high + 1) // 2
            quantity = middle * step
            cost, loss, _ = trade_economics(
                result["price"], result["stop"], result["target"], quantity
            )
            if cost <= money_limit and loss <= risk_limit:
                low = middle
            else:
                high = middle - 1

        quantity = low * step
        if quantity <= 0:
            continue

        cost, loss, profit = trade_economics(
            result["price"], result["stop"], result["target"], quantity
        )
        if loss <= 0 or profit / loss < MIN_NET_RR:
            continue

        item = dict(result)
        item.update({
            "quantity": quantity,
            "estimated_cost": cost,
            "estimated_loss": loss,
            "estimated_profit": profit,
        })
        selected.append(item)

        cash -= cost
        open_risk += loss
        sector_values[result["sector"]] += cost

    return selected


# =========================================================
# SIGNAL STORAGE AND CHRONOLOGICAL EVALUATION
# =========================================================

STATUS_LABELS = {
    "GENERATED": "จัดทำแล้ว ยังไม่ยืนยันการส่ง",
    "DELIVERY_UNKNOWN": "ไม่ยืนยันการส่ง ไม่รวมสถิติ",
    "PENDING": "กำลังติดตาม",
    "TARGET": "ถึงเป้าก่อน Stop",
    "STOP": "ถึง Stop ก่อนเป้า",
    "TIMEOUT": "ครบเวลาติดตาม",
    "AMBIGUOUS": "แตะทั้งสองระดับ แยกลำดับไม่ได้",
    "CANCELLED": "ไม่เข้าเงื่อนไขราคาจำลอง",
    "REVIEW": "ต้องตรวจข้อมูลเพิ่มเติม",
    "LEGACY": "ประวัติรุ่นเก่า ไม่รวมสถิติ",
}


def load_signals(user_id, status=None):
    query = "SELECT * FROM signals WHERE user_id=?"
    parameters = [user_id]
    if status:
        query += " AND status=?"
        parameters.append(status)
    query += " ORDER BY rowid"

    with closing(connect_db()) as connection:
        rows = connection.execute(query, parameters).fetchall()

    return [
        {
            "id": row["id"],
            "symbol": row["symbol"],
            "status": row["status"],
            "payload": json.loads(row["data"]),
        }
        for row in rows
    ]


def record_signals(user_id, selected, context):
    """
    บันทึกเฉพาะรายการที่นำไปแสดงเป็นแผนซื้อจริง
    สัญญาณเดิมที่ยัง PENDING จะไม่ถูกนับเป็นสัญญาณใหม่ซ้ำ
    """
    new_ids = []

    with transaction() as connection:
        for result in selected:
            signal_key = (
                f"{STRATEGY_ID}:{result['symbol']}:{result['data_date']}"
            )

            existing = connection.execute(
                """
                SELECT id FROM signals
                WHERE user_id=? AND
                    (signal_key=? OR
                     (symbol=? AND status IN ('PENDING', 'GENERATED')))
                LIMIT 1
                """,
                (user_id, signal_key, result["symbol"]),
            ).fetchone()

            if existing:
                result["signal_id"] = existing["id"]
                continue

            signal_id = uuid.uuid4().hex
            payload = {
                "created_at": utc_text(),
                "published_at": None,
                "strategy_id": STRATEGY_ID,
                "symbol": result["symbol"],
                "sector": result["sector"],
                "data_date": result["data_date"],
                "reference_price": result["price"],
                "stop": result["stop"],
                "target": result["target"],
                "quantity": result["quantity"],
                "setup": result["setup"],
                "score": result["score"],
                "rsi": result["rsi"],
                "volume_ratio": result["volume_ratio"],
                "atr": result["atr"],
                "resistance": result["prior_high20"],
                "reasons": result["reasons"],
                "market_breadth": context["breadth"],
                "market_index_positive": bool(
                    context["index"]
                    and context["index"]["price"] > context["index"]["ema50"]
                ),
                "horizon": SIGNAL_HORIZON,
                "max_entry_gap": MAX_ENTRY_GAP,
                "slippage": SLIPPAGE,
                "fees": {
                    "rate": COMMISSION_RATE,
                    "vat": VAT_RATE,
                    "minimum": MIN_COMMISSION,
                },
                "evaluation_rule": (
                    "ราคาเปิดวันซื้อขายถัดจากวันที่ส่งคำแนะนำ "
                    "ไม่ใช่ราคาที่ซื้อจริง"
                ),
            }

            connection.execute(
                """
                INSERT INTO signals
                (id, user_id, signal_key, symbol, status, data)
                VALUES (?, ?, ?, ?, 'GENERATED', ?)
                """,
                (
                    signal_id, user_id, signal_key,
                    result["symbol"], json_text(payload),
                ),
            )
            result["signal_id"] = signal_id
            new_ids.append(signal_id)

    return new_ids


def mark_signal_delivery(user_id, ids, accepted):
    if not ids:
        return

    with transaction() as connection:
        for signal_id in ids:
            row = connection.execute(
                """
                SELECT data FROM signals
                WHERE id=? AND user_id=? AND status='GENERATED'
                """,
                (signal_id, user_id),
            ).fetchone()
            if not row:
                continue

            payload = json.loads(row["data"])
            if accepted:
                published = now_bkk()
                payload["published_at"] = published.isoformat()
                # ไม่ใช้ High/Low ที่เกิดก่อนส่งคำแนะนำ
                payload["eligible_from"] = (
                    published.date() + timedelta(days=1)
                ).isoformat()

            connection.execute(
                "UPDATE signals SET status=?, data=? WHERE id=? AND user_id=?",
                (
                    "PENDING" if accepted else "DELIVERY_UNKNOWN",
                    json_text(payload), signal_id, user_id,
                ),
            )


def event_observations(payload, frame, index_frame, event_date):
    """
    เป็นข้อสังเกตถึงวันเกิดเหตุ ไม่ใช่ข้อพิสูจน์สาเหตุทางเศรษฐกิจ
    """
    observations = []
    cutoff = pd.Timestamp(event_date)
    history = frame[frame.index <= cutoff]
    snap = snapshot(history)

    if not history.empty:
        close = float(history["Close"].iloc[-1])
        if (
            payload.get("setup") == "BREAKOUT"
            and close < payload.get("resistance", 0)
        ):
            observations.append(
                "วันเกิดเหตุปิดต่ำกว่าแนวต้านเดิม "
                "สอดคล้องกับการเบรกที่ไม่ต่อเนื่อง"
            )

        if len(history) >= 2 and payload.get("atr", 0) > 0:
            last = history.iloc[-1]
            previous = float(history["Close"].iloc[-2])
            true_range = max(
                float(last["High"] - last["Low"]),
                abs(float(last["High"]) - previous),
                abs(float(last["Low"]) - previous),
            )
            ratio = true_range / payload["atr"]
            if ratio >= 1.5:
                observations.append(
                    f"ช่วงแกว่งวันเกิดเหตุเท่ากับ {ratio:.2f} "
                    "เท่าของ ATR ตอนแนะนำ"
                )

    if snap and snap["volume_ratio"] < 0.8:
        observations.append(
            f"Volume วันเกิดเหตุเหลือ {snap['volume_ratio']:.2f} "
            "เท่าของค่าเฉลี่ยก่อนหน้า"
        )

    if index_frame is not None:
        index_history = index_frame[index_frame.index <= cutoff]
        if (
            not index_history.empty
            and index_history.index[-1] == cutoff
        ):
            index_snap = snapshot(index_history)
            if index_snap and index_snap["price"] < index_snap["ema50"]:
                observations.append(
                    "ดัชนีวันเกิดเหตุปิดต่ำกว่า EMA50"
                )

    if not observations:
        observations.append(
            "ยังไม่มีหลักฐานจากกฎที่ตรวจเพียงพอจะระบุสาเหตุ"
        )

    return observations


def evaluate_signal(payload, frame, index_frame):
    if frame is None or index_frame is None:
        return None, "ข้อมูลหุ้นหรือดัชนีไม่พร้อม"

    eligible = payload.get("eligible_from")
    if not eligible:
        return None, "ยังไม่มีวันเริ่มติดตามที่ยืนยันได้"

    if not has_actions(frame):
        return None, "ยังไม่มีข้อมูลปันผล/แตกหุ้นครบ"

    latest_day = min(frame.index[-1], index_frame.index[-1])
    if (now_bkk().date() - latest_day.date()).days > MAX_DATA_AGE_DAYS:
        return None, "ข้อมูลล่าสุดเก่าเกินเกณฑ์"

    if frame.index[0].strftime("%Y-%m-%d") > payload["data_date"]:
        return None, "ช่วงข้อมูลไม่ครอบคลุมจุดเริ่มสัญญาณ"

    benchmark_days = index_frame.index[
        (index_frame.index >= pd.Timestamp(eligible))
        & (index_frame.index <= latest_day)
    ]
    if len(benchmark_days) == 0:
        return None, "รอแท่งรายวันหลังวันส่งคำแนะนำ"

    horizon = int(payload["horizon"])
    needed = benchmark_days[:horizon]
    entry_fill = None
    entry_cost = None
    risk_amount = None
    entry_day = None

    stop = float(payload["stop"])
    target = float(payload["target"])
    quantity = int(payload["quantity"])
    slip = float(payload["slippage"])
    fees = payload["fees"]

    if not 0 < stop < payload["reference_price"] < target or quantity <= 0:
        return {
            "status": "REVIEW",
            "note": "ระดับราคาหรือจำนวนหุ้นในสัญญาณไม่ถูกต้อง",
        }, None

    def finish(status, day, note, exit_price=None):
        outcome = {
            "status": status,
            "event_date": day.strftime("%Y-%m-%d"),
            "note": note,
            "evaluated_at": utc_text(),
        }
        if entry_fill is not None:
            outcome["entry_fill"] = entry_fill
            outcome["entry_date"] = entry_day.strftime("%Y-%m-%d")
        if exit_price is not None and entry_cost is not None:
            actual_exit = exit_price * (1 - slip)
            proceeds = actual_exit * quantity - commission(
                actual_exit * quantity, fees
            )
            pnl = proceeds - entry_cost
            outcome.update({
                "exit_fill": actual_exit,
                "net_pnl": pnl,
                "net_return_pct": pnl / entry_cost * 100,
                "net_r": pnl / risk_amount if risk_amount and risk_amount > 0 else None,
            })
        outcome["observations"] = event_observations(
            payload, frame, index_frame, day
        )
        return outcome, None

    for number, day in enumerate(needed, 1):
        # ห้ามข้ามวันที่ข้อมูลหุ้นหายแล้วตัดสินว่าชนะ
        if day not in frame.index:
            return None, f"ข้อมูลหุ้นขาดวันที่ {day.date()}"

        row = frame.loc[day]
        if float(row["Dividends"]) != 0 or float(row["Stock Splits"]) != 0:
            return finish(
                "REVIEW", day,
                "พบปันผลหรือแตกหุ้น ต้องตรวจการปรับราคา/จำนวนก่อนสรุป",
            )

        open_price = float(row["Open"])
        high = float(row["High"])
        low = float(row["Low"])
        close = float(row["Close"])

        if entry_fill is None:
            candidate_fill = open_price * (1 + slip)
            maximum_entry = (
                payload["reference_price"] * (1 + payload["max_entry_gap"])
            )

            if (
                candidate_fill <= stop
                or candidate_fill >= target
                or candidate_fill > maximum_entry
            ):
                return finish(
                    "CANCELLED", day,
                    "ราคาเปิดพร้อมสมมติ Slippage ไม่เข้าเงื่อนไขรับสถานะ",
                )

            entry_fill = candidate_fill
            entry_day = day
            entry_cost = entry_fill * quantity + commission(
                entry_fill * quantity, fees
            )
            stop_exit = stop * (1 - slip)
            risk_amount = entry_cost - (
                stop_exit * quantity - commission(stop_exit * quantity, fees)
            )

        else:
            # ราคาเปิดเกิดก่อน High/Low ของวัน
            if open_price <= stop:
                return finish(
                    "STOP", day,
                    "ราคาเปิดอยู่ที่หรือต่ำกว่า Stop ใช้ราคาเปิดจำลองการออก",
                    open_price,
                )
            if open_price >= target:
                return finish(
                    "TARGET", day,
                    "ราคาเปิดถึงเป้า ใช้ราคาเป้าจำลองอย่างระมัดระวัง",
                    target,
                )

        hit_stop = low <= stop
        hit_target = high >= target

        if hit_stop and hit_target:
            return finish(
                "AMBIGUOUS", day,
                "แท่งเดียวแตะทั้ง Stop และเป้า ข้อมูลรายวันแยกลำดับไม่ได้",
            )
        if hit_stop:
            return finish(
                "STOP", day, "แตะ Stop ก่อนพบเป้าหมายในลำดับวันที่", stop
            )
        if hit_target:
            return finish(
                "TARGET", day, "แตะเป้าหมายก่อนพบ Stop ในลำดับวันที่", target
            )
        if number == horizon:
            return finish(
                "TIMEOUT", day,
                f"ครบ {horizon} วันซื้อขายอ้างอิง ใช้ราคาปิดจำลองการออก",
                close,
            )

    return None, f"ติดตามแล้ว {len(needed)}/{horizon} วันซื้อขายอ้างอิง"


def update_signal_outcomes(user_id, frames):
    notes = {}
    for item in load_signals(user_id, "PENDING"):
        outcome, note = evaluate_signal(
            item["payload"],
            frames.get(item["symbol"]),
            frames.get(INDEX_SYMBOL),
        )
        if outcome is None:
            notes[item["id"]] = note
            continue

        payload = item["payload"]
        payload["outcome"] = outcome

        with transaction() as connection:
            connection.execute(
                """
                UPDATE signals SET status=?, data=?
                WHERE id=? AND user_id=? AND status='PENDING'
                """,
                (
                    outcome["status"], json_text(payload),
                    item["id"], user_id,
                ),
            )

    return notes


# =========================================================
# REPORTS
# =========================================================

SETUP_LABELS = {
    "BREAKOUT": "เบรกแนวต้าน",
    "PULLBACK": "ย่อตัวแล้วฟื้น",
    "MOMENTUM": "โมเมนตัมต่อเนื่อง",
}


def market_header(context):
    breadth = (
        f"{context['breadth']:.1f}%"
        if context["breadth"] is not None else "ไม่ทราบ"
    )
    lines = [
        f"เวลารายงาน: {now_bkk().strftime('%d/%m/%Y %H:%M')}",
        f"ข้อมูลราคา: {context['asof'] or 'ไม่พร้อม'}",
        "ใช้แท่งรายวันที่จบแล้ว ไม่ใช่ราคาสด",
        "",
        f"Breadth: {breadth} "
        f"จาก {context['sample_count']}/{len(UNIVERSE)} หุ้นอ้างอิง",
        "ตลาด: " + (
            "ผ่านเกณฑ์เปิดสถานะใหม่"
            if context["positive"] else "ยังไม่ผ่านเกณฑ์เปิดสถานะใหม่"
        ),
        "กลุ่มเด่นในชุดตัวอย่าง: "
        + (", ".join(context["leaders"]) or "ยังยืนยันไม่ได้"),
    ]
    lines.extend(f"• {issue}" for issue in context["issues"])
    return lines


def load_analysis_data(user_id, requested):
    portfolio = get_portfolio(user_id)
    pending = load_signals(user_id, "PENDING")
    symbols = (
        UNIVERSE
        + list(requested)
        + list(portfolio["stocks"])
        + [item["symbol"] for item in pending]
        + [INDEX_SYMBOL]
    )
    frames = get_market_frames(symbols)
    context = market_context(frames)
    update_signal_outcomes(user_id, frames)
    portfolio, state = refresh_peak(user_id, frames)
    return portfolio, state, frames, context


def holding_message(symbol, info, frames):
    frame = frames.get(symbol)
    price = latest_price(frame)
    quantity = info["shares"]
    average = info["average_cost"]

    lines = [
        f"{symbol_name(symbol)} — {quantity} หุ้น",
        f"ต้นทุนเฉลี่ยรวมค่าซื้อ: {average:.2f} บาท",
    ]

    if price is None:
        return "\n".join(lines + [
            "ข้อมูลราคาไม่พร้อม ยังสรุปว่าควรถือหรือขายไม่ได้"
        ])

    lines.append(
        f"ราคาอ้างอิง: {price:.2f} บาท "
        f"({frame.index[-1].strftime('%Y-%m-%d')})"
    )
    if average > 0:
        lines.append(
            f"P/L ตามราคา: {(price / average - 1) * 100:+.2f}% "
            "(ยังไม่หักค่าขาย)"
        )

    warning = holding_corporate_warning(info, frame)
    if warning:
        return "\n".join(lines + [
            f"ต้องตรวจข้อมูล: {warning}",
            "งดสรุปสัญญาณขายอัตโนมัติจากแผนเดิม",
        ])

    stop = info.get("stop_loss", 0)
    target = info.get("target_price", 0)
    lines.append(f"Stop เดิม: {stop:.2f} | เป้าเดิม: {target:.2f}")

    if stop <= 0:
        lines.append("ยังไม่มี Stop ที่ยืนยัน")
    elif price <= stop:
        lines.append("เข้าเงื่อนไขตัดขาดทุนตามแผน ตรวจราคาจริงก่อนดำเนินการ")
    elif target > 0 and price >= target:
        lines.append("ถึงเป้าตามแผน ตรวจราคาจริงก่อนดำเนินการ")
    else:
        r_unit = info.get("r_unit", 0)
        if r_unit > 0 and price >= average + r_unit:
            lines.append("ราคาอยู่เหนือระดับ 1R พิจารณาตามแผนขายบางส่วน")
        else:
            lines.append("ยังไม่ถึง Stop หรือเป้าที่บันทึกไว้")

        if info.get("opened_at"):
            opened = parse_time(info["opened_at"]).astimezone(BKK).date()
            sessions = int(sum(frame.index.date > opened))
            if sessions >= TIME_STOP_DAYS and (
                r_unit <= 0 or price < average + r_unit
            ):
                lines.append(
                    f"ถึงเกณฑ์ทบทวน Time Stop: {sessions} แท่งหลังเปิดสถานะ"
                )

    return "\n".join(lines)


def build_scan_report(user_id, requested, title, limit=5):
    portfolio, state, frames, context = load_analysis_data(user_id, requested)
    lines = [title] + market_header(context)

    if state["equity"] is None:
        return "\n".join(lines + [
            "", "งดจัดแผนซื้อ เพราะประเมินพอร์ตไม่ครบ",
            *[f"• {issue}" for issue in state["issues"]],
        ]), []

    profile = risk_profile(portfolio, state)
    lines.extend([
        f"มูลค่าพอร์ตประมาณ: {state['equity']:,.2f} บาท",
        f"เงินสด: {portfolio['cash']:,.2f} บาท | โหมด: {profile['label']}",
    ])

    results = []
    missing = []
    for symbol in unique_symbols(requested):
        result = analyse_symbol(
            symbol, frames, context, profile["min_score"]
        )
        if result is None:
            missing.append(symbol)
        else:
            results.append(result)

    selected = allocate_candidates(
        results, portfolio, state, profile, limit
    )
    new_ids = record_signals(user_id, selected, context)

    if not selected:
        lines.extend(["", "ข้อสรุป: ยังไม่มีแผนซื้อใหม่ที่ผ่านครบ"])
        if state["paused"]:
            lines.append("• พอร์ตเข้าเงื่อนไขหยุดเพิ่มความเสี่ยง")
        lines.extend(f"• {issue}" for issue in state["risk_issues"])

        counts = Counter(
            reason for result in results for reason in result["blockers"]
        )
        for reason, count in counts.most_common(3):
            lines.append(f"• {reason}: {count} หุ้น")

        if any(not result["blockers"] for result in results):
            lines.append(
                "• บางหุ้นผ่านสัญญาณ แต่ติดจำนวนสถานะ เงินสด "
                "เพดานกลุ่ม หรืออัตรากำไรต่อขาดทุนหลังต้นทุน"
            )
        if missing:
            lines.append(f"• ข้อมูลไม่พอวิเคราะห์: {len(missing)} หุ้น")

    for number, result in enumerate(selected, 1):
        lines.extend([
            "",
            f"{number}) {symbol_name(result['symbol'])} — แผนซื้อเพื่อพิจารณา",
            f"{result['sector']} | คะแนนตามกฎ {result['score']}/100",
            f"รูปแบบ: {SETUP_LABELS.get(result['setup'], 'ไม่ระบุ')}",
            f"ราคาอ้างอิง: {result['price']:.2f}",
            f"Stop: {result['stop']:.2f} | เป้า: {result['target']:.2f}",
            f"จำนวนตามงบ: {result['quantity']} หุ้น",
            f"เงินที่กันไว้ประมาณ: {result['estimated_cost']:,.2f} บาท",
            f"ขาดทุนกรณีออกที่ Stop: {result['estimated_loss']:,.2f} บาท",
            f"กำไรกรณีถึงเป้า: {result['estimated_profit']:,.2f} บาท",
            "เหตุผล:",
            *[f"• {reason}" for reason in result["reasons"][:3]],
            f"รหัสติดตาม: {result['signal_id'][:8]}",
        ])

        if result["quantity"] % BOARD_LOT:
            lines.append("• เป็น Odd Lot ต้องตรวจราคากระดานหน่วยย่อย")

    # รายตัวที่ถืออยู่ ใช้แผนเดิมในพอร์ต ไม่เอาแผนซื้อใหม่มาทับ
    if len(requested) == 1:
        symbol = requested[0]
        if symbol in portfolio["stocks"]:
            lines.extend([
                "", "หุ้นที่ถืออยู่",
                holding_message(symbol, portfolio["stocks"][symbol], frames),
            ])
        elif results and not selected:
            result = results[0]
            lines.extend([
                "",
                f"{symbol_name(symbol)}: คะแนนตามกฎ {result['score']}/100",
                f"ราคาอ้างอิง {result['price']:.2f} | RSI {result['rsi']:.1f}",
                *[f"• {reason}" for reason in result["reasons"][:3]],
            ])

    lines.extend([
        "",
        "คะแนนไม่ใช่เปอร์เซ็นต์โอกาสชนะ",
        "จำนวนและผลตอบแทนเป็นแผน ไม่ใช่คำสั่งซื้อขายที่ดำเนินการแล้ว",
        "Stop ไม่รับประกันราคาขายหรือขาดทุนสูงสุด",
        "สถิติติดตามจำลองเริ่มจากวันซื้อขายถัดจากวันที่ส่งคำแนะนำ",
    ])
    return "\n".join(lines), new_ids


def apply_trailing(user_id, frames):
    with transaction() as connection:
        portfolio = read_portfolio_in_transaction(connection, user_id)
        changes = []

        for symbol, info in portfolio["stocks"].items():
            frame = frames.get(symbol)
            price = latest_price(frame)
            snap = snapshot(frame)

            if (
                price is None or snap is None
                or holding_corporate_warning(info, frame)
            ):
                continue

            old_stop = info.get("stop_loss", 0)
            average = info["average_cost"]
            r_unit = info.get("r_unit", 0)

            if old_stop <= 0 or price <= old_stop or r_unit <= 0:
                continue

            info["highest_price"] = max(
                info.get("highest_price", price), price
            )

            if price >= average + r_unit:
                candidate = max(
                    old_stop,
                    average,
                    round(info["highest_price"] - 2 * snap["atr"], 2),
                )
                if old_stop < candidate < price:
                    info["stop_loss"] = candidate
                    changes.append(
                        f"{symbol_name(symbol)}: Stop "
                        f"{old_stop:.2f} → {candidate:.2f}"
                    )

        save_portfolio(connection, user_id, portfolio)
    return changes


def build_portfolio_report(user_id, trailing=False):
    portfolio = get_portfolio(user_id)
    frames = get_market_frames(list(portfolio["stocks"]))

    changes = apply_trailing(user_id, frames) if trailing else []
    portfolio, state = refresh_peak(user_id, frames)

    lines = [
        "สถานะพอร์ตที่บันทึกในบอต",
        f"เงินสด: {portfolio['cash']:,.2f} บาท",
    ]

    if state["equity"] is None:
        lines.append("ยังประเมินมูลค่ารวมไม่ได้ ไม่ใช้ต้นทุนแทนราคาล่าสุด")
        lines.extend(f"• {issue}" for issue in state["issues"])
    else:
        lines.extend([
            f"หุ้นตามราคาอ้างอิง: {state['stock_value']:,.2f} บาท",
            f"มูลค่ารวมประมาณ: {state['equity']:,.2f} บาท",
            f"กำไร/ขาดทุนเทียบเงินทุนสุทธิ: {state['net_pnl']:+,.2f} บาท",
            f"Drawdown ปรับฐานเงินทุนแบบประมาณ: {state['drawdown'] * 100:.2f}%",
        ])

    if not portfolio["stocks"]:
        lines.append("ยังไม่มีหุ้นในพอร์ต")

    for symbol, info in portfolio["stocks"].items():
        lines.extend(["", holding_message(symbol, info, frames)])

    if trailing:
        lines.extend(["", "ผลปรับ Trailing Stop"])
        lines.extend(changes or ["ไม่มีจุดที่เข้าเงื่อนไขปรับ"])
        lines.append("ปรับเฉพาะแผนในบอต ไม่ได้ส่งคำสั่งให้โบรกเกอร์")

    return "\n".join(lines)


def build_performance_report(user_id):
    history = load_signals(user_id)
    if not history:
        return (
            "ยังไม่มีประวัติคำแนะนำที่บันทึก\n"
            "เมื่อมีแผนซื้อผ่านเกณฑ์จากการสแกน "
            "ระบบจะเริ่มเก็บประวัติโดยไม่ต้องบันทึกซื้อจริง"
        )

    pending = [item for item in history if item["status"] == "PENDING"]
    frames = get_market_frames(
        [item["symbol"] for item in pending] + [INDEX_SYMBOL]
    ) if pending else {}

    notes = update_signal_outcomes(user_id, frames)
    history = load_signals(user_id)
    counts = Counter(item["status"] for item in history)

    outcomes = [
        item["payload"].get("outcome", {})
        for item in history
        if item["status"] in {"TARGET", "STOP", "TIMEOUT"}
    ]
    evaluated = [
        outcome for outcome in outcomes if "net_pnl" in outcome
    ]

    lines = [
        "ผลติดตามคำแนะนำ — พอร์ตจำลองแยกรายสัญญาณ",
        f"ประวัติทั้งหมด: {len(history)} รายการ",
        f"ถึงเป้าก่อน Stop: {counts['TARGET']} | Stop ก่อนเป้า: {counts['STOP']}",
        f"ครบเวลา: {counts['TIMEOUT']} | กำลังติดตาม: {counts['PENDING']}",
        f"ลำดับไม่ชัดเจน: {counts['AMBIGUOUS']} | ไม่ได้เข้า: {counts['CANCELLED']}",
        f"ต้องตรวจข้อมูล: {counts['REVIEW']} | รุ่นเก่า: {counts['LEGACY']}",
        f"ยังไม่ยืนยันการส่ง: {counts['GENERATED'] + counts['DELIVERY_UNKNOWN']}",
    ]

    if evaluated:
        wins = sum(outcome["net_pnl"] > 0 for outcome in evaluated)
        lines.append(
            f"สัดส่วนรายการจำลองกำไรหลังต้นทุน: "
            f"{wins / len(evaluated) * 100:.1f}% จาก {len(evaluated)} รายการ"
        )
        r_values = [
            outcome["net_r"] for outcome in evaluated
            if outcome.get("net_r") is not None
        ]
        if r_values:
            lines.append(
                f"R สุทธิเฉลี่ยของรายการที่คำนวณได้: "
                f"{sum(r_values) / len(r_values):+.2f}R"
            )

    grouped = defaultdict(list)
    for item in history:
        if item["status"] in {"TARGET", "STOP", "TIMEOUT"}:
            grouped[
                (
                    item["payload"].get("strategy_id", "ไม่ระบุรุ่น"),
                    item["payload"].get("setup", "ไม่ระบุ"),
                )
            ].append(item)

    if grouped:
        lines.extend(["", "สถิติแยกรุ่นและรูปแบบ"])
        for (version, setup), items in sorted(grouped.items()):
            target_count = sum(item["status"] == "TARGET" for item in items)
            lines.append(
                f"• {version} / {SETUP_LABELS.get(setup, setup)}: "
                f"ถึงเป้า {target_count}/{len(items)} รายการ "
                "(รวมรายการครบเวลาในตัวหาร)"
            )

    lines.extend(["", "รายละเอียด 10 รายการล่าสุด"])
    for item in history[-10:]:
        payload = item["payload"]
        outcome = payload.get("outcome", {})
        lines.extend([
            "",
            f"{symbol_name(item['symbol'])} | {item['id'][:8]}",
            f"สถานะ: {STATUS_LABELS.get(item['status'], item['status'])}",
            f"รูปแบบ: {SETUP_LABELS.get(payload.get('setup'), payload.get('setup', '-'))}",
        ])

        if payload.get("published_at"):
            lines.append(
                "ส่งคำแนะนำ: "
                + parse_time(payload["published_at"]).astimezone(BKK).strftime(
                    "%d/%m/%Y %H:%M"
                )
            )
        if outcome:
            lines.append(
                f"เหตุการณ์: {outcome.get('event_date', '-')} — "
                f"{outcome.get('note', '')}"
            )
            if "net_pnl" in outcome:
                lines.append(
                    f"P/L จำลองสุทธิ: {outcome['net_pnl']:+,.2f} บาท "
                    f"({outcome['net_return_pct']:+.2f}%)"
                )
            for observation in outcome.get("observations", []):
                lines.append(f"• {observation}")
        elif item["status"] == "PENDING":
            lines.append(notes.get(item["id"], "รอข้อมูลเพื่อประเมิน"))
        elif payload.get("note"):
            lines.append(payload["note"])

    lines.extend([
        "",
        "วิธีอ่านผล",
        "• ใช้ราคาเปิดวันซื้อขายถัดจากวันส่ง ไม่สมมติว่าซื้อได้ที่ราคาปิดย้อนหลัง",
        "• ใช้ค่าธรรมเนียมและ Slippage ที่บันทึกตอนสร้างสัญญาณ",
        "• ไม่รวมรายการลำดับไม่ชัดเจน/ข้อมูลไม่ครบเป็นชนะหรือแพ้",
        "• ข้อสังเกตทางเทคนิคไม่ใช่การพิสูจน์สาเหตุของราคา",
        "• สัญญาณอาจทับซ้อนกัน จึงไม่ใช่ผลตอบแทน Backtest ของพอร์ตจริง",
        "• ยังไม่ปรับน้ำหนักคะแนนอัตโนมัติจากสถิติเหล่านี้",
    ])
    return "\n".join(lines)


def build_plan_report(user_id):
    portfolio = get_portfolio(user_id)
    frames = get_market_frames(list(portfolio["stocks"]))
    portfolio, state = refresh_peak(user_id, frames)
    plan = portfolio["financial_plan"]

    lines = [
        "ติดตามเป้าหมายการเงิน",
        f"เป้าหมายที่ตั้งไว้: {plan['target_value']:,.2f} บาท",
        f"ระยะเวลาแผน: {plan['target_months']} เดือน",
    ]
    if state["equity"] is None:
        lines.append("ข้อมูลราคาไม่ครบ จึงยังประเมินความคืบหน้าไม่ได้")
    else:
        lines.extend([
            f"มูลค่าพอร์ตประมาณ: {state['equity']:,.2f} บาท",
            f"ส่วนต่างถึงเป้า: {max(0, plan['target_value'] - state['equity']):,.2f} บาท",
            f"เงินฝากสะสม: {portfolio['contributions_total']:,.2f} บาท",
            f"กำไร/ขาดทุนเทียบเงินทุนสุทธิ: {state['net_pnl']:+,.2f} บาท",
        ])
    lines.extend([
        "",
        "เงินฝากไม่ใช่กำไรลงทุน",
        "เป้าหมายเป็นค่าที่ผู้ใช้ตั้ง ไม่ใช่ผลตอบแทนที่บอตคาดว่าจะทำได้",
        "ไม่ประมาณจำนวนครั้งที่ต้องเทรดจาก Win Rate สมมติ",
    ])
    return "\n".join(lines)


def build_trade_statistics(portfolio):
    sells = [
        event for event in portfolio["transactions"]
        if event.get("type") == "SELL"
        and event.get("realized_pnl") is not None
    ]
    if not sells:
        return "ยังไม่มีรายการขายที่บันทึก"

    profits = [float(event["realized_pnl"]) for event in sells]
    wins = sum(value > 0 for value in profits)
    losses = sum(value < 0 for value in profits)

    return "\n".join([
        "สถิติธุรกรรมที่ผู้ใช้บันทึก",
        f"รายการขาย: {len(profits)}",
        f"กำไร: {wins} | ขาดทุน: {losses} | เท่าทุน: {len(profits) - wins - losses}",
        f"สัดส่วนรายการกำไร: {wins / len(profits) * 100:.1f}%",
        f"กำไร/ขาดทุนสุทธิที่บันทึก: {sum(profits):+,.2f} บาท",
        f"ค่าเฉลี่ยต่อรายการขาย: {sum(profits) / len(profits):+,.2f} บาท",
        f"ค่าธรรมเนียมซื้อขายสะสม: {portfolio['fees_total']:,.2f} บาท",
        "",
        "รายการขายบางส่วนถูกนับเป็นคนละรายการ ไม่ใช่รอบเทรดเต็ม",
        "สถิตินี้แยกจากคำสั่ง ผลการแนะนำ และไม่ยืนยันธุรกรรมกับโบรกเกอร์",
    ])


# =========================================================
# MANUAL TRANSACTIONS
# =========================================================

def record_buy(portfolio, symbol, quantity, price, fee):
    gross = round(quantity * price, 2)
    if gross > 1e12:
        raise ValueError("มูลค่ารายการสูงเกินขอบเขต")

    fee = commission(gross) if fee is None else fee
    cost = round(gross + fee, 2)

    if cost > portfolio["cash"]:
        raise ValueError(
            f"เงินสดไม่พอ ต้องใช้ {cost:,.2f} บาท "
            f"แต่มี {portfolio['cash']:,.2f} บาท"
        )

    old = portfolio["stocks"].get(symbol)
    if old:
        info = copy.deepcopy(old)
        info["shares"] += quantity
        info["total_cost"] = round(info["total_cost"] + cost, 2)
        info["average_cost"] = info["total_cost"] / info["shares"]
        info["highest_price"] = max(info.get("highest_price", price), price)
        plan_note = "คง Stop และเป้าเดิมไว้ กรุณาทบทวนแผนหลังซื้อเพิ่ม"
    else:
        average = cost / quantity
        initial_stop = round(price * 0.95, 2)
        r_unit = max(average - initial_stop, 0.01)
        info = {
            "shares": quantity,
            "total_cost": cost,
            "average_cost": average,
            "initial_stop": initial_stop,
            "stop_loss": initial_stop,
            "target_price": round(average + 2 * r_unit, 2),
            "r_unit": r_unit,
            "highest_price": max(price, average),
            "opened_at": utc_text(),
        }
        plan_note = (
            "แผนเริ่มต้นใช้ Stop ต่ำกว่าราคาที่กรอก 5% "
            "ไม่ใช่แนวรับที่วิเคราะห์จากตลาด"
        )

    portfolio["stocks"][symbol] = info
    portfolio["cash"] = round(portfolio["cash"] - cost, 2)
    portfolio["fees_total"] = round(portfolio["fees_total"] + fee, 2)

    transaction_id = uuid.uuid4().hex[:12]
    portfolio["transactions"].append({
        "id": transaction_id,
        "type": "BUY",
        "symbol": symbol,
        "shares": quantity,
        "price": price,
        "fee": fee,
        "net_amount": cost,
        "date": utc_text(),
    })

    return "\n".join([
        f"บันทึกซื้อ {symbol_name(symbol)} {quantity} หุ้นแล้ว",
        f"รวมค่าซื้อ: {cost:,.2f} บาท",
        f"ต้นทุนเฉลี่ย: {info['average_cost']:.4f} บาท",
        f"รหัสรายการ: {transaction_id}",
        plan_note,
        "แก้แผนได้ด้วย: ตั้งจุดขาย ชื่อหุ้น Stop เป้าหมาย",
        "เป็นการบันทึกในบอต ไม่ได้ส่งคำสั่งซื้อจริง",
    ])


def record_sell(portfolio, symbol, quantity, price, fee):
    if symbol not in portfolio["stocks"]:
        raise ValueError("ไม่มีหุ้นนี้ในพอร์ต")

    info = portfolio["stocks"][symbol]
    if quantity > info["shares"]:
        raise ValueError(f"มีหุ้นเพียง {info['shares']} หุ้น")

    gross = round(quantity * price, 2)
    fee = commission(gross) if fee is None else fee
    if fee > gross:
        raise ValueError("ค่าธรรมเนียมสูงกว่ามูลค่าขาย")

    old_quantity = info["shares"]
    allocated_cost = (
        info["total_cost"]
        if quantity == old_quantity
        else round(info["total_cost"] * quantity / old_quantity, 2)
    )
    proceeds = round(gross - fee, 2)
    pnl = round(proceeds - allocated_cost, 2)

    portfolio["cash"] = round(portfolio["cash"] + proceeds, 2)
    portfolio["fees_total"] = round(portfolio["fees_total"] + fee, 2)
    portfolio["realized_pnl_total"] = round(
        portfolio["realized_pnl_total"] + pnl, 2
    )

    remaining = old_quantity - quantity
    if remaining == 0:
        del portfolio["stocks"][symbol]
    else:
        info["shares"] = remaining
        info["total_cost"] = round(info["total_cost"] - allocated_cost, 2)
        info["average_cost"] = info["total_cost"] / remaining

    transaction_id = uuid.uuid4().hex[:12]
    portfolio["transactions"].append({
        "id": transaction_id,
        "type": "SELL",
        "symbol": symbol,
        "shares": quantity,
        "price": price,
        "fee": fee,
        "realized_pnl": pnl,
        "date": utc_text(),
    })

    return "\n".join([
        f"บันทึกขาย {symbol_name(symbol)} {quantity} หุ้นแล้ว",
        f"เงินรับสุทธิ: {proceeds:,.2f} บาท",
        f"กำไร/ขาดทุนสุทธิ: {pnl:+,.2f} บาท",
        f"รหัสรายการ: {transaction_id}",
        "เป็นการบันทึกในบอต ไม่ได้ส่งคำสั่งขายจริง",
    ])


HELP_TEXT = """คำสั่ง Portfolio Bot

ดูข้อมูล
• ดูพอร์ต
• วิเคราะห์พอร์ต — ตรวจและปรับ Trailing Stop ในบอต
• เช็กจุดขาย
• แผนการเงิน
• สถิติการเทรด
• ผลการแนะนำ / เช็กผลย้อนหลัง

วิเคราะห์หุ้น
• ควรซื้อวันนี้ / ซื้อวันนี้
• แผนเทรดวันนี้ / แผนวันนี้
• สรุปหุ้นวันนี้
• สแกนหุ้น
• สแกน SET50 — ใช้กลุ่มอ้างอิงที่ตั้งไว้
• วิเคราะห์ PTT / สแกน PTT

บันทึกรายการ
• ซื้อ PTT 100 35.50
• ขาย PTT 100 38.00
• เพิ่มค่าธรรมเนียมท้ายคำสั่งได้ เช่น ซื้อ PTT 100 35.50 10
• ฝากเงิน 1000
• ตั้งจุดขาย PTT 33 40

การตั้งค่า
• โหมดเสี่ยง conservative
• โหมดเสี่ยง balanced
• โหมดเสี่ยง aggressive
• ตั้งเป้าหมาย 20000 3
• รีเซ็ตแผน ยืนยัน — ไม่ลบหุ้นหรือธุรกรรม
• ติดตาม PTT
• เลิกติดตาม PTT
• รายการติดตาม

บอตไม่เชื่อมส่งคำสั่งซื้อขายกับโบรกเกอร์
ราคาที่ใช้เป็นข้อมูลรายวัน ไม่ใช่ราคาสด"""


# =========================================================
# COMMAND ROUTING
# =========================================================

def dispatch_command(user_id, text, event_id):
    parts = text.split()
    command = parts[0] if parts else ""
    normalized_text = " ".join(parts)

    if normalized_text in {"ช่วยเหลือ", "help", "คำสั่ง"}:
        return HELP_TEXT, []

    if normalized_text == "ดูพอร์ต":
        return build_portfolio_report(user_id), []

    if normalized_text == "วิเคราะห์พอร์ต":
        return build_portfolio_report(user_id, trailing=True), []

    if normalized_text in {"เช็กจุดขาย", "เช็คจุดขาย"}:
        return build_portfolio_report(user_id), []

    if normalized_text in {"ผลการแนะนำ", "เช็กผลย้อนหลัง", "ตรวจสอบผล"}:
        return build_performance_report(user_id), []

    if normalized_text in {"แผนการเงิน", "ความคืบหน้า"}:
        return build_plan_report(user_id), []

    if normalized_text == "สถิติการเทรด":
        return build_trade_statistics(get_portfolio(user_id)), []

    scan_commands = {
        "ควรซื้อวันนี้", "ซื้อวันนี้", "แผนเทรดวันนี้", "แผนวันนี้",
        "สรุปหุ้นวันนี้", "สแกนหุ้น", "สแกน SET50",
    }
    if normalized_text in scan_commands:
        portfolio = get_portfolio(user_id)
        symbols = (
            UNIVERSE if normalized_text == "สแกน SET50"
            else portfolio["watchlist"]
        )
        if not symbols:
            return "Watchlist ว่าง เพิ่มด้วยคำสั่ง ติดตาม PTT", []

        limit = 10 if normalized_text in {"สแกนหุ้น", "สแกน SET50"} else 5
        report, ids = build_scan_report(
            user_id, symbols, "หุ้นน่าพิจารณา — แผนซื้อใหม่", limit
        )
        if normalized_text in {"แผนเทรดวันนี้", "แผนวันนี้"}:
            report += (
                "\n\nตรวจหุ้นที่ถืออยู่เพิ่มเติมด้วยคำสั่ง เช็กจุดขาย"
                "\nดูเป้าหมายด้วยคำสั่ง แผนการเงิน"
            )
        return report, ids

    if command in {"วิเคราะห์", "สแกน"}:
        if len(parts) != 2:
            raise ValueError("รูปแบบ: วิเคราะห์ PTT หรือ สแกน PTT")
        symbol = normalize_symbol(parts[1])
        return build_scan_report(
            user_id, [symbol], f"วิเคราะห์ {symbol_name(symbol)}", 1
        )

    if command in {"ซื้อ", "ขาย"}:
        if len(parts) not in {4, 5}:
            raise ValueError(
                f"รูปแบบ: {command} PTT 100 35.50 [ค่าธรรมเนียม]"
            )
        symbol = normalize_symbol(parts[1])
        if symbol.startswith("^"):
            raise ValueError("คำสั่งนี้ใช้บันทึกหุ้น ไม่ใช่ดัชนี")
        quantity = positive_integer(parts[2], "จำนวนหุ้น")
        price = positive_number(parts[3], "ราคา")
        fee = (
            finite_number(parts[4], "ค่าธรรมเนียม")
            if len(parts) == 5 else None
        )
        operation = record_buy if command == "ซื้อ" else record_sell
        return mutate_portfolio(
            user_id, event_id,
            lambda p: operation(p, symbol, quantity, price, fee),
        ), []

    if command == "ฝากเงิน":
        if len(parts) != 2:
            raise ValueError("รูปแบบ: ฝากเงิน 1000")
        amount = round(positive_number(parts[1], "จำนวนเงิน"), 2)
        if amount <= 0:
            raise ValueError("จำนวนเงินหลังปัดเศษต้องมากกว่า 0")

        def deposit(portfolio):
            portfolio["cash"] = round(portfolio["cash"] + amount, 2)
            portfolio["contributions_total"] = round(
                portfolio["contributions_total"] + amount, 2
            )
            portfolio["transactions"].append({
                "id": uuid.uuid4().hex[:12],
                "type": "DEPOSIT",
                "amount": amount,
                "date": utc_text(),
            })
            return (
                f"บันทึกเงินฝาก {amount:,.2f} บาทแล้ว\n"
                "เงินฝากเป็นเงินทุน ไม่ใช่กำไร"
            )

        return mutate_portfolio(user_id, event_id, deposit), []

    if command == "ตั้งจุดขาย":
        if len(parts) != 4:
            raise ValueError("รูปแบบ: ตั้งจุดขาย PTT 33 40")
        symbol = normalize_symbol(parts[1])
        stop = positive_number(parts[2], "Stop")
        target = positive_number(parts[3], "เป้าหมาย")
        if stop >= target:
            raise ValueError("Stop ต้องต่ำกว่าเป้าหมาย")

        def set_levels(portfolio):
            if symbol not in portfolio["stocks"]:
                raise ValueError("ไม่มีหุ้นนี้ในพอร์ต")
            info = portfolio["stocks"][symbol]
            info["stop_loss"] = stop
            info["target_price"] = target
            if info.get("r_unit", 0) <= 0 and stop < info["average_cost"]:
                info["r_unit"] = info["average_cost"] - stop
                info["initial_stop"] = stop
            return (
                f"บันทึกแผน {symbol_name(symbol)}: Stop {stop:.2f} "
                f"| เป้า {target:.2f}\nไม่ได้ส่งคำสั่งซื้อขายจริง"
            )

        return mutate_portfolio(user_id, event_id, set_levels), []

    if command == "โหมดเสี่ยง":
        if len(parts) != 2 or parts[1].lower() not in RISK_PROFILES:
            raise ValueError(
                "ใช้: โหมดเสี่ยง conservative / balanced / aggressive"
            )
        mode = parts[1].lower()

        def set_mode(portfolio):
            portfolio["risk_mode"] = mode
            return f"เปลี่ยนโหมดเป็น {RISK_PROFILES[mode]['label']} แล้ว"

        return mutate_portfolio(user_id, event_id, set_mode), []

    if command == "ตั้งเป้าหมาย":
        if len(parts) != 3:
            raise ValueError("รูปแบบ: ตั้งเป้าหมาย 20000 3")
        target = positive_number(parts[1], "เป้าหมาย")
        months = positive_integer(parts[2], "จำนวนเดือน", 1200)

        def set_target(portfolio):
            portfolio["financial_plan"]["target_value"] = target
            portfolio["financial_plan"]["target_months"] = months
            return (
                f"ตั้งเป้าหมาย {target:,.2f} บาท ระยะ {months} เดือนแล้ว\n"
                "เป็นเป้าหมายที่ตั้ง ไม่ใช่ผลตอบแทนรับประกัน"
            )

        return mutate_portfolio(user_id, event_id, set_target), []

    if normalized_text == "รีเซ็ตแผน ยืนยัน":
        portfolio = get_portfolio(user_id)
        frames = get_market_frames(list(portfolio["stocks"]))

        def reset_plan(current):
            state = portfolio_state(current, frames)
            if state["equity"] is None or state["risk_issues"]:
                raise ValueError("ข้อมูลพอร์ตไม่พร้อม ยังเริ่มแผนใหม่ไม่ได้")
            current["plan_start_date"] = utc_text()
            current["plan_start_equity"] = state["equity"]
            return "เริ่มนับแผนใหม่แล้ว หุ้น ธุรกรรม และประวัติคำแนะนำยังอยู่"

        return mutate_portfolio(user_id, event_id, reset_plan), []

    if command in {"ติดตาม", "เลิกติดตาม"}:
        if len(parts) != 2:
            raise ValueError(f"รูปแบบ: {command} PTT")
        symbol = normalize_symbol(parts[1])
        if symbol.startswith("^"):
            raise ValueError("Watchlist นี้ใช้สำหรับหุ้น")

        def change_watchlist(portfolio):
            values = portfolio["watchlist"]
            if command == "ติดตาม":
                if symbol not in values:
                    values.append(symbol)
            else:
                portfolio["watchlist"] = [s for s in values if s != symbol]
            return f"บันทึกคำสั่ง {command} {symbol_name(symbol)} แล้ว"

        return mutate_portfolio(user_id, event_id, change_watchlist), []

    if normalized_text == "รายการติดตาม":
        values = get_portfolio(user_id)["watchlist"]
        return (
            "รายการติดตาม\n"
            + (", ".join(symbol_name(s) for s in values) or "ยังไม่มี")
        ), []

    raise ValueError("ไม่รู้จักคำสั่ง พิมพ์ ช่วยเหลือ เพื่อดูรูปแบบ")


# =========================================================
# BACKGROUND JOBS AND WEBHOOK
# =========================================================

executor = ThreadPoolExecutor(max_workers=2)
pending_users = set()
pending_lock = threading.Lock()


def reserve_job(user_id):
    with pending_lock:
        if user_id in pending_users or len(pending_users) >= 32:
            return False
        pending_users.add(user_id)
        return True


def release_job(user_id):
    with pending_lock:
        pending_users.discard(user_id)


def run_job(user_id, text, event_id):
    new_signal_ids = []
    try:
        reply, new_signal_ids = dispatch_command(user_id, text, event_id)
        try:
            push_text(user_id, reply)
        except Exception:
            mark_signal_delivery(user_id, new_signal_ids, False)
            raise
        mark_signal_delivery(user_id, new_signal_ids, True)

    except ValueError as error:
        try:
            push_text(user_id, f"ข้อมูลไม่ถูกต้อง: {error}")
        except Exception:
            logger.exception("ส่งข้อความแจ้งรูปแบบคำสั่งไม่สำเร็จ")

    except StorageError:
        logger.exception("ข้อผิดพลาดฐานข้อมูล")
        try:
            push_text(
                user_id,
                "ข้อมูลพอร์ตหรือฐานข้อมูลมีปัญหา "
                "ระบบไม่ได้รีเซ็ตพอร์ต กรุณาตรวจ Log และไฟล์สำรอง",
            )
        except Exception:
            logger.exception("ส่งข้อความแจ้งฐานข้อมูลไม่สำเร็จ")

    except Exception:
        logger.exception("ประมวลผลคำสั่งไม่สำเร็จ")
        try:
            push_text(
                user_id,
                "ประมวลผลหรือส่งผลลัพธ์ไม่สำเร็จ "
                "หากเป็นคำสั่งซื้อ/ขาย/ฝากเงิน "
                "กรุณาตรวจพอร์ตก่อนส่งรายการเดิมใหม่",
            )
        except Exception:
            logger.exception("ส่งข้อความแจ้งข้อผิดพลาดไม่สำเร็จ")
    finally:
        release_job(user_id)


@app.route("/", methods=["GET", "HEAD"])
def health():
    return jsonify({
        "status": "ok",
        "service": "portfolio-bot",
        "version": VERSION,
    })


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        line_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@line_handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    # ป้องกันการนำพอร์ตส่วนตัวไปแสดงในกลุ่ม
    if getattr(event.source, "type", None) != "user":
        line_api.reply_message(
            event.reply_token,
            TextSendMessage(text="กรุณาใช้บอตในแชตส่วนตัวเพื่อรักษาข้อมูลพอร์ต"),
        )
        return

    user_id = getattr(event.source, "user_id", None)
    if not user_id:
        return

    text = " ".join(event.message.text.strip().split())
    if not text:
        return

    if text in {"ช่วยเหลือ", "help", "คำสั่ง"}:
        line_api.reply_message(
            event.reply_token, TextSendMessage(text=HELP_TEXT)
        )
        return

    event_id = getattr(event, "webhook_event_id", None)
    if not event_id:
        message_id = getattr(event.message, "id", None)
        if not message_id:
            line_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text="ไม่พบรหัสข้อความที่ใช้ป้องกันรายการซ้ำ กรุณาส่งใหม่"
                ),
            )
            return
        event_id = f"message:{user_id}:{message_id}"

    if not reserve_job(user_id):
        line_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="มีงานกำลังประมวลผลหรือคิวเต็ม กรุณารอผลก่อนส่งคำสั่งใหม่"
            ),
        )
        return

    try:
        # ตอบรับก่อน ไม่รอโหลดราคาใน webhook
        line_api.reply_message(
            event.reply_token,
            TextSendMessage(text="รับคำสั่งแล้ว กำลังประมวลผล"),
        )
    except Exception:
        logger.warning("ส่งข้อความตอบรับไม่สำเร็จ แต่ยังประมวลผลคำสั่ง")

    try:
        executor.submit(run_job, user_id, text, str(event_id))
    except Exception:
        release_job(user_id)
        logger.exception("เพิ่มงานเข้าคิวไม่สำเร็จ")


# =========================================================
# SCHEDULER
# =========================================================

def registered_users():
    with closing(connect_db()) as connection:
        return [
            row["user_id"]
            for row in connection.execute("SELECT user_id FROM portfolios")
        ]


def scheduled_command(text):
    for user_id in registered_users():
        if reserve_job(user_id):
            try:
                executor.submit(run_job, user_id, text, None)
            except Exception:
                release_job(user_id)
                logger.exception("เพิ่มงานตามเวลาไม่สำเร็จ")


scheduler = None
if ENABLE_SCHEDULER:
    scheduler = BackgroundScheduler(timezone="Asia/Bangkok")
    scheduler.add_job(
        scheduled_command,
        "cron",
        args=["แผนเทรดวันนี้"],
        day_of_week="mon-fri",
        hour=8,
        minute=30,
        id="morning-plan",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        scheduled_command,
        "cron",
        args=["ผลการแนะนำ"],
        day_of_week="mon-fri",
        hour=18,
        minute=30,
        id="evening-performance",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=900,
    )
    scheduler.start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=False,
    )
