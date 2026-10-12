import copy
import fcntl
import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# =========================================================
# CONFIG
# =========================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("portfolio-bot")

BKK = timezone(timedelta(hours=7))
VERSION = 17
TRACKING_VERSION = 2
MAX_LINE_TEXT = 4000

TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

if not TOKEN or not SECRET:
    raise RuntimeError(
        "ต้องตั้งค่า LINE_CHANNEL_ACCESS_TOKEN "
        "และ LINE_CHANNEL_SECRET"
    )

# ค่าเริ่มต้นยังเข้ากับการติดตั้งปัจจุบัน
# เมื่อติดตั้ง Persistent Disk แล้วจึงตั้ง PORTFOLIO_FILE
# ให้ชี้ไปยังไฟล์บนดิสก์นั้น
PORTFOLIO_FILE = os.path.abspath(
    os.getenv("PORTFOLIO_FILE", "portfolio.json")
)

INDEX_SYMBOL = os.getenv(
    "MARKET_INDEX_SYMBOL", "^SET.BK"
).strip().upper()

CACHE_SECONDS = max(
    60, int(os.getenv("CACHE_SECONDS", "900"))
)
MAX_DATA_AGE_DAYS = max(
    1, int(os.getenv("MAX_DATA_AGE_DAYS", "7"))
)
SIGNAL_HORIZON = max(
    1, int(os.getenv("SIGNAL_HORIZON", "15"))
)

BOARD_LOT = max(1, int(os.getenv("BOARD_LOT", "100")))
ALLOW_ODD_LOT = (
    os.getenv("ALLOW_ODD_LOT", "true").lower() == "true"
)

MIN_AVG_VALUE = float(
    os.getenv("MIN_AVG_VALUE", "10000000")
)
MIN_ATR_PCT = float(
    os.getenv("MIN_ATR_PCT", "1.2")
)
MAX_ATR_PCT = float(
    os.getenv("MAX_ATR_PCT", "12.0")
)

COMMISSION_RATE = float(
    os.getenv("COMMISSION_RATE", "0.00157")
)
VAT_RATE = float(
    os.getenv("VAT_RATE", "0.07")
)
MIN_COMMISSION = float(
    os.getenv("MIN_COMMISSION", "0")
)
SLIPPAGE = float(
    os.getenv("SLIPPAGE", "0.002")
)
MAX_ENTRY_GAP = float(
    os.getenv("MAX_ENTRY_GAP", "0.02")
)

ENABLE_SCHEDULER = (
    os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
)

RISK_PROFILES = {
    "conservative": {
        "label": "ระมัดระวัง",
        "risk_per_trade": 0.01,
        "max_positions": 3,
        "max_total_risk": 0.04,
        "max_weight": 0.25,
        "min_score": 70,
    },
    "balanced": {
        "label": "สมดุล",
        "risk_per_trade": 0.02,
        "max_positions": 4,
        "max_total_risk": 0.07,
        "max_weight": 0.35,
        "min_score": 60,
    },
    "aggressive": {
        "label": "เชิงรุก",
        "risk_per_trade": 0.03,
        "max_positions": 5,
        "max_total_risk": 0.12,
        "max_weight": 0.45,
        "min_score": 55,
    },
}

DEFAULT_RISK_MODE = os.getenv(
    "RISK_MODE", "balanced"
).lower()

if DEFAULT_RISK_MODE not in RISK_PROFILES:
    DEFAULT_RISK_MODE = "balanced"

FALLBACK_SYMBOLS = """
ADVANC AOT AWC BANPU BBL BDMS BH BJC BTS CBG
CENTEL COM7 CPALL CPAXT CPF CPN CRC DELTA EA EGCO
GLOBAL GPSC GULF HMPRO ITC IVL KBANK KKP KTB KTC
LH MINT MTC OR OSP PTT PTTEP PTTGC RATCH SAWAD
SCB SCC SCGP TCAP TIDLOR TISCO TOP TRUE TU WHA
""".split()

SECTOR_GROUPS = {
    "สื่อสาร": "ADVANC TRUE",
    "ธนาคาร": "BBL KBANK KKP KTB SCB TISCO",
    "พาณิชย์": "BJC COM7 CPALL CPAXT CRC GLOBAL HMPRO",
    "พลังงาน": "BANPU EA EGCO GPSC GULF OR PTT PTTEP RATCH TOP",
    "อาหาร": "CBG CPF ITC OSP TU",
    "อสังหาริมทรัพย์": "AWC CPN LH WHA",
    "การแพทย์": "BDMS BH",
    "เงินทุน": "KTC MTC SAWAD TCAP TIDLOR",
    "ขนส่ง": "AOT BTS",
    "ท่องเที่ยว": "CENTEL MINT",
    "ปิโตรเคมี": "IVL PTTGC",
    "วัสดุก่อสร้าง": "SCC",
    "บรรจุภัณฑ์": "SCGP",
    "อิเล็กทรอนิกส์": "DELTA",
}

app = Flask(__name__)
line_api = LineBotApi(TOKEN)
handler = WebhookHandler(SECRET)


# =========================================================
# UTILITIES
# =========================================================

def now_bkk():
    return datetime.now(BKK)


def utc_text():
    return datetime.now(timezone.utc).isoformat()


def format_symbol(value):
    symbol = str(value).strip().upper()

    allowed = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789^&._-"
    )

    if (
        not symbol
        or len(symbol) > 30
        or any(char not in allowed for char in symbol)
    ):
        raise ValueError("ชื่อหุ้นไม่ถูกต้อง")

    if not symbol.startswith("^") and not symbol.endswith(".BK"):
        symbol += ".BK"

    return symbol


def display_symbol(symbol):
    symbol = str(symbol)
    return symbol[:-3] if symbol.endswith(".BK") else symbol


def unique_symbols(values):
    return list(
        dict.fromkeys(format_symbol(value) for value in values)
    )


configured_symbols = os.getenv(
    "SET50_SYMBOLS", ""
).replace(",", " ").split()

UNIVERSE = unique_symbols(
    configured_symbols or FALLBACK_SYMBOLS
)

SECTOR_MAP = {
    format_symbol(symbol): sector
    for sector, symbols in SECTOR_GROUPS.items()
    for symbol in symbols.split()
}


def number(value, name, minimum=0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} ต้องเป็นตัวเลข")

    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} ไม่ถูกต้อง")

    return result


def positive_number(value, name):
    result = number(value, name)

    if result <= 0:
        raise ValueError(f"{name} ต้องมากกว่า 0")

    return result


def positive_int(value, name):
    text = str(value)

    if not text.isdecimal():
        raise ValueError(f"{name} ต้องเป็นจำนวนเต็มบวก")

    result = int(text)

    if result <= 0 or result > 100000000:
        raise ValueError(f"{name} ไม่ถูกต้อง")

    return result


def commission(value, settings=None):
    if value <= 0:
        return 0.0

    cfg = settings or {
        "rate": COMMISSION_RATE,
        "vat": VAT_RATE,
        "minimum": MIN_COMMISSION,
    }

    return round(
        max(
            value * cfg["rate"],
            cfg["minimum"],
        ) * (1 + cfg["vat"]),
        2,
    )


def split_message(text):
    chunks = []
    current = ""

    for line in str(text).splitlines():
        while len(line) > MAX_LINE_TEXT:
            if current:
                chunks.append(current)
                current = ""

            chunks.append(line[:MAX_LINE_TEXT])
            line = line[MAX_LINE_TEXT:]

        candidate = (
            current + ("\n" if current else "") + line
        )

        if len(candidate) > MAX_LINE_TEXT:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks or ["ไม่มีข้อมูล"]


def push_long_message(user_id, text):
    for chunk in split_message(text):
        line_api.push_message(
            user_id,
            TextSendMessage(text=chunk),
        )


# =========================================================
# JSON STORAGE
# =========================================================

class StorageError(RuntimeError):
    pass


storage_lock = threading.RLock()


def default_portfolio():
    return {
        "schema_version": VERSION,
        "cash": 10000.0,
        "initial_capital": 10000.0,
        "contributions_total": 0.0,
        "withdrawals_total": 0.0,
        "realized_pnl_total": 0.0,
        "fees_total": 0.0,
        "high_water_mark": 10000.0,
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
        "signal_history": [],
    }


def normalize_portfolio(raw):
    if not isinstance(raw, dict):
        raise StorageError("รูปแบบพอร์ตไม่ถูกต้อง")

    result = default_portfolio()
    result.update(copy.deepcopy(raw))

    if not isinstance(result["stocks"], dict):
        raise StorageError("ข้อมูลหุ้นในพอร์ตไม่ถูกต้อง")

    if not isinstance(result["watchlist"], list):
        raise StorageError("ข้อมูล Watchlist ไม่ถูกต้อง")

    if not isinstance(result["transactions"], list):
        raise StorageError("ข้อมูลธุรกรรมไม่ถูกต้อง")

    if not isinstance(result["signal_history"], list):
        raise StorageError("ประวัติสัญญาณไม่ถูกต้อง")

    if not isinstance(result["financial_plan"], dict):
        raise StorageError("แผนการเงินไม่ถูกต้อง")

    try:
        result["watchlist"] = unique_symbols(
            result["watchlist"]
        )
        result["stocks"] = {
            format_symbol(symbol): info
            for symbol, info in result["stocks"].items()
        }
    except ValueError as error:
        raise StorageError(
            "ชื่อหุ้นในข้อมูลพอร์ตไม่ถูกต้อง"
        ) from error

    if result["risk_mode"] not in RISK_PROFILES:
        result["risk_mode"] = DEFAULT_RISK_MODE

    plan = default_portfolio()["financial_plan"]
    plan.update(result["financial_plan"])

    result["financial_plan"] = plan
    result["schema_version"] = VERSION

    return result


def load_store_unlocked():
    if not os.path.exists(PORTFOLIO_FILE):
        return {}

    try:
        with open(
            PORTFOLIO_FILE,
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise StorageError(
            "อ่าน portfolio.json ไม่สำเร็จ "
            "ระบบไม่ได้รีเซ็ตหรือเขียนทับไฟล์เดิม"
        ) from error

    if not isinstance(data, dict):
        raise StorageError(
            "portfolio.json ต้องเป็น JSON object"
        )

    # ป้องกันการนำไฟล์ config หรือพอร์ตแบบไม่มีเจ้าของ
    # มาอ่านเป็นข้อมูลผู้ใช้โดยไม่ได้ตั้งใจ
    if any(
        key in data
        for key in (
            "cash",
            "stocks",
            "financial_plan",
            "signal_history",
        )
    ):
        raise StorageError(
            "พบข้อมูลพอร์ตแบบไม่มีเจ้าของ "
            "ต้องตรวจและย้ายข้อมูลก่อน"
        )

    return data


def save_store_unlocked(data):
    directory = os.path.dirname(PORTFOLIO_FILE)
    os.makedirs(directory, exist_ok=True)

    descriptor, temporary = tempfile.mkstemp(
        prefix=".portfolio_",
        suffix=".tmp",
        dir=directory,
    )

    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, PORTFOLIO_FILE)

    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


@contextmanager
def locked_store(write=False):
    """
    อ่านอย่างเดียวใช้ shared lock
    การแก้ไขใช้ exclusive lock และบันทึกแบบ atomic
    รองรับ Linux/Render
    """
    directory = os.path.dirname(PORTFOLIO_FILE)
    os.makedirs(directory, exist_ok=True)

    with storage_lock:
        with open(
            PORTFOLIO_FILE + ".lock",
            "a+",
            encoding="utf-8",
        ) as lock_file:
            mode = (
                fcntl.LOCK_EX
                if write
                else fcntl.LOCK_SH
            )
            fcntl.flock(lock_file.fileno(), mode)

            try:
                data = load_store_unlocked()
                yield data

                if write:
                    save_store_unlocked(data)
            finally:
                fcntl.flock(
                    lock_file.fileno(),
                    fcntl.LOCK_UN,
                )


def mutate_portfolio(user_id, callback, event_id=None):
    with locked_store(write=True) as data:
        processed = data.setdefault(
            "__processed_events__", {}
        )

        if not isinstance(processed, dict):
            raise StorageError(
                "ข้อมูลป้องกันรายการซ้ำไม่ถูกต้อง"
            )

        if event_id and event_id in processed:
            previous = processed[event_id]

            if previous.get("user_id") != user_id:
                raise StorageError(
                    "รหัสเหตุการณ์ไม่ตรงกับผู้ใช้"
                )

            # การส่งซ้ำไม่ทำธุรกรรมซ้ำ
            return (
                "รายการนี้บันทึกไว้แล้ว ไม่ได้ทำซ้ำ\n"
                + previous["reply"]
            )

        raw = data.get(user_id)

        portfolio = (
            default_portfolio()
            if raw is None
            else normalize_portfolio(raw)
        )

        reply = callback(portfolio)
        data[user_id] = normalize_portfolio(portfolio)

        if event_id:
            processed[event_id] = {
                "user_id": user_id,
                "reply": reply,
            }

            while len(processed) > 2000:
                processed.pop(next(iter(processed)))

        return reply


def get_portfolio(user_id):
    # หลังจากมีพอร์ตแล้ว คำสั่งอ่านจะไม่เขียน JSON อีก
    with locked_store(write=False) as data:
        raw = data.get(user_id)

        if raw is not None:
            return normalize_portfolio(raw)

    # เขียนเพียงครั้งแรกเพื่อสร้างพอร์ตของผู้ใช้
    mutate_portfolio(
        user_id,
        lambda portfolio: "สร้างพอร์ตแล้ว",
    )

    with locked_store(write=False) as data:
        raw = data.get(user_id)

        if raw is None:
            raise StorageError(
                "สร้างพอร์ตไม่สำเร็จ"
            )

        return normalize_portfolio(raw)


def registered_users():
    with locked_store(write=False) as data:
        return [
            key
            for key, value in data.items()
            if (
                not key.startswith("__")
                and isinstance(value, dict)
            )
        ]


# =========================================================
# MARKET DATA
# =========================================================

market_cache = {}
market_cache_lock = threading.RLock()


def extract_frame(downloaded, symbol, count):
    if downloaded is None or downloaded.empty:
        return None

    if isinstance(downloaded.columns, pd.MultiIndex):
        frame = None

        for level in range(
            downloaded.columns.nlevels
        ):
            if symbol in downloaded.columns.get_level_values(
                level
            ):
                frame = downloaded.xs(
                    symbol,
                    axis=1,
                    level=level,
                ).copy()
                break

        if frame is None:
            return None

    elif count == 1:
        frame = downloaded.copy()

    else:
        return None

    frame.columns = [
        str(column).strip().title()
        for column in frame.columns
    ]

    required = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]

    if not set(required).issubset(frame.columns):
        return None

    optional = [
        column
        for column in (
            "Dividends",
            "Stock Splits",
        )
        if column in frame.columns
    ]

    frame = frame[required + optional].copy()

    index = pd.DatetimeIndex(
        pd.to_datetime(frame.index)
    )

    if index.tz is not None:
        index = index.tz_convert(
            "Asia/Bangkok"
        ).tz_localize(None)

    frame.index = index.normalize()
    frame = frame.sort_index()

    frame = frame[
        ~frame.index.duplicated(
            keep="last"
        )
    ]

    for column in required + optional:
        frame[column] = pd.to_numeric(
            frame[column],
            errors="coerce",
        )

    frame = frame.dropna(
        subset=required
    )

    valid = (
        (frame["Open"] > 0)
        & (frame["High"] > 0)
        & (frame["Low"] > 0)
        & (frame["Close"] > 0)
        & (frame["Volume"] >= 0)
        & (frame["High"] >= frame["Low"])
        & (frame["High"] >= frame["Open"])
        & (frame["High"] >= frame["Close"])
        & (frame["Low"] <= frame["Open"])
        & (frame["Low"] <= frame["Close"])
    )

    frame = frame[valid]

    return (
        frame
        if not frame.empty
        else None
    )


def completed_frame(frame):
    if frame is None or frame.empty:
        return None

    current = now_bkk()
    today = pd.Timestamp(current.date())

    # ก่อน 18:00 น. ใช้เฉพาะแท่งก่อนวันนี้
    if (
        current.hour,
        current.minute,
    ) < (18, 0):
        result = frame[
            frame.index < today
        ]
    else:
        result = frame[
            frame.index <= today
        ]

    return (
        result.copy()
        if not result.empty
        else None
    )


def get_market_frames(symbols):
    requested = unique_symbols(symbols)
    now = time.time()

    results = {}
    missing = []

    with market_cache_lock:
        for symbol in requested:
            cached = market_cache.get(symbol)

            if (
                cached
                and cached["expires_at"] > now
            ):
                if cached["frame"] is not None:
                    results[symbol] = (
                        cached["frame"].copy()
                    )
            else:
                missing.append(symbol)

    for offset in range(
        0,
        len(missing),
        10,
    ):
        chunk = missing[offset:offset + 10]

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

        except Exception:
            logger.exception(
                "ดาวน์โหลดข้อมูลตลาดไม่สำเร็จ"
            )
            downloaded = None

        for symbol in chunk:
            try:
                frame = extract_frame(
                    downloaded,
                    symbol,
                    len(chunk),
                )
            except Exception:
                logger.exception(
                    "แยกข้อมูล %s ไม่สำเร็จ",
                    symbol,
                )
                frame = None

            with market_cache_lock:
                market_cache[symbol] = {
                    "expires_at": now + (
                        CACHE_SECONDS
                        if frame is not None
                        else 60
                    ),
                    "frame": frame,
                }

            if frame is not None:
                results[symbol] = (
                    frame.copy()
                )

        if offset + 10 < len(missing):
            time.sleep(0.5)

    output = {}

    for symbol, frame in results.items():
        complete = completed_frame(frame)

        if complete is not None:
            output[symbol] = complete

    return output


def frame_recent(frame):
    if frame is None or frame.empty:
        return False

    age = (
        now_bkk().date()
        - frame.index[-1].date()
    ).days

    return (
        0 <= age <= MAX_DATA_AGE_DAYS
    )


# =========================================================
# INDICATORS
# =========================================================

def rsi_value(close, period=14):
    changes = close.diff().dropna().tolist()

    if len(changes) < period:
        return None

    gain = sum(
        max(value, 0)
        for value in changes[:period]
    ) / period

    loss = sum(
        max(-value, 0)
        for value in changes[:period]
    ) / period

    for change in changes[period:]:
        gain = (
            gain * (period - 1)
            + max(change, 0)
        ) / period

        loss = (
            loss * (period - 1)
            + max(-change, 0)
        ) / period

    if loss == 0:
        return (
            100.0
            if gain > 0
            else 50.0
        )

    return 100 - 100 / (
        1 + gain / loss
    )


def snapshot(frame):
    if frame is None or len(frame) < 90:
        return None

    close = frame["Close"].astype(float)
    price = float(close.iloc[-1])

    ema20 = close.ewm(
        span=20,
        adjust=False,
    ).mean()

    ema50 = close.ewm(
        span=50,
        adjust=False,
    ).mean()

    macd = (
        close.ewm(
            span=12,
            adjust=False,
        ).mean()
        - close.ewm(
            span=26,
            adjust=False,
        ).mean()
    )

    histogram = (
        macd
        - macd.ewm(
            span=9,
            adjust=False,
        ).mean()
    )

    previous = close.shift(1)

    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (
                frame["High"]
                - previous
            ).abs(),
            (
                frame["Low"]
                - previous
            ).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = float(
        true_range.rolling(
            14
        ).mean().iloc[-1]
    )

    rsi = rsi_value(close)

    if (
        price <= 0
        or not math.isfinite(atr)
        or atr <= 0
        or rsi is None
    ):
        return None

    previous20 = frame.iloc[-21:-1]
    previous10 = frame.iloc[-11:-1]

    average_volume = float(
        previous20["Volume"].mean()
    )

    volume_ratio = (
        float(
            frame["Volume"].iloc[-1]
        ) / average_volume
        if average_volume > 0
        else 0
    )

    # ใช้เฉพาะสัปดาห์ที่จบแล้ว
    weekly = close.resample(
        "W-FRI"
    ).last().dropna()

    weekly = weekly[
        weekly.index
        <= frame.index[-1]
    ]

    weekly_up = False

    if len(weekly) >= 25:
        weekly_ema = weekly.ewm(
            span=20,
            adjust=False,
        ).mean()

        weekly_up = bool(
            weekly.iloc[-1]
            >= weekly_ema.iloc[-1]
        )

    last = frame.iloc[-1]

    candle_range = float(
        last["High"] - last["Low"]
    )

    strong_close = bool(
        candle_range > 0
        and (
            float(last["Close"])
            - float(last["Low"])
        ) / candle_range >= 0.65
    )

    return {
        "date": frame.index[-1].strftime(
            "%Y-%m-%d"
        ),
        "price": price,
        "ema20": float(
            ema20.iloc[-1]
        ),
        "ema50": float(
            ema50.iloc[-1]
        ),
        "rsi": rsi,
        "atr": atr,
        "atr_pct": (
            atr / price * 100
        ),
        "hist": float(
            histogram.iloc[-1]
        ),
        "hist_prev": float(
            histogram.iloc[-2]
        ),
        "weekly_up": weekly_up,
        "strong_close": strong_close,
        "previous_high20": float(
            previous20["High"].max()
        ),
        "previous_low10": float(
            previous10["Low"].min()
        ),
        "volume_ratio": volume_ratio,
        "average_value": float(
            (
                previous20["Close"]
                * previous20["Volume"]
            ).mean()
        ),
        "ret20": (
            price
            / float(close.iloc[-21])
            - 1
        ) * 100,
        "extension_pct": (
            price
            / float(ema20.iloc[-1])
            - 1
        ) * 100,
    }


def market_context(frames):
    index_frame = frames.get(
        INDEX_SYMBOL
    )

    index_snap = (
        snapshot(index_frame)
        if frame_recent(index_frame)
        else None
    )

    available = {}

    for symbol in UNIVERSE:
        frame = frames.get(symbol)

        if frame_recent(frame):
            snap = snapshot(frame)

            if snap:
                available[symbol] = snap

    asof = max(
        (
            snap["date"]
            for snap in available.values()
        ),
        default=None,
    )

    comparable = {
        symbol: snap
        for symbol, snap in available.items()
        if snap["date"] == asof
    }

    breadth = (
        100
        * sum(
            snap["price"]
            > snap["ema50"]
            for snap in comparable.values()
        )
        / len(comparable)
        if comparable
        else None
    )

    coverage = (
        len(comparable)
        / len(UNIVERSE)
        if UNIVERSE
        else 0
    )

    sufficient = bool(
        asof
        and len(comparable) >= 20
        and coverage >= 0.8
        and index_snap
        and index_snap["date"] == asof
    )

    positive = bool(
        sufficient
        and breadth >= 40
        and index_snap["price"]
        > index_snap["ema50"]
    )

    reasons = []

    if not asof:
        reasons.append(
            "ไม่มีข้อมูลหุ้นอ้างอิงที่ใหม่พอ"
        )

    elif len(comparable) < 20:
        reasons.append(
            "หุ้นอ้างอิงที่ใช้ได้ต่ำกว่า 20 ตัว"
        )

    elif coverage < 0.8:
        reasons.append(
            "ข้อมูลหุ้นอ้างอิงครอบคลุมต่ำกว่า 80%"
        )

    if not index_snap:
        reasons.append(
            "ข้อมูลดัชนีไม่พร้อมหรือเก่าเกินเกณฑ์"
        )

    elif asof and index_snap["date"] != asof:
        reasons.append(
            "วันที่ข้อมูลดัชนีกับหุ้นอ้างอิงไม่ตรงกัน"
        )

    if sufficient:
        if breadth < 40:
            reasons.append(
                f"Breadth {breadth:.1f}% "
                "ต่ำกว่าเกณฑ์ 40%"
            )

        if (
            index_snap["price"]
            <= index_snap["ema50"]
        ):
            reasons.append(
                "ดัชนียังไม่ยืนเหนือ EMA50"
            )

    return {
        "date": asof,
        "breadth": breadth,
        "checked": len(comparable),
        "sufficient": sufficient,
        "positive": positive,
        "index_ret20": (
            index_snap["ret20"]
            if index_snap
            else None
        ),
        "reasons": reasons,
    }


def analyze_symbol(
    symbol,
    frame,
    context,
):
    if not frame_recent(frame):
        return None

    snap = snapshot(frame)

    if snap is None:
        return None

    price = snap["price"]

    uptrend = bool(
        price > snap["ema50"]
        and snap["ema20"]
        > snap["ema50"]
    )

    setup = None

    if uptrend:
        if (
            price
            > snap["previous_high20"]
            and snap["volume_ratio"]
            >= 1.3
        ):
            setup = "BREAKOUT"

        elif (
            -1
            <= snap["extension_pct"]
            <= 4.5
            and 40
            <= snap["rsi"]
            <= 62
            and snap["hist"]
            > snap["hist_prev"]
        ):
            setup = "PULLBACK"

        elif (
            snap["hist"] > 0
            and 52
            <= snap["rsi"]
            <= 72
        ):
            setup = "MOMENTUM"

    tests = [
        (
            price
            > snap["ema20"]
            > snap["ema50"],
            20,
            "ราคายืนเหนือ EMA20 และ EMA50",
        ),
        (
            snap["weekly_up"],
            12,
            "แนวโน้มสัปดาห์ที่จบแล้วเป็นบวก",
        ),
        (
            context["index_ret20"]
            is not None
            and snap["ret20"]
            > context["index_ret20"],
            10,
            "ผลตอบแทน 20 วันแข็งกว่าดัชนี",
        ),
        (
            snap["hist"] > 0
            and snap["hist"]
            > snap["hist_prev"],
            10,
            "MACD เป็นบวกและเพิ่มขึ้น",
        ),
        (
            50
            <= snap["rsi"]
            <= 70,
            8,
            "RSI อยู่ในช่วงที่กำหนด",
        ),
        (
            snap["volume_ratio"]
            >= 1.5,
            10,
            "Volume สูงกว่าค่าเฉลี่ย",
        ),
        (
            setup is not None,
            12,
            "มีรูปแบบเข้าซื้อตามกฎ",
        ),
        (
            snap["strong_close"],
            8,
            "ราคาปิดใกล้จุดสูงของวัน",
        ),
        (
            MIN_ATR_PCT
            <= snap["atr_pct"]
            <= 7,
            5,
            "ATR อยู่ในช่วงที่กำหนด",
        ),
        (
            context["positive"],
            5,
            "ตลาดรวมผ่านเกณฑ์",
        ),
    ]

    score = sum(
        points
        for passed, points, _ in tests
        if passed
    )

    reasons = [
        reason
        for passed, _, reason in tests
        if passed
    ]

    stop = round(
        max(
            price - 1.5 * snap["atr"],
            snap["previous_low10"]
            * 0.995,
        ),
        2,
    )

    stop = min(
        stop,
        round(
            price * 0.985,
            2,
        ),
    )

    stop = max(
        stop,
        round(
            price * 0.90,
            2,
        ),
    )

    target = round(
        price
        + 2 * (price - stop),
        2,
    )

    blocked = [
        not context["sufficient"],
        not context["positive"],
        snap["date"]
        != context["date"],
        not uptrend,
        setup is None,
        snap["average_value"]
        < MIN_AVG_VALUE,
        not (
            MIN_ATR_PCT
            <= snap["atr_pct"]
            <= MAX_ATR_PCT
        ),
        snap["rsi"] >= 80,
        stop <= 0,
        stop >= price,
    ]

    return {
        **snap,
        "symbol": symbol,
        "sector": SECTOR_MAP.get(
            symbol,
            "ทั่วไป",
        ),
        "setup": setup,
        "score": score,
        "stop": stop,
        "target": target,
        "reasons": reasons,
        "passes": not any(blocked),
    }


# =========================================================
# RISK AND POSITION SIZE
# =========================================================

def portfolio_equity(
    portfolio,
    frames,
):
    cash = float(
        portfolio.get("cash", 0)
    )

    stock_value = 0.0
    missing = []

    for symbol, info in portfolio["stocks"].items():
        frame = frames.get(symbol)

        if not frame_recent(frame):
            missing.append(symbol)
            continue

        stock_value += (
            int(info["shares"])
            * float(
                frame["Close"].iloc[-1]
            )
        )

    if missing:
        return None, None, missing

    return cash + stock_value, stock_value, []


def open_risk(
    portfolio,
    frames,
):
    total = 0.0

    for symbol, info in portfolio["stocks"].items():
        frame = frames.get(symbol)

        if not frame_recent(frame):
            return None

        stop = float(
            info.get(
                "stop_loss",
                0,
            )
        )

        if stop <= 0:
            return None

        price = float(
            frame["Close"].iloc[-1]
        )

        total += (
            max(
                0,
                price - stop,
            )
            * int(info["shares"])
        )

    return total


def paused(
    portfolio,
    equity,
):
    plan = portfolio["financial_plan"]

    peak = max(
        float(
            portfolio.get(
                "high_water_mark",
                equity,
            )
        ),
        equity,
    )

    drawdown = (
        (peak - equity) / peak
        if peak > 0
        else 0
    )

    return bool(
        equity
        <= float(
            plan.get(
                "trading_paused_at_value",
                8000,
            )
        )
        or drawdown
        >= float(
            plan.get(
                "max_portfolio_drawdown_pct",
                0.20,
            )
        )
    )


def trade_cost(
    entry,
    quantity,
):
    gross = (
        entry
        * (1 + SLIPPAGE)
        * quantity
    )

    return (
        gross
        + commission(gross)
    )


def trade_stop_loss(
    entry,
    stop,
    quantity,
):
    cost = trade_cost(
        entry,
        quantity,
    )

    exit_gross = (
        stop
        * (1 - SLIPPAGE)
        * quantity
    )

    proceeds = (
        exit_gross
        - commission(exit_gross)
    )

    return max(
        0.0,
        cost - proceeds,
    )


def choose_quantity(
    entry,
    stop,
    money_limit,
    risk_limit,
):
    if (
        entry <= stop
        or risk_limit <= 0
        or money_limit <= 0
    ):
        return 0

    estimated_cap = int(
        money_limit
        / (
            entry
            * (1 + SLIPPAGE)
        )
    )

    step = BOARD_LOT

    if (
        ALLOW_ODD_LOT
        and estimated_cap < BOARD_LOT
    ):
        step = 1

    low = 0
    high = max(
        0,
        estimated_cap // step,
    )

    while low < high:
        middle = (
            low + high + 1
        ) // 2

        quantity = (
            middle * step
        )

        if (
            trade_cost(
                entry,
                quantity,
            )
            <= money_limit
            and trade_stop_loss(
                entry,
                stop,
                quantity,
            )
            <= risk_limit
        ):
            low = middle

        else:
            high = middle - 1

    return low * step


# =========================================================
# SIGNAL TRACKING
# =========================================================

def new_signal(item):
    return bool(
        isinstance(item, dict)
        and item.get(
            "tracking_version"
        ) == TRACKING_VERSION
    )


def active_symbols(portfolio):
    return {
        item.get("symbol")
        for item in portfolio["signal_history"]
        if new_signal(item)
        and item.get("status") in {
            "PENDING",
            "DELIVERY_UNKNOWN",
        }
    }


def prepare_signals(
    user_id,
    selected,
):
    created_ids = []

    def prepare(portfolio):
        history = portfolio[
            "signal_history"
        ]

        active = active_symbols(
            portfolio
        )

        for item in selected:
            symbol = item["symbol"]

            if symbol in active:
                continue

            same_bar = any(
                new_signal(old)
                and old.get("symbol")
                == symbol
                and old.get("data_date")
                == item["date"]
                for old in history
            )

            if same_bar:
                continue

            signal_id = uuid.uuid4().hex

            history.append({
                "id": signal_id,
                "tracking_version":
                    TRACKING_VERSION,
                "created_at": utc_text(),
                "published_at": None,
                "eligible_date": None,
                "data_date":
                    item["date"],
                "symbol": symbol,
                "setup": item["setup"],
                "score": item["score"],
                "entry_price":
                    item["price"],
                "stop_loss":
                    item["stop"],
                "target_price":
                    item["target"],
                "quantity":
                    item["quantity"],
                "horizon":
                    SIGNAL_HORIZON,
                "slippage":
                    SLIPPAGE,
                "max_entry_gap":
                    MAX_ENTRY_GAP,
                "fees": {
                    "rate":
                        COMMISSION_RATE,
                    "vat":
                        VAT_RATE,
                    "minimum":
                        MIN_COMMISSION,
                },
                "status":
                    "DELIVERY_UNKNOWN",
            })

            created_ids.append(
                signal_id
            )
            active.add(symbol)

        return "จองสัญญาณแล้ว"

    mutate_portfolio(
        user_id,
        prepare,
    )

    return created_ids


def confirm_delivery(
    user_id,
    signal_ids,
):
    if not signal_ids:
        return

    published = now_bkk()
    id_set = set(signal_ids)

    def update(portfolio):
        for item in portfolio["signal_history"]:
            if (
                not new_signal(item)
                or item.get("id")
                not in id_set
                or item.get("status")
                != "DELIVERY_UNKNOWN"
            ):
                continue

            data_day = (
                datetime.fromisoformat(
                    item["data_date"]
                ).date()
            )

            before_open = bool(
                (
                    published.hour,
                    published.minute,
                ) < (9, 30)
                and data_day
                < published.date()
            )

            eligible = (
                published.date()
                if before_open
                else published.date()
                + timedelta(days=1)
            )

            item["published_at"] = (
                published.isoformat()
            )

            item["eligible_date"] = (
                eligible.isoformat()
            )

            item["status"] = (
                "PENDING"
            )

        return "ยืนยันเวลาส่งแล้ว"

    mutate_portfolio(
        user_id,
        update,
    )


def benchmark_return(
    index_frame,
    entry_day,
    exit_day,
):
    if (
        index_frame is None
        or entry_day
        not in index_frame.index
        or exit_day
        not in index_frame.index
    ):
        return None

    first = float(
        index_frame.loc[
            entry_day,
            "Close",
        ]
    )

    last = float(
        index_frame.loc[
            exit_day,
            "Close",
        ]
    )

    if first <= 0:
        return None

    return (
        last / first - 1
    ) * 100


def evaluate_signal(
    item,
    frame,
    index_frame,
):
    if (
        not frame_recent(frame)
        or not frame_recent(
            index_frame
        )
    ):
        return (
            None,
            "ข้อมูลหุ้นหรือดัชนียังไม่พร้อม",
        )

    if not {
        "Dividends",
        "Stock Splits",
    }.issubset(
        frame.columns
    ):
        return (
            None,
            "ข้อมูลปันผล/แตกหุ้นไม่ครบ",
        )

    eligible = item.get(
        "eligible_date"
    )

    if not eligible:
        return (
            None,
            "ยังไม่มีวันเริ่มติดตามที่ยืนยันได้",
        )

    data_day = pd.Timestamp(
        item["data_date"]
    )

    if frame.index[0] > data_day:
        return (
            None,
            "ข้อมูลราคาไม่ครอบคลุมวันอ้างอิง",
        )

    latest = min(
        frame.index[-1],
        index_frame.index[-1],
    )

    days = index_frame.index[
        (
            index_frame.index
            >= pd.Timestamp(
                eligible
            )
        )
        & (
            index_frame.index
            <= latest
        )
    ]

    if len(days) == 0:
        return (
            None,
            "รอวันซื้อขายหลังส่งคำแนะนำ",
        )

    entry_ref = float(
        item["entry_price"]
    )

    stop = float(
        item["stop_loss"]
    )

    target = float(
        item["target_price"]
    )

    quantity = int(
        item["quantity"]
    )

    slippage = float(
        item["slippage"]
    )

    max_gap = float(
        item["max_entry_gap"]
    )

    fees = item["fees"]
    horizon = int(
        item["horizon"]
    )

    if (
        not (
            0
            < stop
            < entry_ref
            < target
        )
        or quantity <= 0
        or horizon <= 0
    ):
        return ({
            "status": "REVIEW",
            "detail": (
                "ข้อมูลระดับราคา "
                "หรือจำนวนหุ้นไม่ถูกต้อง"
            ),
            "evaluated_at":
                utc_text(),
        }, None)

    entry_fill = None
    entry_cost = None
    entry_day = None

    def finish(
        status,
        day,
        detail,
        exit_price=None,
    ):
        outcome = {
            "status": status,
            "event_date":
                day.strftime(
                    "%Y-%m-%d"
                ),
            "detail": detail,
            "evaluated_at":
                utc_text(),
        }

        if entry_fill is not None:
            outcome[
                "entry_fill"
            ] = entry_fill

            outcome[
                "entry_date"
            ] = entry_day.strftime(
                "%Y-%m-%d"
            )

        if (
            exit_price is not None
            and entry_cost is not None
        ):
            exit_fill = (
                exit_price
                * (1 - slippage)
            )

            gross = (
                exit_fill
                * quantity
            )

            proceeds = (
                gross
                - commission(
                    gross,
                    fees,
                )
            )

            net_pnl = (
                proceeds
                - entry_cost
            )

            outcome[
                "exit_fill"
            ] = exit_fill

            outcome[
                "net_pnl"
            ] = net_pnl

            outcome[
                "net_return_pct"
            ] = (
                net_pnl
                / entry_cost
                * 100
            )

            outcome[
                "index_return_pct"
            ] = benchmark_return(
                index_frame,
                entry_day,
                day,
            )

        return outcome, None

    for number, day in enumerate(
        days[:horizon],
        1,
    ):
        if day not in frame.index:
            return (
                None,
                (
                    f"ข้อมูลหุ้นวันที่ "
                    f"{day.date()} ขาด "
                    "จึงไม่ข้ามไปตัดสินวันหลัง"
                ),
            )

        row = frame.loc[day]

        if (
            float(
                row["Dividends"]
            ) != 0
            or float(
                row["Stock Splits"]
            ) != 0
        ):
            return finish(
                "REVIEW",
                day,
                "พบปันผลหรือแตกหุ้น "
                "ต้องตรวจผลกระทบต่อราคา",
            )

        opening = float(
            row["Open"]
        )

        high = float(
            row["High"]
        )

        low = float(
            row["Low"]
        )

        close = float(
            row["Close"]
        )

        if entry_fill is None:
            candidate_fill = (
                opening
                * (1 + slippage)
            )

            if (
                candidate_fill
                <= stop
                or candidate_fill
                >= target
                or candidate_fill
                > entry_ref
                * (1 + max_gap)
            ):
                return finish(
                    "CANCELLED",
                    day,
                    "ราคาเปิดไม่เข้าเงื่อนไขซื้อจำลอง",
                )

            entry_fill = (
                candidate_fill
            )
            entry_day = day

            gross = (
                entry_fill
                * quantity
            )

            entry_cost = (
                gross
                + commission(
                    gross,
                    fees,
                )
            )

        else:
            if opening <= stop:
                return finish(
                    "STOP",
                    day,
                    "เปิดต่ำกว่า Stop "
                    "ใช้ราคาเปิดจำลองการออก",
                    opening,
                )

            if opening >= target:
                return finish(
                    "TARGET",
                    day,
                    "เปิดถึงเป้า "
                    "ใช้ราคาเป้าจำลองการออก",
                    target,
                )

        hit_stop = (
            low <= stop
        )

        hit_target = (
            high >= target
        )

        if (
            hit_stop
            and hit_target
        ):
            return finish(
                "AMBIGUOUS",
                day,
                "แท่งเดียวแตะ Stop "
                "และเป้า แยกลำดับไม่ได้",
            )

        if hit_stop:
            return finish(
                "STOP",
                day,
                "แตะ Stop",
                stop,
            )

        if hit_target:
            return finish(
                "TARGET",
                day,
                "แตะเป้า",
                target,
            )

        if number == horizon:
            return finish(
                "TIMEOUT",
                day,
                f"ครบ {horizon} วันซื้อขาย",
                close,
            )

    return (
        None,
        (
            f"ติดตามแล้ว "
            f"{min(len(days), horizon)}"
            f"/{horizon} วันซื้อขาย"
        ),
    )


def build_performance_report(
    user_id,
):
    portfolio = get_portfolio(
        user_id
    )

    history = portfolio[
        "signal_history"
    ]

    if not history:
        return (
            "ยังไม่มีประวัติคำแนะนำที่ติดตามได้\n"
            "เมื่อรายงาน ควรซื้อวันนี้ หรือ "
            "แผนเทรดวันนี้ ส่งหุ้นที่ผ่านเกณฑ์ "
            "ระบบจะเริ่มบันทึก"
        )

    pending = [
        item
        for item in history
        if new_signal(item)
        and item.get("status")
        == "PENDING"
    ]

    frames = (
        get_market_frames(
            [
                item["symbol"]
                for item in pending
            ]
            + [INDEX_SYMBOL]
        )
        if pending
        else {}
    )

    waiting_notes = {}

    if pending:
        def update(portfolio):
            for item in portfolio[
                "signal_history"
            ]:
                if (
                    not new_signal(item)
                    or item.get("status")
                    != "PENDING"
                ):
                    continue

                outcome, waiting = (
                    evaluate_signal(
                        item,
                        frames.get(
                            item["symbol"]
                        ),
                        frames.get(
                            INDEX_SYMBOL
                        ),
                    )
                )

                if outcome is None:
                    waiting_notes[
                        item["id"]
                    ] = waiting

                else:
                    item["status"] = (
                        outcome["status"]
                    )

                    item["outcome"] = (
                        outcome
                    )

            return "อัปเดตผลแล้ว"

        mutate_portfolio(
            user_id,
            update,
        )

    history = get_portfolio(
        user_id
    )["signal_history"]

    statuses = Counter(
        item.get(
            "status",
            "REVIEW",
        )
        for item in history
        if new_signal(item)
    )

    legacy_count = sum(
        not new_signal(item)
        for item in history
    )

    grouped = defaultdict(
        list
    )

    for item in history:
        if new_signal(item):
            grouped[
                item.get(
                    "setup"
                ) or "ไม่ระบุ"
            ].append(item)

    lines = [
        "ผลการแนะนำย้อนหลัง — ผลจำลอง",
        (
            f"ถึงเป้า {statuses['TARGET']} "
            f"| Stop {statuses['STOP']} "
            f"| ครบเวลา {statuses['TIMEOUT']}"
        ),
        (
            f"กำลังติดตาม {statuses['PENDING']} "
            f"| แตะทั้งคู่ "
            f"{statuses['AMBIGUOUS']}"
        ),
        (
            f"ไม่ได้เข้า {statuses['CANCELLED']} "
            f"| ต้องตรวจ {statuses['REVIEW']}"
        ),
        (
            f"ส่งไม่ยืนยัน "
            f"{statuses['DELIVERY_UNKNOWN']} "
            f"| ประวัติรุ่นเก่า {legacy_count}"
        ),
        "",
        "แยกตามรูปแบบ:",
    ]

    for setup in (
        "BREAKOUT",
        "PULLBACK",
        "MOMENTUM",
        "ไม่ระบุ",
    ):
        items = grouped.get(
            setup,
            [],
        )

        if not items:
            continue

        counts = Counter(
            item.get(
                "status"
            )
            for item in items
        )

        evaluated = [
            item["outcome"]
            for item in items
            if (
                item.get(
                    "status"
                )
                in {
                    "TARGET",
                    "STOP",
                    "TIMEOUT",
                }
                and "net_pnl"
                in item.get(
                    "outcome",
                    {}
                )
            )
        ]

        net_total = sum(
            result["net_pnl"]
            for result in evaluated
        )

        profit_count = sum(
            result["net_pnl"] > 0
            for result in evaluated
        )

        not_evaluated = (
            counts["PENDING"]
            + counts["AMBIGUOUS"]
            + counts["CANCELLED"]
            + counts["REVIEW"]
            + counts[
                "DELIVERY_UNKNOWN"
            ]
        )

        lines.extend([
            (
                f"- {setup}: "
                f"ทั้งหมด {len(items)} "
                f"| ถึงเป้า "
                f"{counts['TARGET']} "
                f"| Stop "
                f"{counts['STOP']} "
                f"| ครบเวลา "
                f"{counts['TIMEOUT']}"
            ),
            (
                f"  กำไรหลังต้นทุน "
                f"{profit_count}/"
                f"{len(evaluated)} "
                f"รายการที่คำนวณได้ "
                f"| P/L รวมรายสัญญาณ "
                f"{net_total:+,.2f} บาท"
            ),
            (
                f"  รอ/ประเมินไม่ได้ "
                f"{not_evaluated}"
            ),
        ])

    lines.extend([
        "",
        "10 รายการล่าสุด:",
    ])

    for item in history[-10:]:
        if not isinstance(
            item,
            dict,
        ):
            continue

        symbol = display_symbol(
            item.get(
                "symbol",
                "?",
            )
        )

        if not new_signal(item):
            lines.append(
                f"- {symbol}: LEGACY "
                "— ไม่รวมสถิติใหม่"
            )
            continue

        status = item.get(
            "status",
            "REVIEW",
        )

        lines.append(
            f"- {symbol} "
            f"[{item.get('data_date', '-')}] "
            f"{item.get('setup', '-')} "
            f"— {status}"
        )

        outcome = item.get(
            "outcome",
            {},
        )

        if outcome:
            lines.append(
                "  "
                + outcome.get(
                    "detail",
                    "",
                )
            )

            if "net_pnl" in outcome:
                lines.append(
                    "  P/L จำลองสุทธิ "
                    f"{outcome['net_pnl']:+,.2f} บาท "
                    f"({outcome['net_return_pct']:+.2f}%)"
                )

                index_return = (
                    outcome.get(
                        "index_return_pct"
                    )
                )

                if index_return is not None:
                    lines.append(
                        "  ดัชนีช่วงเดียวกัน "
                        f"{index_return:+.2f}% "
                        "(ยังไม่หักต้นทุน)"
                    )

        elif status == "PENDING":
            lines.append(
                "  "
                + waiting_notes.get(
                    item["id"],
                    "รอข้อมูลวันซื้อขายถัดไป",
                )
            )

    lines.extend([
        "",
        "คะแนนไม่ใช่โอกาสชนะ",
        (
            "P/L รวมรายสัญญาณไม่ใช่"
            "ผลตอบแทนพอร์ตจริง "
            "เพราะสัญญาณอาจเกิดพร้อมกัน"
        ),
        (
            "ไม่รวมรายการที่ข้อมูลไม่พอ "
            "หรือแยกลำดับไม่ได้เป็นชนะ/แพ้"
        ),
    ])

    return "\n".join(
        lines
    )


# =========================================================
# REPORTS
# =========================================================

def combined_universe(
    portfolio,
):
    return unique_symbols(
        portfolio["watchlist"]
        + list(
            portfolio["stocks"]
        )
    )


def market_text(context):
    breadth = (
        f"{context['breadth']:.1f}%"
        if context["breadth"]
        is not None
        else "ไม่ทราบ"
    )

    status = (
        "ผ่านเกณฑ์"
        if context["positive"]
        else "ไม่ผ่านเกณฑ์"
    )

    lines = [
        (
            f"ตลาด {status} "
            f"| Breadth {breadth} "
            f"จาก {context['checked']}"
            f"/{len(UNIVERSE)} "
            "หุ้นอ้างอิง"
        )
    ]

    if context["reasons"]:
        lines.append(
            "เหตุผล: "
            + "; ".join(
                context["reasons"]
            )
        )

    return "\n".join(
        lines
    )


def scan_candidates(
    portfolio,
    frames,
    context,
    requested,
    limit,
):
    """
    ส่งคืน selected และคำอธิบายเมื่อไม่ผ่าน
    ไม่ลดเกณฑ์คัดหุ้นเพื่อให้มีคำแนะนำ
    """
    equity, _, missing = (
        portfolio_equity(
            portfolio,
            frames,
        )
    )

    if missing or equity is None:
        return [], (
            "ประเมินพอร์ตไม่ครบ: "
            + ", ".join(
                display_symbol(symbol)
                for symbol in missing
            )
        )

    if paused(
        portfolio,
        equity,
    ):
        return (
            [],
            "พอร์ตเข้าเงื่อนไขหยุดเพิ่มความเสี่ยง",
        )

    existing_risk = open_risk(
        portfolio,
        frames,
    )

    if existing_risk is None:
        return (
            [],
            "ยังคำนวณความเสี่ยงหุ้นที่ถืออยู่ไม่ครบ",
        )

    profile = RISK_PROFILES[
        portfolio["risk_mode"]
    ]

    slots = max(
        0,
        profile["max_positions"]
        - len(
            portfolio["stocks"]
        ),
    )

    if slots == 0:
        return (
            [],
            "จำนวนสถานะในพอร์ตเต็ม",
        )

    if not context["positive"]:
        return (
            [],
            "ยังไม่เสนอซื้อใหม่เพราะตลาดรวมไม่ผ่านเกณฑ์",
        )

    active = active_symbols(
        portfolio
    )

    candidates = []
    counts = Counter()

    for symbol in unique_symbols(
        requested
    ):
        if symbol in portfolio["stocks"]:
            counts[
                "held"
            ] += 1
            continue

        if symbol in active:
            counts[
                "active"
            ] += 1
            continue

        frame = frames.get(
            symbol
        )

        if not frame_recent(frame):
            counts[
                "missing"
            ] += 1
            continue

        result = analyze_symbol(
            symbol,
            frame,
            context,
        )

        if result is None:
            counts[
                "missing"
            ] += 1
            continue

        if not result["passes"]:
            counts[
                "rules"
            ] += 1
            continue

        if (
            result["score"]
            < profile[
                "min_score"
            ]
        ):
            counts[
                "score"
            ] += 1
            continue

        candidates.append(
            result
        )

    candidates.sort(
        key=lambda item: (
            item["score"]
        ),
        reverse=True,
    )

    cash = float(
        portfolio["cash"]
    )

    selected = []

    for result in candidates:
        if len(selected) >= min(
            limit,
            slots,
        ):
            break

        money_limit = min(
            cash,
            equity
            * profile[
                "max_weight"
            ],
        )

        risk_limit = min(
            equity
            * profile[
                "risk_per_trade"
            ],
            max(
                0,
                equity
                * profile[
                    "max_total_risk"
                ]
                - existing_risk,
            ),
        )

        quantity = choose_quantity(
            result["price"],
            result["stop"],
            money_limit,
            risk_limit,
        )

        if quantity <= 0:
            counts[
                "budget"
            ] += 1
            continue

        cost = trade_cost(
            result["price"],
            quantity,
        )

        loss = trade_stop_loss(
            result["price"],
            result["stop"],
            quantity,
        )

        candidate = dict(
            result
        )

        candidate[
            "quantity"
        ] = quantity

        candidate[
            "estimated_cost"
        ] = cost

        candidate[
            "estimated_stop_loss"
        ] = loss

        selected.append(
            candidate
        )

        cash -= cost
        existing_risk += loss

    explanations = []

    label_map = {
        "held":
            "อยู่ในพอร์ตแล้ว",
        "active":
            "กำลังติดตามสัญญาณเดิม",
        "missing":
            "ข้อมูลไม่พอ/เก่า",
        "rules":
            "ไม่ผ่านกฎคัดกรอง",
        "score":
            "คะแนนต่ำกว่าเกณฑ์",
        "budget":
            "งบหรือความเสี่ยงไม่พอ",
    }

    for key, label in label_map.items():
        if counts[key]:
            explanations.append(
                f"{label} "
                f"{counts[key]} ตัว"
            )

    issue = (
        "สรุปการคัดกรอง: "
        + "; ".join(
            explanations
        )
        if explanations
        else None
    )

    return selected, issue


def build_buy_today_report(
    portfolio,
    frames=None,
    context=None,
):
    symbols = combined_universe(
        portfolio
    )

    if frames is None:
        frames = get_market_frames(
            UNIVERSE
            + symbols
            + list(
                portfolio["stocks"]
            )
            + [INDEX_SYMBOL]
        )

    if context is None:
        context = market_context(
            frames
        )

    selected, issue = (
        scan_candidates(
            portfolio,
            frames,
            context,
            symbols,
            5,
        )
    )

    lines = [
        "หุ้นควรซื้อวันนี้",
        (
            "เวลารายงาน "
            + now_bkk().strftime(
                "%d/%m/%Y %H:%M"
            )
        ),
        (
            "แท่งราคาล่าสุด "
            + (
                context["date"]
                or "ไม่พร้อม"
            )
        ),
        market_text(
            context
        ),
    ]

    if issue:
        lines.append(
            issue
        )

    if not selected:
        lines.append(
            "ยังไม่มีแผนซื้อใหม่ที่ผ่านครบ"
        )

        return (
            "\n".join(lines),
            [],
        )

    for number, item in enumerate(
        selected,
        1,
    ):
        lines.extend([
            "",
            (
                f"{number}. "
                f"{display_symbol(item['symbol'])} "
                f"[{item['sector']}] "
                f"คะแนน {item['score']}/100"
            ),
            (
                f"รูปแบบ "
                f"{item['setup']}"
            ),
            (
                f"ราคาอ้างอิง "
                f"{item['price']:.2f} บาท"
            ),
            (
                f"Stop "
                f"{item['stop']:.2f} "
                f"| เป้า "
                f"{item['target']:.2f}"
            ),
            (
                f"จำนวนตามงบ "
                f"{item['quantity']} หุ้น"
            ),
            (
                f"เงินที่ใช้ประมาณ "
                f"{item['estimated_cost']:,.2f} บาท"
            ),
            (
                f"ขาดทุนจำลองที่ Stop "
                f"{item['estimated_stop_loss']:,.2f} บาท"
            ),
            (
                "เหตุผล: "
                + "; ".join(
                    item["reasons"][:3]
                )
            ),
        ])

    lines.append(
        "\nราคาอ้างอิงเป็นแท่งรายวันที่จบแล้ว "
        "ไม่ใช่ราคาซื้อที่รับประกันได้"
    )

    return (
        "\n".join(
            lines
        ),
        selected,
    )


def check_sell_alerts(
    portfolio,
    frames=None,
):
    if not portfolio["stocks"]:
        return ""

    if frames is None:
        frames = get_market_frames(
            list(
                portfolio["stocks"]
            )
        )

    messages = []

    for symbol, info in (
        portfolio["stocks"].items()
    ):
        frame = frames.get(
            symbol
        )

        if not frame_recent(
            frame
        ):
            messages.append(
                f"{display_symbol(symbol)}: "
                "ราคาไม่พร้อม"
            )
            continue

        price = float(
            frame["Close"].iloc[-1]
        )

        stop = float(
            info.get(
                "stop_loss",
                0,
            )
        )

        target = float(
            info.get(
                "target_price",
                0,
            )
        )

        if (
            stop > 0
            and price <= stop
        ):
            messages.append(
                f"{display_symbol(symbol)}: "
                f"ราคาปิด {price:.2f} "
                f"อยู่ที่/ต่ำกว่า Stop "
                f"{stop:.2f}"
            )

        elif (
            target > 0
            and price >= target
        ):
            messages.append(
                f"{display_symbol(symbol)}: "
                f"ราคาปิด {price:.2f} "
                f"ถึงเป้า "
                f"{target:.2f}"
            )

    return "\n".join(
        messages
    )


def build_portfolio_report(
    portfolio,
    frames=None,
):
    if frames is None:
        frames = (
            get_market_frames(
                list(
                    portfolio["stocks"]
                )
            )
            if portfolio["stocks"]
            else {}
        )

    equity, stock_value, missing = (
        portfolio_equity(
            portfolio,
            frames,
        )
    )

    lines = [
        "สถานะพอร์ต",
        (
            f"เงินสด "
            f"{float(portfolio['cash']):,.2f} บาท"
        ),
    ]

    if equity is None:
        lines.append(
            "ราคาหุ้นบางตัวไม่พร้อม "
            "จึงไม่สรุปมูลค่าพอร์ตรวม"
        )

        lines.append(
            "ข้อมูลที่ขาด: "
            + ", ".join(
                display_symbol(
                    symbol
                )
                for symbol in missing
            )
        )

    else:
        lines.extend([
            (
                f"มูลค่าหุ้นประมาณ "
                f"{stock_value:,.2f} บาท"
            ),
            (
                f"มูลค่ารวมประมาณ "
                f"{equity:,.2f} บาท"
            ),
        ])

    if not portfolio["stocks"]:
        lines.append(
            "ยังไม่มีหุ้นในพอร์ต"
        )

    for symbol, info in (
        portfolio["stocks"].items()
    ):
        frame = frames.get(
            symbol
        )

        if frame_recent(
            frame
        ):
            price_text = (
                f"{float(frame['Close'].iloc[-1]):.2f} บาท "
                f"({frame.index[-1].date()})"
            )

        else:
            price_text = (
                "ราคาไม่พร้อม"
            )

        lines.append(
            f"- {display_symbol(symbol)} "
            f"{int(info['shares'])} หุ้น "
            f"| ต้นทุน "
            f"{float(info.get('average_cost', 0)):.2f} "
            f"| {price_text}"
        )

    return "\n".join(
        lines
    )


def build_plan_report(
    portfolio,
    frames=None,
):
    if frames is None:
        frames = (
            get_market_frames(
                list(
                    portfolio["stocks"]
                )
            )
            if portfolio["stocks"]
            else {}
        )

    equity, _, _ = (
        portfolio_equity(
            portfolio,
            frames,
        )
    )

    plan = portfolio[
        "financial_plan"
    ]

    target = float(
        plan.get(
            "target_value",
            20000,
        )
    )

    months = int(
        plan.get(
            "target_months",
            3,
        )
    )

    lines = [
        "แผนการเงิน",
        (
            f"เป้าหมาย "
            f"{target:,.2f} บาท "
            f"ภายใน {months} เดือน"
        ),
    ]

    if equity is None:
        lines.append(
            "ข้อมูลราคาไม่ครบ "
            "ยังประเมินความคืบหน้าไม่ได้"
        )

    else:
        lines.extend([
            (
                f"มูลค่าพอร์ตประมาณ "
                f"{equity:,.2f} บาท"
            ),
            (
                f"ส่วนต่างถึงเป้า "
                f"{max(0, target - equity):,.2f} บาท"
            ),
        ])

    lines.append(
        "เป้าหมายไม่ใช่ผลตอบแทนที่รับประกัน"
    )

    return "\n".join(
        lines
    )


def trade_statistics(
    portfolio,
):
    sells = [
        item
        for item in portfolio[
            "transactions"
        ]
        if item.get("type")
        == "SELL"
    ]

    if not sells:
        return (
            "ยังไม่มีรายการขายที่บันทึก"
        )

    values = [
        float(
            item.get(
                "realized_pnl",
                0,
            )
        )
        for item in sells
    ]

    wins = sum(
        value > 0
        for value in values
    )

    losses = sum(
        value < 0
        for value in values
    )

    return "\n".join([
        "สถิติรายการขายที่บันทึก",
        (
            f"ขาย {len(sells)} รายการ "
            f"| กำไร {wins} "
            f"| ขาดทุน {losses}"
        ),
        (
            f"รายการที่กำไร "
            f"{wins / len(sells) * 100:.1f}%"
        ),
        (
            f"P/L สุทธิรวม "
            f"{sum(values):+,.2f} บาท"
        ),
        (
            "การขายบางส่วนถูกนับ"
            "เป็นคนละรายการ"
        ),
    ])


def build_daily_plan(
    portfolio,
):
    # ดาวน์โหลดชุดข้อมูลเพียงครั้งเดียวต่อรายงาน
    symbols = combined_universe(
        portfolio
    )

    frames = get_market_frames(
        UNIVERSE
        + symbols
        + list(
            portfolio["stocks"]
        )
        + [INDEX_SYMBOL]
    )

    context = market_context(
        frames
    )

    buy_report, selected = (
        build_buy_today_report(
            portfolio,
            frames=frames,
            context=context,
        )
    )

    sections = [
        buy_report
    ]

    alerts = check_sell_alerts(
        portfolio,
        frames=frames,
    )

    if alerts:
        sections.append(
            "ตรวจจุดขาย\n"
            + alerts
        )

    sections.append(
        build_plan_report(
            portfolio,
            frames=frames,
        )
    )

    return (
        "\n\n--------------------\n\n".join(
            sections
        ),
        selected,
    )


def build_scan_report(
    portfolio,
    requested,
    title,
    limit,
):
    frames = get_market_frames(
        UNIVERSE
        + list(requested)
        + list(
            portfolio["stocks"]
        )
        + [INDEX_SYMBOL]
    )

    context = market_context(
        frames
    )

    selected, issue = (
        scan_candidates(
            portfolio,
            frames,
            context,
            requested,
            limit,
        )
    )

    lines = [
        title,
        market_text(
            context
        ),
        (
            "แท่งราคาอ้างอิง "
            + (
                context["date"]
                or "ไม่พร้อม"
            )
        ),
    ]

    if issue:
        lines.append(
            issue
        )

    if not selected:
        lines.append(
            "ยังไม่มีหุ้นผ่านเกณฑ์เสนอซื้อใหม่"
        )

    for item in selected:
        lines.extend([
            "",
            (
                f"- {display_symbol(item['symbol'])}: "
                f"{item['score']}/100 "
                f"[{item['setup']}]"
            ),
            (
                f"  ราคา "
                f"{item['price']:.2f} "
                f"| Stop "
                f"{item['stop']:.2f} "
                f"| เป้า "
                f"{item['target']:.2f}"
            ),
            (
                f"  จำนวนตามงบ "
                f"{item['quantity']} หุ้น"
            ),
        ])

    lines.append(
        "คำสั่งสแกนนี้ไม่บันทึกสัญญาณ "
        "ใช้ ควรซื้อวันนี้ หรือ แผนเทรดวันนี้ "
        "เมื่อต้องการเริ่มติดตามผล"
    )

    return "\n".join(
        lines
    )


def build_single_report(
    portfolio,
    symbol,
):
    frames = get_market_frames(
        UNIVERSE
        + list(
            portfolio["stocks"]
        )
        + [
            symbol,
            INDEX_SYMBOL,
        ]
    )

    context = market_context(
        frames
    )

    result = analyze_symbol(
        symbol,
        frames.get(
            symbol
        ),
        context,
    )

    if result is None:
        return (
            f"ข้อมูล "
            f"{display_symbol(symbol)} "
            "ไม่พอหรือเก่าเกินเกณฑ์"
        )

    lines = [
        (
            f"วิเคราะห์ "
            f"{display_symbol(symbol)}"
        ),
        market_text(
            context
        ),
        (
            f"ราคาอ้างอิง "
            f"{result['price']:.2f} "
            f"({result['date']})"
        ),
        (
            f"คะแนนตามกฎ "
            f"{result['score']}/100"
        ),
        (
            f"รูปแบบ "
            f"{result['setup'] or 'ยังไม่มี'}"
        ),
        (
            f"RSI "
            f"{result['rsi']:.1f} "
            f"| Volume "
            f"{result['volume_ratio']:.2f} เท่า"
        ),
    ]

    if symbol in portfolio["stocks"]:
        lines.append(
            "หุ้นนี้อยู่ในพอร์ตแล้ว "
            "ให้ใช้ Stop/เป้าที่บันทึกในพอร์ต"
        )

    elif symbol in active_symbols(
        portfolio
    ):
        lines.append(
            "มีสัญญาณเดิมกำลังติดตามอยู่"
        )

    elif result["passes"]:
        lines.append(
            f"Stop สำหรับพิจารณา "
            f"{result['stop']:.2f} "
            f"| เป้า "
            f"{result['target']:.2f}"
        )

    else:
        lines.append(
            "ยังไม่ผ่านเงื่อนไขเสนอซื้อครบ"
        )

    lines.extend(
        "- " + reason
        for reason in result[
            "reasons"
        ][:4]
    )

    lines.append(
        "การวิเคราะห์รายตัวไม่บันทึก"
        "ในผลการแนะนำย้อนหลัง"
    )

    return "\n".join(
        lines
    )


# =========================================================
# MANUAL TRANSACTIONS
# =========================================================

def record_buy(
    portfolio,
    symbol,
    shares,
    price,
    fee,
):
    gross = shares * price

    actual_fee = (
        commission(gross)
        if fee is None
        else fee
    )

    total = (
        gross
        + actual_fee
    )

    cash = float(
        portfolio["cash"]
    )

    if total > cash:
        raise ValueError(
            f"เงินสดไม่พอ "
            f"ต้องใช้ "
            f"{total:,.2f} บาท"
        )

    stocks = portfolio[
        "stocks"
    ]

    old = stocks.get(
        symbol
    )

    if old:
        old_shares = int(
            old["shares"]
        )

        old_cost = float(
            old.get(
                "total_cost",
                float(
                    old.get(
                        "average_cost",
                        0,
                    )
                ) * old_shares,
            )
        )

        new_shares = (
            old_shares
            + shares
        )

        new_cost = (
            old_cost
            + total
        )

        old["shares"] = (
            new_shares
        )

        old["total_cost"] = (
            new_cost
        )

        old["average_cost"] = (
            new_cost
            / new_shares
        )

        note = (
            "คง Stop/เป้าเดิมไว้ "
            "กรุณาทบทวนแผนหลังซื้อเพิ่ม"
        )

        average = old[
            "average_cost"
        ]

    else:
        average = (
            total
            / shares
        )

        stop = round(
            price * 0.95,
            2,
        )

        if stop <= 0:
            stop = max(
                price * 0.95,
                0.01,
            )

        risk_unit = max(
            average - stop,
            0.01,
        )

        stocks[symbol] = {
            "shares": shares,
            "total_cost":
                total,
            "average_cost":
                average,
            "initial_stop":
                stop,
            "stop_loss":
                stop,
            "r_unit":
                risk_unit,
            "target_price":
                round(
                    average
                    + 2
                    * risk_unit,
                    2,
                ),
            "highest_price":
                max(
                    price,
                    average,
                ),
            "opened_at":
                utc_text(),
        }

        note = (
            "Stop เริ่มต้นเป็นค่าประมาณ "
            "ไม่ใช่แนวรับที่ยืนยันจากตลาด"
        )

    portfolio["cash"] = (
        cash - total
    )

    portfolio[
        "fees_total"
    ] = (
        float(
            portfolio[
                "fees_total"
            ]
        )
        + actual_fee
    )

    portfolio[
        "transactions"
    ].append({
        "type": "BUY",
        "symbol":
            symbol,
        "shares":
            shares,
        "price":
            price,
        "fee":
            actual_fee,
        "net_amount":
            total,
        "date":
            utc_text(),
    })

    return (
        f"บันทึกซื้อ "
        f"{display_symbol(symbol)} "
        f"{shares} หุ้นแล้ว\n"
        f"ต้นทุนเฉลี่ย "
        f"{average:.2f} บาท\n"
        f"{note}\n"
        "ไม่ได้ส่งคำสั่งซื้อจริง"
    )


def record_sell(
    portfolio,
    symbol,
    shares,
    price,
    fee,
):
    stocks = portfolio[
        "stocks"
    ]

    if symbol not in stocks:
        raise ValueError(
            "ไม่พบหุ้นนี้ในพอร์ต"
        )

    info = stocks[
        symbol
    ]

    old_shares = int(
        info["shares"]
    )

    if shares > old_shares:
        raise ValueError(
            f"มีหุ้นเพียง "
            f"{old_shares} หุ้น"
        )

    gross = (
        shares
        * price
    )

    actual_fee = (
        commission(gross)
        if fee is None
        else fee
    )

    if actual_fee > gross:
        raise ValueError(
            "ค่าธรรมเนียมสูงกว่า"
            "มูลค่าขาย"
        )

    total_cost = float(
        info.get(
            "total_cost",
            float(
                info.get(
                    "average_cost",
                    0,
                )
            ) * old_shares,
        )
    )

    allocated = (
        total_cost
        * shares
        / old_shares
    )

    received = (
        gross
        - actual_fee
    )

    pnl = (
        received
        - allocated
    )

    portfolio["cash"] = (
        float(
            portfolio["cash"]
        )
        + received
    )

    portfolio[
        "fees_total"
    ] = (
        float(
            portfolio[
                "fees_total"
            ]
        )
        + actual_fee
    )

    portfolio[
        "realized_pnl_total"
    ] = (
        float(
            portfolio[
                "realized_pnl_total"
            ]
        )
        + pnl
    )

    remaining = (
        old_shares
        - shares
    )

    if remaining == 0:
        del stocks[symbol]

    else:
        info["shares"] = (
            remaining
        )

        info["total_cost"] = (
            total_cost
            - allocated
        )

        info["average_cost"] = (
            info["total_cost"]
            / remaining
        )

    portfolio[
        "transactions"
    ].append({
        "type": "SELL",
        "symbol":
            symbol,
        "shares":
            shares,
        "price":
            price,
        "fee":
            actual_fee,
        "realized_pnl":
            pnl,
        "date":
            utc_text(),
    })

    return (
        f"บันทึกขาย "
        f"{display_symbol(symbol)} "
        f"{shares} หุ้นแล้ว\n"
        f"P/L สุทธิ "
        f"{pnl:+,.2f} บาท\n"
        "ไม่ได้ส่งคำสั่งขายจริง"
    )


# =========================================================
# COMMANDS AND BACKGROUND JOBS
# =========================================================

HELP_TEXT = """คำสั่งบอต

ดูข้อมูล
• ดูพอร์ต / วิเคราะห์พอร์ต
• แผนเทรดวันนี้ / แผนวันนี้
• ควรซื้อวันนี้ / ซื้อวันนี้
• เช็กจุดขาย
• ผลการแนะนำ / เช็กผลย้อนหลัง
• แผนการเงิน
• สถิติการเทรด

วิเคราะห์หุ้น
• สรุปหุ้นวันนี้
• สแกนหุ้น / สแกน SET50
• วิเคราะห์ PTT / สแกน PTT

บันทึกและตั้งค่า
• ซื้อ PTT 100 35.50
• ขาย PTT 100 38.00
• ฝากเงิน 1000
• ตั้งจุดขาย PTT 33 40
• โหมดเสี่ยง balanced
• ตั้งเป้าหมาย 20000 3
• ติดตาม PTT / เลิกติดตาม PTT
• รายการติดตาม

บอตไม่ส่งคำสั่งซื้อขายให้โบรกเกอร์"""


executor = ThreadPoolExecutor(
    max_workers=2
)

pending_users = set()
pending_lock = threading.Lock()


def reserve_user(user_id):
    with pending_lock:
        if (
            user_id in pending_users
            or len(
                pending_users
            ) >= 32
        ):
            return False

        pending_users.add(
            user_id
        )

        return True


def release_user(user_id):
    with pending_lock:
        pending_users.discard(
            user_id
        )


def run_background(
    user_id,
    kind,
    payload=None,
):
    try:
        portfolio = get_portfolio(
            user_id
        )

        selected = []

        if kind == "buy_today":
            text, selected = (
                build_buy_today_report(
                    portfolio
                )
            )

        elif kind == "daily_plan":
            text, selected = (
                build_daily_plan(
                    portfolio
                )
            )

        elif kind == "performance":
            text = (
                build_performance_report(
                    user_id
                )
            )

        elif kind == "sell_check":
            alert = (
                check_sell_alerts(
                    portfolio
                )
            )

            text = (
                "ตรวจจุดขาย\n"
                + (
                    alert
                    or (
                        "ยังไม่มีจุดขาย"
                        "ที่ตรวจพบ"
                    )
                )
            )

        elif kind == "summary":
            text = (
                build_scan_report(
                    portfolio,
                    combined_universe(
                        portfolio
                    ),
                    "สรุปหุ้นวันนี้",
                    5,
                )
            )

        elif kind == "scan":
            text = (
                build_scan_report(
                    portfolio,
                    combined_universe(
                        portfolio
                    ),
                    "ผลสแกนหุ้น",
                    10,
                )
            )

        elif kind == "set50":
            text = (
                build_scan_report(
                    portfolio,
                    UNIVERSE,
                    "ผลสแกนชุดหุ้นอ้างอิง",
                    10,
                )
            )

        elif kind == "single":
            text = (
                build_single_report(
                    portfolio,
                    payload,
                )
            )

        else:
            text = (
                "ไม่รู้จักประเภทงาน"
            )

        ids = (
            prepare_signals(
                user_id,
                selected,
            )
            if selected
            else []
        )

        # ถ้าส่งไม่สำเร็จ จะไม่ยืนยันว่า
        # คำแนะนำนี้ส่งถึงผู้ใช้แล้ว
        push_long_message(
            user_id,
            text,
        )

        if ids:
            confirm_delivery(
                user_id,
                ids,
            )

    except StorageError:
        logger.exception(
            "อ่าน/บันทึกพอร์ตไม่สำเร็จ"
        )

        try:
            push_long_message(
                user_id,
                "ข้อมูลพอร์ตมีปัญหา "
                "ระบบไม่ได้รีเซ็ตข้อมูล "
                "กรุณาตรวจ Log และไฟล์สำรอง",
            )
        except Exception:
            logger.exception(
                "ส่งข้อความแจ้งข้อผิดพลาดไม่สำเร็จ"
            )

    except Exception:
        logger.exception(
            "งานเบื้องหลังไม่สำเร็จ"
        )

        try:
            push_long_message(
                user_id,
                "ประมวลผลหรือส่งรายงาน"
                "ไม่สำเร็จ กรุณาตรวจ Log",
            )
        except Exception:
            logger.exception(
                "ส่งข้อความแจ้งข้อผิดพลาดไม่สำเร็จ"
            )

    finally:
        release_user(
            user_id
        )


def queue_background(
    user_id,
    kind,
    payload=None,
):
    if not reserve_user(
        user_id
    ):
        return False

    try:
        executor.submit(
            run_background,
            user_id,
            kind,
            payload,
        )

    except Exception:
        release_user(
            user_id
        )
        raise

    return True


def command_reply(
    user_id,
    text,
    event_id,
):
    parts = text.split()

    if text in {
        "ช่วยเหลือ",
        "help",
        "คำสั่ง",
    }:
        return HELP_TEXT

    if text == "ดูพอร์ต":
        return (
            build_portfolio_report(
                get_portfolio(
                    user_id
                )
            )
        )

    if text == "วิเคราะห์พอร์ต":
        portfolio = get_portfolio(
            user_id
        )

        # ใช้ข้อมูลหุ้นชุดเดียวกัน
        frames = (
            get_market_frames(
                list(
                    portfolio[
                        "stocks"
                    ]
                )
            )
            if portfolio[
                "stocks"
            ]
            else {}
        )

        alert = check_sell_alerts(
            portfolio,
            frames=frames,
        )

        return (
            build_portfolio_report(
                portfolio,
                frames=frames,
            )
            + "\n\nตรวจจุดขาย\n"
            + (
                alert
                or (
                    "ยังไม่มีจุดขาย"
                    "ที่ตรวจพบ"
                )
            )
        )

    if text in {
        "แผนเทรดวันนี้",
        "แผนวันนี้",
    }:
        return (
            "กำลังจัดทำแผน..."
            if queue_background(
                user_id,
                "daily_plan",
            )
            else (
                "มีงานของคุณ"
                "กำลังประมวลผล"
            )
        )

    if text in {
        "ควรซื้อวันนี้",
        "ซื้อวันนี้",
    }:
        return (
            "กำลังคัดกรองหุ้น..."
            if queue_background(
                user_id,
                "buy_today",
            )
            else (
                "มีงานของคุณ"
                "กำลังประมวลผล"
            )
        )

    if text in {
        "ผลการแนะนำ",
        "เช็กผลย้อนหลัง",
        "ตรวจสอบผล",
    }:
        return (
            "กำลังตรวจผลคำแนะนำเดิม..."
            if queue_background(
                user_id,
                "performance",
            )
            else (
                "มีงานของคุณ"
                "กำลังประมวลผล"
            )
        )

    if text in {
        "เช็กจุดขาย",
        "เช็คจุดขาย",
    }:
        return (
            "กำลังตรวจจุดขาย..."
            if queue_background(
                user_id,
                "sell_check",
            )
            else (
                "มีงานของคุณ"
                "กำลังประมวลผล"
            )
        )

    if text in {
        "แผนการเงิน",
        "ความคืบหน้า",
    }:
        return (
            build_plan_report(
                get_portfolio(
                    user_id
                )
            )
        )

    if text == "สถิติการเทรด":
        return (
            trade_statistics(
                get_portfolio(
                    user_id
                )
            )
        )

    scan_kinds = {
        "สรุปหุ้นวันนี้":
            "summary",
        "สแกนหุ้น":
            "scan",
        "สแกน SET50":
            "set50",
    }

    if text in scan_kinds:
        return (
            "กำลังสแกนหุ้น..."
            if queue_background(
                user_id,
                scan_kinds[
                    text
                ],
            )
            else (
                "มีงานของคุณ"
                "กำลังประมวลผล"
            )
        )

    if (
        parts
        and parts[0]
        in {
            "สแกน",
            "วิเคราะห์",
        }
    ):
        if len(parts) != 2:
            raise ValueError(
                "ใช้: วิเคราะห์ PTT"
            )

        symbol = format_symbol(
            parts[1]
        )

        return (
            f"กำลังวิเคราะห์ "
            f"{display_symbol(symbol)}..."
            if queue_background(
                user_id,
                "single",
                symbol,
            )
            else (
                "มีงานของคุณ"
                "กำลังประมวลผล"
            )
        )

    if (
        parts
        and parts[0]
        in {
            "ซื้อ",
            "ขาย",
        }
    ):
        if len(parts) not in {
            4,
            5,
        }:
            raise ValueError(
                "ใช้: ซื้อ PTT 100 "
                "35.50 [ค่าธรรมเนียม]"
            )

        symbol = format_symbol(
            parts[1]
        )

        if symbol.startswith("^"):
            raise ValueError(
                "คำสั่งนี้ใช้กับหุ้น "
                "ไม่ใช่ดัชนี"
            )

        shares = positive_int(
            parts[2],
            "จำนวนหุ้น",
        )

        price = positive_number(
            parts[3],
            "ราคา",
        )

        fee = (
            number(
                parts[4],
                "ค่าธรรมเนียม",
            )
            if len(parts) == 5
            else None
        )

        operation = (
            record_buy
            if parts[0] == "ซื้อ"
            else record_sell
        )

        return mutate_portfolio(
            user_id,
            lambda portfolio:
                operation(
                    portfolio,
                    symbol,
                    shares,
                    price,
                    fee,
                ),
            event_id,
        )

    if (
        parts
        and parts[0]
        == "ฝากเงิน"
    ):
        if len(parts) != 2:
            raise ValueError(
                "ใช้: ฝากเงิน 1000"
            )

        amount = positive_number(
            parts[1],
            "จำนวนเงิน",
        )

        def deposit(
            portfolio
        ):
            portfolio[
                "cash"
            ] = (
                float(
                    portfolio[
                        "cash"
                    ]
                )
                + amount
            )

            portfolio[
                "contributions_total"
            ] = (
                float(
                    portfolio[
                        "contributions_total"
                    ]
                )
                + amount
            )

            portfolio[
                "high_water_mark"
            ] = (
                float(
                    portfolio[
                        "high_water_mark"
                    ]
                )
                + amount
            )

            portfolio[
                "transactions"
            ].append({
                "type":
                    "DEPOSIT",
                "amount":
                    amount,
                "date":
                    utc_text(),
            })

            return (
                "บันทึกฝากเงิน "
                f"{amount:,.2f} บาทแล้ว"
            )

        return mutate_portfolio(
            user_id,
            deposit,
            event_id,
        )

    if (
        parts
        and parts[0]
        == "ตั้งจุดขาย"
    ):
        if len(parts) != 4:
            raise ValueError(
                "ใช้: ตั้งจุดขาย "
                "PTT 33 40"
            )

        symbol = format_symbol(
            parts[1]
        )

        stop = positive_number(
            parts[2],
            "Stop",
        )

        target = positive_number(
            parts[3],
            "เป้า",
        )

        if stop >= target:
            raise ValueError(
                "Stop ต้องต่ำกว่าเป้า"
            )

        def set_levels(
            portfolio
        ):
            if symbol not in portfolio[
                "stocks"
            ]:
                raise ValueError(
                    "ไม่มีหุ้นนี้ในพอร์ต"
                )

            info = portfolio[
                "stocks"
            ][symbol]

            info[
                "stop_loss"
            ] = stop

            info[
                "target_price"
            ] = target

            return (
                f"บันทึกแผน "
                f"{display_symbol(symbol)} "
                f"Stop {stop:.2f} "
                f"| เป้า "
                f"{target:.2f}"
            )

        return mutate_portfolio(
            user_id,
            set_levels,
            event_id,
        )

    if (
        parts
        and parts[0]
        == "โหมดเสี่ยง"
    ):
        if (
            len(parts) != 2
            or parts[1].lower()
            not in RISK_PROFILES
        ):
            raise ValueError(
                "ใช้: โหมดเสี่ยง "
                "conservative / "
                "balanced / aggressive"
            )

        mode = parts[
            1
        ].lower()

        def set_mode(
            portfolio
        ):
            portfolio[
                "risk_mode"
            ] = mode

            return (
                "ตั้งโหมด "
                + RISK_PROFILES[
                    mode
                ]["label"]
                + " แล้ว"
            )

        return mutate_portfolio(
            user_id,
            set_mode,
            event_id,
        )

    if (
        parts
        and parts[0]
        == "ตั้งเป้าหมาย"
    ):
        if len(parts) != 3:
            raise ValueError(
                "ใช้: ตั้งเป้าหมาย "
                "20000 3"
            )

        target = positive_number(
            parts[1],
            "เป้าหมาย",
        )

        months = positive_int(
            parts[2],
            "จำนวนเดือน",
        )

        def set_target(
            portfolio
        ):
            plan = portfolio[
                "financial_plan"
            ]

            plan[
                "target_value"
            ] = target

            plan[
                "target_months"
            ] = months

            return (
                f"ตั้งเป้าหมาย "
                f"{target:,.2f} บาท "
                f"ภายใน "
                f"{months} เดือนแล้ว"
            )

        return mutate_portfolio(
            user_id,
            set_target,
            event_id,
        )

    if (
        parts
        and parts[0]
        in {
            "ติดตาม",
            "เลิกติดตาม",
        }
    ):
        if len(parts) != 2:
            raise ValueError(
                f"ใช้: "
                f"{parts[0]} PTT"
            )

        symbol = format_symbol(
            parts[1]
        )

        if symbol.startswith("^"):
            raise ValueError(
                "Watchlist นี้ใช้กับหุ้น"
            )

        def update_watchlist(
            portfolio
        ):
            values = portfolio[
                "watchlist"
            ]

            if parts[0] == "ติดตาม":
                if symbol not in values:
                    values.append(
                        symbol
                    )

            else:
                portfolio[
                    "watchlist"
                ] = [
                    item
                    for item in values
                    if item != symbol
                ]

            return (
                f"บันทึก "
                f"{parts[0]} "
                f"{display_symbol(symbol)} "
                "แล้ว"
            )

        return mutate_portfolio(
            user_id,
            update_watchlist,
            event_id,
        )

    if text == "รายการติดตาม":
        values = get_portfolio(
            user_id
        )["watchlist"]

        return (
            "รายการติดตาม\n"
            + (
                ", ".join(
                    display_symbol(
                        symbol
                    )
                    for symbol in values
                )
                or "ยังไม่มี"
            )
        )

    return (
        "ไม่รู้จักคำสั่ง "
        "พิมพ์ ช่วยเหลือ"
    )


# =========================================================
# SCHEDULER AND WEBHOOK
# =========================================================

def scheduled_daily_plan():
    try:
        users = registered_users()

    except StorageError:
        logger.exception(
            "อ่านรายชื่อผู้ใช้"
            "สำหรับรายงานเช้าไม่สำเร็จ"
        )
        return

    for user_id in users:
        queue_background(
            user_id,
            "daily_plan",
        )


scheduler = None

if ENABLE_SCHEDULER:
    scheduler = (
        BackgroundScheduler(
            timezone="Asia/Bangkok"
        )
    )

    scheduler.add_job(
        scheduled_daily_plan,
        "cron",
        day_of_week="mon-fri",
        hour=8,
        minute=30,
        id="daily-plan",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()


@app.route(
    "/",
    methods=[
        "GET",
        "HEAD",
    ],
)
def health():
    return jsonify({
        "status": "ok",
        "service":
            "Portfolio Bot",
        "version":
            VERSION,
    })


@app.route(
    "/callback",
    methods=["POST"],
)
def callback():
    signature = (
        request.headers.get(
            "X-Line-Signature",
            "",
        )
    )

    body = request.get_data(
        as_text=True
    )

    try:
        handler.handle(
            body,
            signature,
        )

    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(
    MessageEvent,
    message=TextMessage,
)
def handle_message(event):
    if getattr(
        event.source,
        "type",
        None,
    ) != "user":
        line_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "กรุณาใช้คำสั่งพอร์ต"
                    "ในแชตส่วนตัว"
                )
            ),
        )
        return

    user_id = getattr(
        event.source,
        "user_id",
        None,
    )

    if not user_id:
        return

    text = " ".join(
        event.message.text.split()
    )

    if not text:
        return

    event_id = getattr(
        event,
        "webhook_event_id",
        None,
    )

    if not event_id:
        message_id = getattr(
            event.message,
            "id",
            None,
        )

        if message_id:
            event_id = (
                f"{user_id}:"
                f"{message_id}"
            )

    try:
        reply = command_reply(
            user_id,
            text,
            (
                str(event_id)
                if event_id
                else None
            ),
        )

    except ValueError as error:
        reply = (
            f"ข้อมูลไม่ถูกต้อง: "
            f"{error}"
        )

    except StorageError:
        logger.exception(
            "ข้อมูลพอร์ตมีปัญหา"
        )

        reply = (
            "ข้อมูลพอร์ตมีปัญหา "
            "ระบบไม่ได้รีเซ็ตข้อมูล "
            "กรุณาตรวจ Log และไฟล์สำรอง"
        )

    except Exception:
        logger.exception(
            "ประมวลผลคำสั่งไม่สำเร็จ"
        )

        reply = (
            "ระบบขัดข้องชั่วคราว "
            "กรุณาตรวจ Log"
        )

    line_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text=reply[
                :MAX_LINE_TEXT
            ]
        ),
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "5000",
            )
        ),
        debug=False,
    )
