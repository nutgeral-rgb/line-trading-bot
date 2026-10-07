import copy
import json
import logging
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, abort, jsonify, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# =========================================================
# CONFIGURATION
# =========================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("portfolio-bot")

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv(
    "LINE_CHANNEL_ACCESS_TOKEN", ""
).strip()

LINE_CHANNEL_SECRET = os.getenv(
    "LINE_CHANNEL_SECRET", ""
).strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    logger.warning(
        "LINE_CHANNEL_ACCESS_TOKEN หรือ LINE_CHANNEL_SECRET ยังไม่ได้ตั้งค่า"
    )

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

PORTFOLIO_FILE = os.getenv("PORTFOLIO_FILE", "portfolio.json")
MARKET_INDEX_SYMBOL = os.getenv("MARKET_INDEX_SYMBOL", "^SET.BK")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "900"))
BOARD_LOT = max(1, int(os.getenv("BOARD_LOT", "100")))
MIN_AVG_VALUE = float(os.getenv("MIN_AVG_VALUE", "10000000"))
ENABLE_SCHEDULER = (
    os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
)
TIMEZONE_NAME = os.getenv("TZ", "Asia/Bangkok")

MAX_LINE_TEXT = 4500
SCHEMA_VERSION = 2

FALLBACK_LARGE_CAP_SYMBOLS = [
    "ADVANC", "AOT", "AWC", "BANPU", "BBL",
    "BDMS", "BH", "BJC", "BTS", "CBG",
    "CENTEL", "COM7", "CPALL", "CPAXT", "CPF",
    "CPN", "CRC", "DELTA", "EA", "EGCO",
    "GLOBAL", "GPSC", "GULF", "HMPRO", "ITC",
    "IVL", "KBANK", "KKP", "KTB", "KTC",
    "LH", "MINT", "MTC", "OR", "OSP",
    "PTT", "PTTEP", "PTTGC", "RATCH", "SAWAD",
    "SCB", "SCC", "SCGP", "TCAP", "TIDLOR",
    "TISCO", "TOP", "TRUE", "TU", "WHA",
]

SECTOR_MAP = {
    "ADVANC.BK": "สื่อสาร",
    "AOT.BK": "ขนส่งและการบิน",
    "AWC.BK": "พัฒนาอสังหาริมทรัพย์",
    "BANPU.BK": "พลังงาน",
    "BBL.BK": "ธนาคาร",
    "BDMS.BK": "การแพทย์",
    "BH.BK": "การแพทย์",
    "BJC.BK": "พาณิชย์",
    "BTS.BK": "ขนส่ง",
    "CBG.BK": "อาหารและเครื่องดื่ม",
    "CENTEL.BK": "ท่องเที่ยวและโรงแรม",
    "COM7.BK": "พาณิชย์",
    "CPALL.BK": "พาณิชย์",
    "CPAXT.BK": "พาณิชย์",
    "CPF.BK": "อาหารและการเกษตร",
    "CPN.BK": "พัฒนาอสังหาริมทรัพย์",
    "CRC.BK": "พาณิชย์",
    "DELTA.BK": "ชิ้นส่วนอิเล็กทรอนิกส์",
    "EA.BK": "พลังงานและสาธารณูปโภค",
    "EGCO.BK": "พลังงานและสาธารณูปโภค",
    "GLOBAL.BK": "พาณิชย์",
    "GPSC.BK": "พลังงานและสาธารณูปโภค",
    "GULF.BK": "พลังงานและสาธารณูปโภค",
    "HMPRO.BK": "พาณิชย์",
    "ITC.BK": "อาหารและการเกษตร",
    "IVL.BK": "ปิโตรเคมีและเคมีภัณฑ์",
    "KBANK.BK": "ธนาคาร",
    "KKP.BK": "ธนาคาร",
    "KTB.BK": "ธนาคาร",
    "KTC.BK": "เงินทุนและหลักทรัพย์",
    "LH.BK": "พัฒนาอสังหาริมทรัพย์",
    "MINT.BK": "อาหารและท่องเที่ยว",
    "MTC.BK": "เงินทุนและหลักทรัพย์",
    "OR.BK": "พลังงานและพาณิชย์",
    "OSP.BK": "อาหารและเครื่องดื่ม",
    "PTT.BK": "พลังงาน",
    "PTTEP.BK": "พลังงาน",
    "PTTGC.BK": "ปิโตรเคมีและเคมีภัณฑ์",
    "RATCH.BK": "พลังงานและสาธารณูปโภค",
    "SAWAD.BK": "เงินทุนและหลักทรัพย์",
    "SCB.BK": "ธนาคาร",
    "SCC.BK": "วัสดุก่อสร้าง",
    "SCGP.BK": "บรรจุภัณฑ์",
    "TCAP.BK": "เงินทุนและหลักทรัพย์",
    "TIDLOR.BK": "เงินทุนและหลักทรัพย์",
    "TISCO.BK": "ธนาคาร",
    "TOP.BK": "พลังงาน",
    "TRUE.BK": "สื่อสาร",
    "TU.BK": "อาหารและการเกษตร",
    "WHA.BK": "พัฒนาอสังหาริมทรัพย์",
}


# =========================================================
# GENERAL UTILITIES
# =========================================================

def format_symbol(symbol):
    symbol = str(symbol).upper().strip()
    if not symbol:
        return ""
    if symbol.startswith("^"):
        return symbol
    if not symbol.endswith(".BK"):
        symbol += ".BK"
    return symbol


def display_symbol(symbol):
    return str(symbol).replace(".BK", "")


def unique_symbols(symbols):
    output = []
    seen = set()

    for symbol in symbols:
        formatted = format_symbol(symbol)
        if formatted and formatted not in seen:
            seen.add(formatted)
            output.append(formatted)

    return output


def get_configured_universe():
    raw = os.getenv("SET50_SYMBOLS", "").strip()

    if raw:
        symbols = raw.replace(",", " ").split()
    else:
        symbols = FALLBACK_LARGE_CAP_SYMBOLS

    return unique_symbols(symbols)


SET50_UNIVERSE = get_configured_universe()


def safe_float(value, default=0.0):
    try:
        result = float(value)
        if not math.isfinite(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def positive_float(value, field_name):
    result = safe_float(value, -1)

    if result <= 0:
        raise ValueError(f"{field_name} ต้องมากกว่า 0")

    return result


def non_negative_float(value, field_name):
    result = safe_float(value, -1)

    if result < 0:
        raise ValueError(f"{field_name} ต้องไม่น้อยกว่า 0")

    return result


def positive_int(value, field_name):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} ต้องเป็นจำนวนเต็ม")

    if result <= 0:
        raise ValueError(f"{field_name} ต้องมากกว่า 0")

    return result


def utc_now_text():
    return datetime.now(timezone.utc).isoformat()


def split_message(text, max_length=MAX_LINE_TEXT):
    text = str(text)

    if len(text) <= max_length:
        return [text]

    chunks = []
    current = []

    for line in text.splitlines():
        candidate = "\n".join(current + [line])

        if len(candidate) <= max_length:
            current.append(line)
        else:
            if current:
                chunks.append("\n".join(current))

            if len(line) <= max_length:
                current = [line]
            else:
                for start in range(0, len(line), max_length):
                    chunks.append(line[start:start + max_length])
                current = []

    if current:
        chunks.append("\n".join(current))

    return chunks


def push_long_message(user_id, text):
    for chunk in split_message(text):
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=chunk),
        )


# =========================================================
# PORTFOLIO STORAGE
# =========================================================

storage_lock = threading.RLock()


def default_financial_plan():
    return {
        "target_value": 20000.0,
        "target_months": 3,
        "monthly_contribution": 1000.0,
        "risk_level": "high",
        "max_portfolio_drawdown_pct": 0.20,
        "trading_paused_at_value": 8000.0,
        "risk_per_trade_pct": 0.01,
        "max_stock_weight_pct": 0.25,
        "default_stop_loss_pct": 0.05,
        "risk_reward_ratio": 2.0,
    }


def default_portfolio():
    return {
        "schema_version": SCHEMA_VERSION,
        "cash": 10000.0,
        "initial_capital": 10000.0,
        "contributions_total": 0.0,
        "withdrawals_total": 0.0,
        "realized_pnl_total": 0.0,
        "fees_total": 0.0,
        "high_water_mark": 10000.0,
        "financial_plan": default_financial_plan(),
        "stocks": {},
        "watchlist": SET50_UNIVERSE.copy(),
        "transactions": [],
    }


def looks_like_single_portfolio(data):
    if not isinstance(data, dict):
        return False

    portfolio_keys = {
        "cash",
        "financial_plan",
        "stocks",
        "watchlist",
        "transactions",
        "initial_capital",
    }

    return bool(portfolio_keys.intersection(data.keys()))


def normalize_portfolio(raw):
    base = default_portfolio()

    if not isinstance(raw, dict):
        return base

    old_schema = int(safe_float(raw.get("schema_version"), 0))

    for key in [
        "cash",
        "initial_capital",
        "contributions_total",
        "withdrawals_total",
        "realized_pnl_total",
        "fees_total",
        "high_water_mark",
    ]:
        if key in raw:
            base[key] = safe_float(raw.get(key), base[key])

    if isinstance(raw.get("financial_plan"), dict):
        base["financial_plan"].update(raw["financial_plan"])

    base["financial_plan"]["risk_per_trade_pct"] = min(
        max(
            safe_float(
                base["financial_plan"].get("risk_per_trade_pct"),
                0.01,
            ),
            0.0025,
        ),
        0.02,
    )

    base["financial_plan"]["max_stock_weight_pct"] = min(
        max(
            safe_float(
                base["financial_plan"].get("max_stock_weight_pct"),
                0.25,
            ),
            0.05,
        ),
        0.40,
    )

    if isinstance(raw.get("stocks"), dict):
        base["stocks"] = raw["stocks"]

    if isinstance(raw.get("transactions"), list):
        base["transactions"] = raw["transactions"]

    existing_watchlist = raw.get("watchlist", [])

    if not isinstance(existing_watchlist, list):
        existing_watchlist = []

    if old_schema < SCHEMA_VERSION:
        base["watchlist"] = unique_symbols(
            SET50_UNIVERSE + existing_watchlist
        )
    else:
        base["watchlist"] = unique_symbols(existing_watchlist)

    if not base["watchlist"]:
        base["watchlist"] = SET50_UNIVERSE.copy()

    base["schema_version"] = SCHEMA_VERSION

    clean_stocks = {}

    for symbol, info in base["stocks"].items():
        if not isinstance(info, dict):
            continue

        symbol = format_symbol(symbol)
        shares = int(safe_float(info.get("shares"), 0))

        if not symbol or shares <= 0:
            continue

        average_cost = safe_float(info.get("average_cost"), 0)
        total_cost = safe_float(
            info.get("total_cost"),
            average_cost * shares,
        )

        clean_stocks[symbol] = {
            "shares": shares,
            "total_cost": max(0.0, total_cost),
            "average_cost": max(0.0, average_cost),
            "stop_loss": max(
                0.0,
                safe_float(
                    info.get("stop_loss"),
                    average_cost * 0.95,
                ),
            ),
            "highest_price": max(
                average_cost,
                safe_float(
                    info.get("highest_price"),
                    average_cost,
                ),
            ),
        }

    base["stocks"] = clean_stocks
    return base


def load_store_unlocked():
    if not os.path.exists(PORTFOLIO_FILE):
        return {}

    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("อ่านไฟล์ Portfolio ไม่สำเร็จ")
        return {}


def save_store_unlocked(data):
    absolute_path = os.path.abspath(PORTFOLIO_FILE)
    directory = os.path.dirname(absolute_path)
    os.makedirs(directory, exist_ok=True)

    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix="portfolio_",
        suffix=".tmp",
        dir=directory,
    )

    try:
        with os.fdopen(
            file_descriptor,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_path, absolute_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def get_user_portfolio(user_id):
    with storage_lock:
        data = load_store_unlocked()

        if looks_like_single_portfolio(data):
            portfolio = normalize_portfolio(data)
            data = {user_id: portfolio}
            save_store_unlocked(data)
            return copy.deepcopy(portfolio)

        portfolio = normalize_portfolio(data.get(user_id))
        data[user_id] = portfolio
        save_store_unlocked(data)

        return copy.deepcopy(portfolio)


def update_user_portfolio(user_id, portfolio):
    with storage_lock:
        data = load_store_unlocked()

        if looks_like_single_portfolio(data):
            data = {}

        data[user_id] = normalize_portfolio(portfolio)
        save_store_unlocked(data)


def get_registered_users():
    with storage_lock:
        data = load_store_unlocked()

    if looks_like_single_portfolio(data):
        return []

    return [
        user_id
        for user_id, value in data.items()
        if isinstance(value, dict)
    ]


# =========================================================
# MARKET DATA CACHE
# =========================================================

market_cache = {}
market_cache_lock = threading.RLock()


def extract_downloaded_frame(downloaded, symbol, total_symbols):
    if downloaded is None or downloaded.empty:
        return None

    try:
        if isinstance(downloaded.columns, pd.MultiIndex):
            level_zero = downloaded.columns.get_level_values(0)
            level_one = downloaded.columns.get_level_values(1)

            if symbol in level_zero:
                frame = downloaded[symbol].copy()
            elif symbol in level_one:
                frame = downloaded.xs(
                    symbol,
                    axis=1,
                    level=1,
                ).copy()
            else:
                return None
        elif total_symbols == 1:
            frame = downloaded.copy()
        else:
            return None

        frame.columns = [
            str(column).strip().title()
            for column in frame.columns
        ]

        required = {"Open", "High", "Low", "Close", "Volume"}

        if not required.issubset(frame.columns):
            return None

        frame = frame[
            ["Open", "High", "Low", "Close", "Volume"]
        ].copy()

        frame = frame.dropna(subset=["Close"])

        if frame.empty:
            return None

        return frame
    except Exception:
        logger.exception("แยกข้อมูล %s ไม่สำเร็จ", symbol)
        return None


def get_market_frames(symbols, force=False):
    symbols = unique_symbols(symbols)
    now = time.time()
    results = {}
    missing = []

    with market_cache_lock:
        for symbol in symbols:
            item = market_cache.get(symbol)

            if (
                not force
                and item
                and item["expires_at"] > now
            ):
                if isinstance(item["frame"], pd.DataFrame):
                    results[symbol] = item["frame"].copy()
            else:
                missing.append(symbol)

    if missing:
        try:
            downloaded = yf.download(
                tickers=missing,
                period="6mo",
                interval="1d",
                auto_adjust=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )

            for symbol in missing:
                frame = extract_downloaded_frame(
                    downloaded,
                    symbol,
                    len(missing),
                )

                with market_cache_lock:
                    market_cache[symbol] = {
                        "expires_at": now + (
                            CACHE_SECONDS if frame is not None else 60
                        ),
                        "frame": frame,
                    }

                if frame is not None:
                    results[symbol] = frame.copy()

        except Exception:
            logger.exception("ดาวน์โหลดข้อมูลตลาดแบบ Batch ไม่สำเร็จ")

    return results


# =========================================================
# TECHNICAL ANALYSIS
# =========================================================

def calculate_rsi(close, period=14):
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    average_gain = gains.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    average_loss = losses.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    relative_strength = average_gain / average_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + relative_strength))

    if rsi.empty or pd.isna(rsi.iloc[-1]):
        return 50.0

    return float(rsi.iloc[-1])


def calculate_snapshot(frame):
    if frame is None or len(frame) < 55:
        return None

    data = frame.copy().dropna(subset=["Close"])

    if len(data) < 55:
        return None

    close = data["Close"].astype(float)
    high = data["High"].astype(float)
    low = data["Low"].astype(float)
    volume = data["Volume"].fillna(0).astype(float)

    ema20_series = close.ewm(span=20, adjust=False).mean()
    ema50_series = close.ewm(span=50, adjust=False).mean()

    macd_series = (
        close.ewm(span=12, adjust=False).mean()
        - close.ewm(span=26, adjust=False).mean()
    )
    signal_series = macd_series.ewm(span=9, adjust=False).mean()
    histogram_series = macd_series - signal_series

    previous_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr_series = true_range.rolling(14).mean()

    previous_20 = data.iloc[:-1].tail(20)
    previous_10 = data.iloc[:-1].tail(10)

    if previous_20.empty or previous_10.empty:
        return None

    price = float(close.iloc[-1])
    ema20 = float(ema20_series.iloc[-1])
    ema50 = float(ema50_series.iloc[-1])
    macd_hist = float(histogram_series.iloc[-1])
    atr = float(atr_series.iloc[-1])

    previous_high20 = float(previous_20["High"].max())
    previous_low10 = float(previous_10["Low"].min())

    average_volume20 = float(
        previous_20["Volume"].fillna(0).mean()
    )

    current_volume = float(volume.iloc[-1])

    volume_ratio = (
        current_volume / average_volume20
        if average_volume20 > 0
        else 0
    )

    traded_values = (
        previous_20["Close"]
        * previous_20["Volume"].fillna(0)
    )

    average_traded_value = float(traded_values.mean())

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": calculate_rsi(close),
        "macd_hist": macd_hist,
        "atr": atr if math.isfinite(atr) and atr > 0 else price * 0.03,
        "previous_high20": previous_high20,
        "previous_low10": previous_low10,
        "volume_ratio": volume_ratio,
        "average_traded_value": average_traded_value,
        "data_date": str(data.index[-1].date()),
    }


def get_market_regime(frames):
    frame = frames.get(MARKET_INDEX_SYMBOL)

    if frame is None:
        return {
            "status": "UNKNOWN",
            "is_positive": None,
            "message": "ไม่สามารถตรวจสอบแนวโน้ม SET Index ได้",
        }

    snapshot = calculate_snapshot(frame)

    if snapshot is None:
        return {
            "status": "UNKNOWN",
            "is_positive": None,
            "message": "ข้อมูล SET Index ไม่เพียงพอ",
        }

    positive = (
        snapshot["price"] > snapshot["ema50"]
        and snapshot["ema20"] >= snapshot["ema50"]
    )

    if positive:
        message = (
            "ตลาดรวมเป็นบวก: ดัชนีอยู่เหนือ EMA50 "
            "และ EMA20 ไม่ต่ำกว่า EMA50"
        )
        status = "POSITIVE"
    else:
        message = (
            "ตลาดรวมอ่อนแอ: ลดขนาดสถานะ "
            "และหลีกเลี่ยงการไล่ราคา"
        )
        status = "DEFENSIVE"

    return {
        "status": status,
        "is_positive": positive,
        "message": message,
        "snapshot": snapshot,
    }


def create_trade_levels(snapshot):
    price = snapshot["price"]
    atr = snapshot["atr"]

    atr_stop = price - (1.5 * atr)
    structure_stop = snapshot["previous_low10"] * 0.995

    stop_loss = max(atr_stop, structure_stop)
    stop_loss = min(stop_loss, price * 0.985)
    stop_loss = max(stop_loss, price * 0.92)

    risk_per_share = max(price - stop_loss, price * 0.015)
    target_price = price + (2 * risk_per_share)

    return {
        "entry_price": round(price, 2),
        "stop_loss": round(stop_loss, 2),
        "target_price": round(target_price, 2),
        "risk_per_share": risk_per_share,
    }


def analyze_swing_symbol(symbol, frame, market_regime):
    snapshot = calculate_snapshot(frame)

    if snapshot is None:
        return None

    price = snapshot["price"]
    score = 0
    reasons = []

    if price > snapshot["ema50"]:
        score += 20
        reasons.append("ราคาอยู่เหนือ EMA50")
    else:
        reasons.append("ราคายังต่ำกว่า EMA50")

    if snapshot["ema20"] > snapshot["ema50"]:
        score += 15
        reasons.append("EMA20 อยู่เหนือ EMA50")

    if snapshot["macd_hist"] > 0:
        score += 15
        reasons.append("MACD Histogram เป็นบวก")

    if 45 <= snapshot["rsi"] <= 68:
        score += 15
        reasons.append("RSI อยู่ในโซนโมเมนตัมเหมาะสม")
    elif 35 <= snapshot["rsi"] < 75:
        score += 5

    if price > snapshot["previous_high20"]:
        score += 15
        reasons.append("ราคาผ่านจุดสูงสุด 20 วันก่อนหน้า")

    if snapshot["volume_ratio"] >= 1.5:
        score += 15
        reasons.append("Volume สูงกว่าค่าเฉลี่ยอย่างน้อย 1.5 เท่า")
    elif snapshot["volume_ratio"] >= 1.0:
        score += 5

    if snapshot["average_traded_value"] >= MIN_AVG_VALUE:
        score += 10
    else:
        reasons.append("มูลค่าซื้อขายเฉลี่ยต่ำกว่าเกณฑ์")

    if market_regime["is_positive"] is False:
        score -= 15
        reasons.append("หักคะแนนเนื่องจากตลาดรวมอ่อนแอ")

    rejected = False

    if snapshot["average_traded_value"] < MIN_AVG_VALUE:
        rejected = True

    if snapshot["rsi"] >= 78:
        rejected = True
        reasons.append("RSI สูงเกินไป เสี่ยงไล่ราคา")

    if (
        price < snapshot["ema50"]
        and snapshot["macd_hist"] < 0
    ):
        rejected = True
        reasons.append("ราคาและ MACD อยู่ในโครงสร้างขาลง")

    score = max(0, min(int(score), 100))

    if rejected:
        action = "REJECT"
    elif score >= 70:
        action = "BUY"
    elif score >= 55:
        action = "WATCH"
    else:
        action = "HOLD"

    levels = create_trade_levels(snapshot)

    return {
        "symbol": symbol,
        "sector": SECTOR_MAP.get(symbol, "ไม่ระบุ"),
        "score": score,
        "action": action,
        "reasons": reasons,
        **snapshot,
        **levels,
    }


def analyze_breakout_symbol(symbol, frame, market_regime):
    snapshot = calculate_snapshot(frame)

    if snapshot is None:
        return None

    price = snapshot["price"]

    breakout = (
        price > snapshot["previous_high20"] * 1.002
        and snapshot["volume_ratio"] >= 1.5
        and snapshot["ema20"] > snapshot["ema50"]
        and price > snapshot["ema50"]
        and snapshot["average_traded_value"] >= MIN_AVG_VALUE
    )

    distance_to_breakout = (
        (snapshot["previous_high20"] - price)
        / snapshot["previous_high20"]
        * 100
        if snapshot["previous_high20"] > 0
        else 0
    )

    score = 0

    if price > snapshot["previous_high20"]:
        score += 30

    if snapshot["volume_ratio"] >= 2:
        score += 25
    elif snapshot["volume_ratio"] >= 1.5:
        score += 15

    if snapshot["ema20"] > snapshot["ema50"]:
        score += 15

    if snapshot["macd_hist"] > 0:
        score += 10

    if 50 <= snapshot["rsi"] <= 72:
        score += 10

    if market_regime["is_positive"] is True:
        score += 10
    elif market_regime["is_positive"] is False:
        score -= 15

    levels = create_trade_levels(snapshot)

    return {
        "symbol": symbol,
        "sector": SECTOR_MAP.get(symbol, "ไม่ระบุ"),
        "is_breakout": breakout,
        "score": max(0, min(score, 100)),
        "distance_to_breakout": distance_to_breakout,
        **snapshot,
        **levels,
    }


# =========================================================
# PORTFOLIO CALCULATIONS
# =========================================================

def estimate_portfolio_equity(portfolio, frames):
    cash = safe_float(portfolio.get("cash"), 0)
    stock_value = 0.0

    for symbol, info in portfolio.get("stocks", {}).items():
        frame = frames.get(symbol)
        price = safe_float(info.get("average_cost"), 0)

        if frame is not None and not frame.empty:
            price = safe_float(frame["Close"].dropna().iloc[-1], price)

        stock_value += int(info.get("shares", 0)) * price

    return cash + stock_value, stock_value


def calculate_position_size(portfolio, equity, entry_price, stop_loss):
    if entry_price <= 0 or stop_loss >= entry_price:
        return 0

    cash = safe_float(portfolio.get("cash"), 0)
    plan = portfolio.get("financial_plan", {})

    risk_pct = min(
        max(safe_float(plan.get("risk_per_trade_pct"), 0.01), 0.0025),
        0.02,
    )

    max_weight_pct = min(
        max(safe_float(plan.get("max_stock_weight_pct"), 0.25), 0.05),
        0.40,
    )

    risk_budget = equity * risk_pct
    risk_per_share = entry_price - stop_loss

    shares_by_risk = int(risk_budget / risk_per_share)
    shares_by_weight = int(
        (equity * max_weight_pct) / entry_price
    )
    shares_by_cash = int(
        (cash * 0.95) / entry_price
    )

    quantity = max(
        0,
        min(
            shares_by_risk,
            shares_by_weight,
            shares_by_cash,
        ),
    )

    quantity = (quantity // BOARD_LOT) * BOARD_LOT
    return quantity


def portfolio_risk_status(portfolio, equity):
    plan = portfolio.get("financial_plan", {})
    high_water_mark = max(
        safe_float(portfolio.get("high_water_mark"), equity),
        equity,
    )

    max_drawdown = safe_float(
        plan.get("max_portfolio_drawdown_pct"),
        0.20,
    )

    stop_value = safe_float(
        plan.get("trading_paused_at_value"),
        8000,
    )

    drawdown = (
        (high_water_mark - equity) / high_water_mark
        if high_water_mark > 0
        else 0
    )

    paused = (
        equity <= stop_value
        or drawdown >= max_drawdown
    )

    return {
        "paused": paused,
        "drawdown": drawdown,
        "high_water_mark": high_water_mark,
    }


# =========================================================
# REPORT GENERATION
# =========================================================

def combined_universe(portfolio):
    return unique_symbols(
        portfolio.get("watchlist", [])
        + list(portfolio.get("stocks", {}).keys())
    )


def build_swing_report(portfolio, symbols, title, limit=10):
    symbols = unique_symbols(symbols)

    frames = get_market_frames(
        symbols + [MARKET_INDEX_SYMBOL]
    )

    market_regime = get_market_regime(frames)
    equity, _ = estimate_portfolio_equity(portfolio, frames)
    risk_status = portfolio_risk_status(portfolio, equity)

    results = []

    for symbol in symbols:
        result = analyze_swing_symbol(
            symbol,
            frames.get(symbol),
            market_regime,
        )

        if result and result["action"] in {"BUY", "WATCH"}:
            quantity = calculate_position_size(
                portfolio,
                equity,
                result["entry_price"],
                result["stop_loss"],
            )
            result["quantity"] = quantity
            results.append(result)

    results.sort(
        key=lambda item: (
            item["score"],
            item["volume_ratio"],
        ),
        reverse=True,
    )

    results = results[:limit]

    lines = [
        title,
        f"ตลาดรวม: {market_regime['message']}",
        f"มูลค่าพอร์ตโดยประมาณ: {equity:,.2f} บาท",
    ]

    if risk_status["paused"]:
        lines.append(
            "คำเตือน: พอร์ตถึงเงื่อนไขหยุดเพิ่มความเสี่ยง "
            "ระบบจะแสดงผลเพื่อเฝ้าดูเท่านั้น"
        )

    if not results:
        lines.append("")
        lines.append("ยังไม่มีหุ้นผ่านเกณฑ์ BUY/WATCH")
        lines.append(
            "การไม่พบสัญญาณถือเป็นผลลัพธ์ปกติ ไม่ควรฝืนเปิดสถานะ"
        )
        return "\n".join(lines)

    for index, result in enumerate(results, 1):
        quantity = (
            0 if risk_status["paused"] else result["quantity"]
        )
        estimated_cost = quantity * result["entry_price"]

        lines.extend(
            [
                "",
                (
                    f"{index}. {display_symbol(result['symbol'])} "
                    f"[{result['sector']}]"
                ),
                (
                    f"สถานะ: {result['action']} | "
                    f"คะแนน: {result['score']}/100"
                ),
                (
                    f"ราคา: {result['price']:.2f} | "
                    f"RSI: {result['rsi']:.1f} | "
                    f"Volume: {result['volume_ratio']:.2f}x"
                ),
                (
                    f"เข้า: {result['entry_price']:.2f} | "
                    f"Stop: {result['stop_loss']:.2f} | "
                    f"เป้า 2R: {result['target_price']:.2f}"
                ),
                (
                    f"ขนาดตามความเสี่ยง: {quantity} หุ้น "
                    f"(ประมาณ {estimated_cost:,.2f} บาท)"
                ),
            ]
        )

    lines.extend(
        [
            "",
            "หมายเหตุ: เป็นสัญญาณเชิงระบบ ไม่รับประกันผลตอบแทน",
            f"ข้อมูลล่าสุดในชุดราคา: {results[0]['data_date']}",
        ]
    )

    return "\n".join(lines)


def build_breakout_report(portfolio):
    symbols = combined_universe(portfolio)

    frames = get_market_frames(
        symbols + [MARKET_INDEX_SYMBOL]
    )

    market_regime = get_market_regime(frames)
    equity, _ = estimate_portfolio_equity(portfolio, frames)
    risk_status = portfolio_risk_status(portfolio, equity)

    breakouts = []
    near_breakouts = []

    for symbol in symbols:
        result = analyze_breakout_symbol(
            symbol,
            frames.get(symbol),
            market_regime,
        )

        if not result:
            continue

        result["quantity"] = calculate_position_size(
            portfolio,
            equity,
            result["entry_price"],
            result["stop_loss"],
        )

        if result["is_breakout"]:
            breakouts.append(result)
        elif 0 <= result["distance_to_breakout"] <= 2:
            near_breakouts.append(result)

    breakouts.sort(
        key=lambda item: (
            item["score"],
            item["volume_ratio"],
        ),
        reverse=True,
    )

    near_breakouts.sort(
        key=lambda item: item["distance_to_breakout"]
    )

    lines = [
        "ผลสแกนหุ้นซิ่ง: Momentum Breakout",
        f"ตลาดรวม: {market_regime['message']}",
    ]

    if risk_status["paused"]:
        lines.append(
            "พอร์ตอยู่ในโหมดหยุดเพิ่มความเสี่ยง "
            "จึงไม่แนะนำจำนวนซื้อ"
        )

    if breakouts:
        lines.append("")
        lines.append("หุ้นที่เกิด Breakout")

        for index, result in enumerate(breakouts[:10], 1):
            quantity = (
                0 if risk_status["paused"] else result["quantity"]
            )

            lines.extend(
                [
                    "",
                    (
                        f"{index}. {display_symbol(result['symbol'])} "
                        f"[{result['sector']}]"
                    ),
                    (
                        f"ราคา {result['price']:.2f} ผ่านแนวต้าน "
                        f"{result['previous_high20']:.2f}"
                    ),
                    (
                        f"Volume {result['volume_ratio']:.2f}x | "
                        f"คะแนน {result['score']}/100"
                    ),
                    (
                        f"Stop {result['stop_loss']:.2f} | "
                        f"เป้า 2R {result['target_price']:.2f}"
                    ),
                    f"ขนาดตามความเสี่ยง: {quantity} หุ้น",
                ]
            )
    else:
        lines.extend(
            [
                "",
                "วันนี้ยังไม่มีหุ้นเข้าเงื่อนไข Breakout ครบทุกข้อ",
            ]
        )

    if near_breakouts:
        lines.append("")
        lines.append("หุ้นใกล้แนวต้านภายใน 2% เฝ้าดูได้ แต่ยังไม่ใช่สัญญาณซื้อ")

        for result in near_breakouts[:5]:
            lines.append(
                f"- {display_symbol(result['symbol'])}: "
                f"ห่างแนวต้าน {result['distance_to_breakout']:.2f}% | "
                f"Volume {result['volume_ratio']:.2f}x"
            )

    lines.extend(
        [
            "",
            "หลีกเลี่ยงการไล่ซื้อหากราคาเปิดกระโดดเหนือจุดเข้าอย่างมาก",
            "สัญญาณนี้ใช้ข้อมูลจาก Yahoo Finance ซึ่งอาจล่าช้า",
        ]
    )

    return "\n".join(lines)


def build_single_stock_report(portfolio, symbol):
    formatted_symbol = format_symbol(symbol)

    frames = get_market_frames(
        [formatted_symbol, MARKET_INDEX_SYMBOL]
    )

    market_regime = get_market_regime(frames)
    result = analyze_swing_symbol(
        formatted_symbol,
        frames.get(formatted_symbol),
        market_regime,
    )

    if not result:
        return (
            f"ไม่สามารถวิเคราะห์ {display_symbol(formatted_symbol)} ได้ "
            "อาจไม่มีข้อมูลหรือชื่อหลักทรัพย์ไม่ถูกต้อง"
        )

    equity, _ = estimate_portfolio_equity(portfolio, frames)

    quantity = calculate_position_size(
        portfolio,
        equity,
        result["entry_price"],
        result["stop_loss"],
    )

    reason_text = "\n".join(
        f"- {reason}"
        for reason in result["reasons"]
    )

    # ตรวจสอบสถานะในพอร์ตของผู้ใช้เพื่อสรุปคำแนะนำการจัดการ
    holding_info = portfolio.get("stocks", {}).get(formatted_symbol)
    conclusion_lines = ["\n💡 สรุปคำแนะนำการลงทุนรายตัว:"]

    if holding_info:
        avg_cost = safe_float(holding_info.get("average_cost"), 0)
        pnl_pct = ((result['price'] - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0
        
        if result['action'] == "REJECT" or result['price'] <= result['stop_loss']:
            conclusion_lines.append(
                f"• **คำแนะนำสำหรับคนมีหุ้นในพอร์ต:** 🔴 **ควรขายออก (SELL)**\n"
                f"  - *เหตุผล:* ราคาปัจจุบันต่ำกว่าหรือแตะจุด Stop Loss ({result['stop_loss']:.2f} บาท) หรือสภาพเทคนิคเริ่มอ่อนแอ (P/L ปัจจุบัน {pnl_pct:+.2f}%) ควรขายล็อกกำไรหรือตัดขาดทุนเพื่อบริหารความเสี่ยง"
            )
        else:
            conclusion_lines.append(
                f"• **คำแนะนำสำหรับคนมีหุ้นในพอร์ต:** 🟢 **ควรถือต่อ (HOLD/TRAIL)**\n"
                f"  - *เหตุผล:* ต้นทุนเฉลี่ยอยู่ที่ {avg_cost:.2f} บาท (P/L {pnl_pct:+.2f}%) โครงสร้างราคายังแข็งแกร่งยืนเหนือเส้นค่าเฉลี่ย ควรถือรันเทรนด์ไปเป้าหมายทำกำไรที่ {result['target_price']:.2f} บาท หรือถือลาก Stop Loss ตามระบบที่ {result['stop_loss']:.2f} บาท"
            )
    else:
        if result['action'] == "BUY":
            conclusion_lines.append(
                f"• **คำแนะนำสำหรับคนยังไม่มีหุ้น:** 🟢 **ควรพิจารณาเข้าซื้อ (BUY)**\n"
                f"  - *เหตุผล:* หุ้นผ่านเกณฑ์ความปลอดภัย คะแนนสูง ({result['score']}/100) และมี Volume หนาแน่น เหมาะสำหรับสะสมที่โซนราคา {result['entry_price']:.2f} บาท โดยมีจุดตัดขาดทุน (Stop Loss) ที่ {result['stop_loss']:.2f} บาท"
            )
        elif result['action'] == "WATCH":
            conclusion_lines.append(
                f"• **คำแนะนำสำหรับคนยังไม่มีหุ้น:** 🟡 **ควรเฝ้าระวัง (WATCH)**\n"
                f"  - *เหตุผล:* สัญญาณยังอยู่ในเกณฑ์ใช้ได้แต่ยังไม่สมบูรณ์ ควรออรอดูกลุ่มราคาพักตัวใกล้แนวรับหรือรอจังหวะ Volume เข้าเพิ่มจึงค่อยตัดสินใจ"
            )
        else:
            conclusion_lines.append(
                f"• **คำแนะนำสำหรับคนยังไม่มีหุ้น:** 🔴 **ไม่แนะนำให้เข้าซื้อ (AVOID)**\n"
                f"  - *เหตุผล:* หุ้นไม่ผ่านเกณฑ์ความปลอดภัยหรือโครงสร้างราคาอยู่ในขาลง หลีกเลี่ยงการเข้าลงทุนเพื่อความปลอดภัยของเงินทุน"
            )

    conclusion_text = "\n".join(conclusion_lines)

    return (
        f"วิเคราะห์ {display_symbol(formatted_symbol)} [{result['sector']}]\n"
        f"ตลาดรวม: {market_regime['message']}\n"
        f"สถานะ: {result['action']} | คะแนน {result['score']}/100\n"
        f"ราคา: {result['price']:.2f} บาท\n"
        f"EMA20: {result['ema20']:.2f} | EMA50: {result['ema50']:.2f}\n"
        f"RSI: {result['rsi']:.1f} | MACD Hist: {result['macd_hist']:.3f}\n"
        f"Volume Ratio: {result['volume_ratio']:.2f}x\n"
        f"แนวต้าน 20 วัน: {result['previous_high20']:.2f}\n"
        f"จุดเข้า: {result['entry_price']:.2f}\n"
        f"Stop Loss: {result['stop_loss']:.2f}\n"
        f"เป้าหมาย 2R: {result['target_price']:.2f}\n"
        f"จำนวนตามความเสี่ยง: {quantity} หุ้น\n"
        f"เหตุผล:\n{reason_text}\n"
        f"{conclusion_text}\n\n"
        "ผลวิเคราะห์ไม่ใช่การรับประกันผลตอบแทน"
    )


def build_portfolio_report(user_id, update_trailing=False):
    portfolio = get_user_portfolio(user_id)
    stocks = portfolio.get("stocks", {})
    symbols = list(stocks.keys())

    frames = get_market_frames(symbols)
    equity, stock_value = estimate_portfolio_equity(
        portfolio,
        frames,
    )

    cash = safe_float(portfolio.get("cash"), 0)
    plan = portfolio.get("financial_plan", {})
    target = safe_float(plan.get("target_value"), 20000)

    portfolio["high_water_mark"] = max(
        safe_float(portfolio.get("high_water_mark"), equity),
        equity,
    )

    risk_status = portfolio_risk_status(portfolio, equity)

    lines = [
        "สถานะพอร์ต",
        f"เงินสด: {cash:,.2f} บาท",
        f"มูลค่าหุ้น: {stock_value:,.2f} บาท",
        f"มูลค่ารวม: {equity:,.2f} บาท",
        f"เป้าหมาย: {target:,.2f} บาท",
        f"ความคืบหน้า: {(equity / target * 100) if target else 0:.2f}%",
        f"Drawdown: {risk_status['drawdown'] * 100:.2f}%",
        (
            "สถานะความเสี่ยง: หยุดเพิ่มสถานะ"
            if risk_status["paused"]
            else "สถานะความเสี่ยง: ปกติ"
        ),
    ]

    if not stocks:
        lines.extend(["", "ยังไม่มีหุ้นในพอร์ต"])
        update_user_portfolio(user_id, portfolio)
        return "\n".join(lines)

    lines.append("")
    lines.append("รายการหุ้น")

    changed = False

    for symbol, info in stocks.items():
        shares = int(info.get("shares", 0))
        average_cost = safe_float(info.get("average_cost"), 0)
        frame = frames.get(symbol)
        price = average_cost
        atr = price * 0.03

        if frame is not None:
            snapshot = calculate_snapshot(frame)

            if snapshot:
                price = snapshot["price"]
                atr = snapshot["atr"]

        market_value = shares * price
        pnl = market_value - safe_float(info.get("total_cost"), 0)
        pnl_pct = (
            pnl / safe_float(info.get("total_cost"), 1) * 100
            if safe_float(info.get("total_cost"), 0) > 0
            else 0
        )

        highest_price = max(
            safe_float(info.get("highest_price"), average_cost),
            price,
        )

        stop_loss = safe_float(
            info.get("stop_loss"),
            average_cost * 0.95,
        )

        if update_trailing and highest_price >= average_cost * 1.05:
            trailing_stop = max(
                average_cost,
                highest_price - (2 * atr),
            )

            if trailing_stop > stop_loss:
                stop_loss = round(trailing_stop, 2)
                info["stop_loss"] = stop_loss
                changed = True

        if highest_price != safe_float(
            info.get("highest_price"),
            average_cost,
        ):
            info["highest_price"] = highest_price
            changed = True

        status = (
            "ถึงจุด Stop"
            if price <= stop_loss
            else "ถือตามแผน"
        )

        lines.extend(
            [
                (
                    f"- {display_symbol(symbol)}: {shares} หุ้น | "
                    f"เฉลี่ย {average_cost:.2f}"
                ),
                (
                    f"  ล่าสุด {price:.2f} | "
                    f"P/L {pnl:+,.2f} บาท ({pnl_pct:+.2f}%)"
                ),
                f"  Stop {stop_loss:.2f} | {status}",
            ]
        )

    if changed or update_trailing:
        update_user_portfolio(user_id, portfolio)
    else:
        update_user_portfolio(user_id, portfolio)

    return "\n".join(lines)


def calculate_trade_statistics(portfolio):
    transactions = portfolio.get("transactions", [])

    sells = [
        transaction
        for transaction in transactions
        if transaction.get("type") == "SELL"
    ]

    if not sells:
        return "ยังไม่มีธุรกรรมขายสำหรับคำนวณสถิติ"

    profits = [
        safe_float(item.get("realized_pnl"), 0)
        for item in sells
    ]

    wins = [value for value in profits if value > 0]
    losses = [value for value in profits if value < 0]
    breakeven = [value for value in profits if value == 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    win_rate = len(wins) / len(profits) * 100
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else float("inf") if gross_profit > 0 else 0
    )

    average_win = (
        gross_profit / len(wins)
        if wins else 0
    )

    average_loss = (
        gross_loss / len(losses)
        if losses else 0
    )

    expectancy = sum(profits) / len(profits)

    profit_factor_text = (
        "∞"
        if math.isinf(profit_factor)
        else f"{profit_factor:.2f}"
    )

    return (
        "สถิติการเทรด\n"
        f"รายการขาย: {len(sells)} ครั้ง\n"
        f"ชนะ: {len(wins)} | แพ้: {len(losses)} | เสมอ: {len(breakeven)}\n"
        f"Win Rate: {win_rate:.2f}%\n"
        f"กำไรรวม: {gross_profit:,.2f} บาท\n"
        f"ขาดทุนรวม: {gross_loss:,.2f} บาท\n"
        f"กำไรเฉลี่ยต่อครั้งที่ชนะ: {average_win:,.2f} บาท\n"
        f"ขาดทุนเฉลี่ยต่อครั้งที่แพ้: {average_loss:,.2f} บาท\n"
        f"Expectancy ต่อรายการขาย: {expectancy:,.2f} บาท\n"
        f"Profit Factor: {profit_factor_text}\n"
        f"ค่าธรรมเนียมสะสม: {safe_float(portfolio.get('fees_total')):,.2f} บาท"
    )


# =========================================================
# BACKGROUND SCAN QUEUE
# =========================================================

executor = ThreadPoolExecutor(max_workers=2)
pending_jobs = set()
pending_jobs_lock = threading.RLock()


def run_scan_job(user_id, job_type, payload=None):
    try:
        portfolio = get_user_portfolio(user_id)

        if job_type == "summary":
            result = build_swing_report(
                portfolio,
                combined_universe(portfolio),
                "สรุปหุ้นวันนี้: Top 5",
                limit=5,
            )
        elif job_type == "scan":
            result = build_swing_report(
                portfolio,
                combined_universe(portfolio),
                "ผลสแกนหุ้น: SET50 + Watchlist + หุ้นในพอร์ต",
                limit=10,
            )
        elif job_type == "set50":
            result = build_swing_report(
                portfolio,
                SET50_UNIVERSE,
                "ผลสแกนจักรวาล SET50 ที่ตั้งค่าไว้",
                limit=10,
            )
        elif job_type == "breakout":
            result = build_breakout_report(portfolio)
        elif job_type == "single":
            result = build_single_stock_report(
                portfolio,
                payload,
            )
        else:
            result = "ไม่รู้จักประเภทการสแกน"

        push_long_message(user_id, result)

    except Exception:
        logger.exception("Background scan failed")

        try:
            push_long_message(
                user_id,
                "ระบบสแกนขัดข้องชั่วคราว กรุณาลองใหม่ภายหลัง",
            )
        except Exception:
            logger.exception("ไม่สามารถแจ้งข้อผิดพลาดกลับ LINE")

    finally:
        job_key = (user_id, job_type, payload or "")

        with pending_jobs_lock:
            pending_jobs.discard(job_key)


def queue_scan(user_id, job_type, payload=None):
    job_key = (user_id, job_type, payload or "")

    with pending_jobs_lock:
        if job_key in pending_jobs:
            return False

        pending_jobs.add(job_key)

    executor.submit(
        run_scan_job,
        user_id,
        job_type,
        payload,
    )

    return True


# =========================================================
# TRANSACTION COMMANDS
# =========================================================

def process_buy(portfolio, symbol, shares, price, fee):
    symbol = format_symbol(symbol)
    shares = positive_int(shares, "จำนวนหุ้น")
    price = positive_float(price, "ราคา")
    fee = non_negative_float(fee, "ค่าธรรมเนียม")

    total_cost = (shares * price) + fee
    cash = safe_float(portfolio.get("cash"), 0)

    if total_cost > cash:
        raise ValueError(
            f"เงินสดไม่พอ ต้องใช้ {total_cost:,.2f} บาท "
            f"แต่มี {cash:,.2f} บาท"
        )

    stocks = portfolio.setdefault("stocks", {})
    old_info = stocks.get(
        symbol,
        {
            "shares": 0,
            "total_cost": 0.0,
            "average_cost": 0.0,
            "stop_loss": 0.0,
            "highest_price": price,
        },
    )

    old_shares = int(old_info.get("shares", 0))
    old_total_cost = safe_float(old_info.get("total_cost"), 0)

    new_shares = old_shares + shares
    new_total_cost = old_total_cost + total_cost
    new_average_cost = new_total_cost / new_shares

    stocks[symbol] = {
        "shares": new_shares,
        "total_cost": new_total_cost,
        "average_cost": new_average_cost,
        "stop_loss": round(new_average_cost * 0.95, 2),
        "highest_price": max(
            safe_float(old_info.get("highest_price"), price),
            price,
        ),
    }

    portfolio["cash"] = cash - total_cost
    portfolio["fees_total"] = (
        safe_float(portfolio.get("fees_total"), 0) + fee
    )

    portfolio.setdefault("transactions", []).append(
        {
            "type": "BUY",
            "symbol": symbol,
            "shares": shares,
            "price": price,
            "fee": fee,
            "gross_amount": shares * price,
            "net_amount": total_cost,
            "date": utc_now_text(),
        }
    )

    lot_warning = ""

    if shares % BOARD_LOT != 0:
        lot_warning = (
            f"\nคำเตือน: จำนวนไม่ลงตัวตาม Board Lot "
            f"ที่ตั้งไว้ {BOARD_LOT} หุ้น"
        )

    return (
        f"บันทึกซื้อ {display_symbol(symbol)} สำเร็จ\n"
        f"จำนวน: {shares} หุ้น\n"
        f"ราคา: {price:.2f} บาท\n"
        f"ค่าธรรมเนียม: {fee:.2f} บาท\n"
        f"จ่ายสุทธิ: {total_cost:,.2f} บาท\n"
        f"ต้นทุนเฉลี่ยใหม่: {new_average_cost:.2f} บาท\n"
        f"Stop เริ่มต้น: {stocks[symbol]['stop_loss']:.2f} บาท"
        f"{lot_warning}"
    )


def process_sell(portfolio, symbol, shares, price, fee):
    symbol = format_symbol(symbol)
    shares = positive_int(shares, "จำนวนหุ้น")
    price = positive_float(price, "ราคา")
    fee = non_negative_float(fee, "ค่าธรรมเนียม")

    stocks = portfolio.setdefault("stocks", {})

    if symbol not in stocks:
        raise ValueError(
            f"ไม่พบ {display_symbol(symbol)} ในพอร์ต"
        )

    info = stocks[symbol]
    old_shares = int(info.get("shares", 0))

    if shares > old_shares:
        raise ValueError(
            f"มีหุ้นเพียง {old_shares} หุ้น ไม่พอขาย {shares} หุ้น"
        )

    old_total_cost = safe_float(info.get("total_cost"), 0)
    average_cost = old_total_cost / old_shares

    cost_of_sold = average_cost * shares
    gross_income = price * shares
    net_income = gross_income - fee
    realized_pnl = net_income - cost_of_sold

    portfolio["cash"] = (
        safe_float(portfolio.get("cash"), 0)
        + net_income
    )

    portfolio["realized_pnl_total"] = (
        safe_float(portfolio.get("realized_pnl_total"), 0)
        + realized_pnl
    )

    portfolio["fees_total"] = (
        safe_float(portfolio.get("fees_total"), 0)
        + fee
    )

    remaining_shares = old_shares - shares

    if remaining_shares == 0:
        del stocks[symbol]
    else:
        remaining_cost = old_total_cost - cost_of_sold
        info["shares"] = remaining_shares
        info["total_cost"] = remaining_cost
        info["average_cost"] = (
            remaining_cost / remaining_shares
        )

    portfolio.setdefault("transactions", []).append(
        {
            "type": "SELL",
            "symbol": symbol,
            "shares": shares,
            "price": price,
            "fee": fee,
            "gross_amount": gross_income,
            "net_amount": net_income,
            "realized_pnl": realized_pnl,
            "date": utc_now_text(),
        }
    )

    pnl_pct = (
        realized_pnl / cost_of_sold * 100
        if cost_of_sold > 0
        else 0
    )

    return (
        f"บันทึกขาย {display_symbol(symbol)} สำเร็จ\n"
        f"จำนวน: {shares} หุ้น\n"
        f"ราคาขาย: {price:.2f} บาท\n"
        f"รับสุทธิ: {net_income:,.2f} บาท\n"
        f"กำไร/ขาดทุน: {realized_pnl:+,.2f} บาท "
        f"({pnl_pct:+.2f}%)"
    )


# =========================================================
# SCHEDULER
# =========================================================

def scheduled_morning_summary():
    logger.info("เริ่มงานสรุปหุ้นประจำวัน")

    for user_id in get_registered_users():
        queue_scan(user_id, "summary")


scheduler = None

if ENABLE_SCHEDULER:
    scheduler = BackgroundScheduler(
        timezone=TIMEZONE_NAME
    )

    scheduler.add_job(
        scheduled_morning_summary,
        CronTrigger(
            day_of_week="mon-fri",
            hour=8,
            minute=30,
            timezone=TIMEZONE_NAME,
        ),
        id="morning-summary",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info(
        "เปิด Scheduler เวลา 08:30 เขตเวลา %s",
        TIMEZONE_NAME,
    )


# =========================================================
# FLASK AND LINE WEBHOOK
# =========================================================

@app.route("/", methods=["GET", "HEAD"])
@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify(
        {
            "status": "ok",
            "service": "Thai Stock Portfolio Bot",
            "schema_version": SCHEMA_VERSION,
            "configured_universe_size": len(SET50_UNIVERSE),
            "scheduler_enabled": ENABLE_SCHEDULER,
        }
    )


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get(
        "X-Line-Signature",
        "",
    )

    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        logger.exception("LINE callback error")
        return "ERROR", 500

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = getattr(event.source, "user_id", None)

    if not user_id:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="คำสั่งนี้ต้องใช้งานจากบัญชีที่มี user_id"
            ),
        )
        return

    portfolio = get_user_portfolio(user_id)
    parts = text.split()
    reply_text = ""

    try:
        if text == "ดูพอร์ต":
            reply_text = build_portfolio_report(
                user_id,
                update_trailing=False,
            )

        elif text == "วิเคราะห์พอร์ต":
            reply_text = build_portfolio_report(
                user_id,
                update_trailing=True,
            )

        elif text == "สรุปหุ้นวันนี้":
            accepted = queue_scan(user_id, "summary")
            reply_text = (
                "กำลังจัดทำสรุปหุ้นวันนี้ ระบบจะส่งผลกลับมาเมื่อเสร็จ"
                if accepted
                else "ระบบกำลังประมวลผลคำสั่งนี้อยู่แล้ว"
            )

        elif text == "สแกนหุ้น":
            accepted = queue_scan(user_id, "scan")
            reply_text = (
                "กำลังสแกน SET50, Watchlist และหุ้นในพอร์ต"
                if accepted
                else "ระบบกำลังสแกนหุ้นให้คุณอยู่แล้ว"
            )

        elif text == "สแกน SET50":
            accepted = queue_scan(user_id, "set50")
            reply_text = (
                "กำลังสแกนจักรวาล SET50 ที่กำหนดไว้"
                if accepted
                else "ระบบกำลังสแกน SET50 อยู่แล้ว"
            )

        elif text == "สแกนหุ้นซิ่ง":
            accepted = queue_scan(user_id, "breakout")
            reply_text = (
                "กำลังค้นหา Momentum Breakout "
                "ระบบจะส่งผลกลับมาเมื่อเสร็จ"
                if accepted
                else "ระบบกำลังสแกนหุ้นซิ่งอยู่แล้ว"
            )

        elif text.startswith("สแกน ") or (text.startswith("วิเคราะห์ ") and text != "วิเคราะห์พอร์ต"):
            if len(parts) < 2:
                raise ValueError(
                    "ตัวอย่างคำสั่ง: สแกน PTT หรือ วิเคราะห์ PTT"
                )

            symbol = format_symbol(parts[1])
            accepted = queue_scan(
                user_id,
                "single",
                symbol,
            )

            reply_text = (
                f"กำลังวิเคราะห์ {display_symbol(symbol)}"
                if accepted
                else f"กำลังวิเคราะห์ {display_symbol(symbol)} อยู่แล้ว"
            )

        elif text == "สถิติการเทรด":
            reply_text = calculate_trade_statistics(portfolio)

        elif text == "รายการติดตาม":
            watchlist = portfolio.get("watchlist", [])

            reply_text = (
                f"รายการติดตามทั้งหมด {len(watchlist)} หุ้น\n"
                + ", ".join(
                    display_symbol(symbol)
                    for symbol in watchlist
                )
            )

        elif text.startswith("ติดตาม "):
            if len(parts) < 2:
                raise ValueError(
                    "ตัวอย่างคำสั่ง: ติดตาม PTT"
                )

            symbol = format_symbol(parts[1])
            watchlist = portfolio.setdefault("watchlist", [])

            if symbol not in watchlist:
                watchlist.append(symbol)
                portfolio["watchlist"] = unique_symbols(watchlist)
                update_user_portfolio(user_id, portfolio)
                reply_text = (
                    f"เพิ่ม {display_symbol(symbol)} "
                    "ในรายการติดตามแล้ว"
                )
            else:
                reply_text = (
                    f"{display_symbol(symbol)} "
                    "อยู่ในรายการติดตามแล้ว"
                )

        elif text.startswith("เลิกติดตาม "):
            if len(parts) < 2:
                raise ValueError(
                    "ตัวอย่างคำสั่ง: เลิกติดตาม PTT"
                )

            symbol = format_symbol(parts[1])
            watchlist = portfolio.setdefault("watchlist", [])

            if symbol in watchlist:
                watchlist.remove(symbol)
                update_user_portfolio(user_id, portfolio)
                reply_text = (
                    f"ลบ {display_symbol(symbol)} "
                    "ออกจากรายการติดตามแล้ว"
                )
            else:
                reply_text = (
                    f"ไม่พบ {display_symbol(symbol)} "
                    "ในรายการติดตาม"
                )

        elif text.startswith("ซื้อ "):
            if len(parts) < 4:
                raise ValueError(
                    "รูปแบบ: ซื้อ PTT 100 35.50 10"
                )

            fee = parts[4] if len(parts) >= 5 else 0

            reply_text = process_buy(
                portfolio,
                parts[1],
                parts[2],
                parts[3],
                fee,
            )

            update_user_portfolio(user_id, portfolio)

        elif text.startswith("ขาย "):
            if len(parts) < 4:
                raise ValueError(
                    "รูปแบบ: ขาย PTT 100 38.00 10"
                )

            fee = parts[4] if len(parts) >= 5 else 0

            reply_text = process_sell(
                portfolio,
                parts[1],
                parts[2],
                parts[3],
                fee,
            )

            update_user_portfolio(user_id, portfolio)

        elif text.startswith("ฝากเงิน "):
            if len(parts) < 2:
                raise ValueError(
                    "รูปแบบ: ฝากเงิน 1000"
                )

            amount = positive_float(
                parts[1],
                "จำนวนเงิน",
            )

            portfolio["cash"] = (
                safe_float(portfolio.get("cash"), 0)
                + amount
            )

            portfolio["contributions_total"] = (
                safe_float(
                    portfolio.get("contributions_total"),
                    0,
                )
                + amount
            )

            portfolio.setdefault("transactions", []).append(
                {
                    "type": "DEPOSIT",
                    "amount": amount,
                    "date": utc_now_text(),
                }
            )

            update_user_portfolio(user_id, portfolio)

            reply_text = (
                f"บันทึกเงินเพิ่ม {amount:,.2f} บาทแล้ว\n"
                f"เงินสดคงเหลือ "
                f"{portfolio['cash']:,.2f} บาท"
            )

        elif text.startswith("ตั้งเป้าหมาย "):
            if len(parts) < 3:
                raise ValueError(
                    "รูปแบบ: ตั้งเป้าหมาย 20000 3"
                )

            target = positive_float(
                parts[1],
                "เป้าหมาย",
            )

            months = positive_int(
                parts[2],
                "จำนวนเดือน",
            )

            portfolio["financial_plan"]["target_value"] = target
            portfolio["financial_plan"]["target_months"] = months

            update_user_portfolio(user_id, portfolio)

            reply_text = (
                f"ตั้งเป้าหมาย {target:,.2f} บาท "
                f"ภายใน {months} เดือนแล้ว\n"
                "เป้าหมายเป็นตัวติดตามผล ไม่ใช่การรับประกันผลตอบแทน"
            )

        elif text in {"ช่วยเหลือ", "help", "คำสั่ง"}:
            reply_text = (
                "คำสั่งของบอต\n"
                "1. ดูพอร์ต\n"
                "2. วิเคราะห์พอร์ต\n"
                "3. สรุปหุ้นวันนี้\n"
                "4. สแกนหุ้น\n"
                "5. สแกน SET50\n"
                "6. สแกนหุ้นซิ่ง\n"
                "7. วิเคราะห์ PTT (หรือ สแกน PTT)\n"
                "8. รายการติดตาม\n"
                "9. ติดตาม PTT\n"
                "10. เลิกติดตาม PTT\n"
                "11. ซื้อ PTT 100 35.50 10\n"
                "12. ขาย PTT 100 38.00 10\n"
                "13. ฝากเงิน 1000\n"
                "14. ตั้งเป้าหมาย 20000 3\n"
                "15. สถิติการเทรด\n\n"
                "การสแกนหุ้นใช้ข้อมูลเชิงเทคนิค "
                "ไม่ได้รับประกันกำไรหรือดำเนินการซื้อขายจริง"
            )

        else:
            reply_text = (
                "ไม่รู้จักคำสั่งนี้\n"
                "พิมพ์ ช่วยเหลือ เพื่อดูรายการคำสั่ง"
            )

    except ValueError as error:
        reply_text = f"ข้อมูลไม่ถูกต้อง: {error}"
    except Exception:
        logger.exception("Command processing failed")
        reply_text = (
            "ระบบขัดข้องชั่วคราว กรุณาลองใหม่อีกครั้ง"
        )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text=reply_text[:MAX_LINE_TEXT]
        ),
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
