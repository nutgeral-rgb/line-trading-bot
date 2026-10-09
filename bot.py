import copy
import json
import logging
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    logger.warning("ยังไม่ได้ตั้งค่า LINE_CHANNEL_ACCESS_TOKEN หรือ LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

PORTFOLIO_FILE = os.getenv("PORTFOLIO_FILE", "portfolio.json")
MARKET_INDEX_SYMBOL = os.getenv("MARKET_INDEX_SYMBOL", "^SET.BK")
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "900"))
BOARD_LOT = max(1, int(os.getenv("BOARD_LOT", "100")))
ALLOW_ODD_LOT = os.getenv("ALLOW_ODD_LOT", "true").lower() == "true"
MIN_AVG_VALUE = float(os.getenv("MIN_AVG_VALUE", "10000000"))
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
TIMEZONE_NAME = os.getenv("TZ", "Asia/Bangkok")

COMMISSION_RATE = float(os.getenv("COMMISSION_RATE", "0.00157"))
VAT_RATE = float(os.getenv("VAT_RATE", "0.07"))
MIN_COMMISSION = float(os.getenv("MIN_COMMISSION", "0"))

MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "1.2"))
MAX_ATR_PCT = float(os.getenv("MAX_ATR_PCT", "12.0"))
MAX_EXTENSION_PCT = float(os.getenv("MAX_EXTENSION_PCT", "12.0"))
TIME_STOP_DAYS = int(os.getenv("TIME_STOP_DAYS", "15"))

DEFAULT_RISK_MODE = os.getenv("RISK_MODE", "balanced").lower()

MAX_LINE_TEXT = 4500
SCHEMA_VERSION = 6
BANGKOK_TZ = timezone(timedelta(hours=7))

RISK_PROFILES = {
    "conservative": {
        "label": "ระมัดระวัง",
        "risk_per_trade": 0.010,
        "max_positions": 3,
        "max_total_risk": 0.04,
        "max_weight": 0.25,
        "min_score": 72,
    },
    "balanced": {
        "label": "สมดุล",
        "risk_per_trade": 0.020,
        "max_positions": 4,
        "max_total_risk": 0.07,
        "max_weight": 0.35,
        "min_score": 65,
    },
    "aggressive": {
        "label": "เชิงรุก",
        "risk_per_trade": 0.030,
        "max_positions": 5,
        "max_total_risk": 0.12,
        "max_weight": 0.45,
        "min_score": 58,
    },
}

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


def now_local():
    return datetime.now(BANGKOK_TZ)


def utc_now_text():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value, fallback=None):
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return fallback


def calculate_commission(value):
    if value <= 0:
        return 0.0
    commission = value * COMMISSION_RATE
    if commission < MIN_COMMISSION:
        commission = MIN_COMMISSION
    return round(commission * (1 + VAT_RATE), 2)


def round_trip_cost_pct():
    return COMMISSION_RATE * (1 + VAT_RATE) * 2 * 100


def net_profit_after_fee(entry_price, exit_price, quantity):
    if quantity <= 0:
        return 0.0
    buy_fee = calculate_commission(entry_price * quantity)
    sell_fee = calculate_commission(exit_price * quantity)
    return ((exit_price - entry_price) * quantity) - buy_fee - sell_fee


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
        line_bot_api.push_message(user_id, TextSendMessage(text=chunk))


# =========================================================
# PORTFOLIO STORAGE
# =========================================================

storage_lock = threading.RLock()


def default_financial_plan():
    return {
        "target_value": 20000.0,
        "target_months": 3,
        "monthly_contribution": 0.0,
        "max_portfolio_drawdown_pct": 0.20,
        "trading_paused_at_value": 8000.0,
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
        "risk_mode": DEFAULT_RISK_MODE,
        "plan_start_equity": 10000.0,
        "plan_start_date": utc_now_text(),
        "financial_plan": default_financial_plan(),
        "stocks": {},
        "watchlist": SET50_UNIVERSE.copy(),
        "transactions": [],
    }


def looks_like_single_portfolio(data):
    if not isinstance(data, dict):
        return False
    portfolio_keys = {
        "cash", "financial_plan", "stocks",
        "watchlist", "transactions", "initial_capital",
    }
    return bool(portfolio_keys.intersection(data.keys()))


def normalize_holding(info, fallback_price=0.0):
    shares = int(safe_float(info.get("shares"), 0))
    if shares <= 0:
        return None

    average_cost = safe_float(info.get("average_cost"), fallback_price)
    total_cost = safe_float(info.get("total_cost"), average_cost * shares)
    initial_stop = safe_float(info.get("initial_stop"), average_cost * 0.95)
    stop_loss = safe_float(info.get("stop_loss"), initial_stop)
    r_unit = safe_float(info.get("r_unit"), max(average_cost - initial_stop, average_cost * 0.02))

    if r_unit <= 0:
        r_unit = max(average_cost * 0.02, 0.01)

    return {
        "shares": shares,
        "total_cost": max(0.0, total_cost),
        "average_cost": max(0.0, average_cost),
        "initial_stop": max(0.0, initial_stop),
        "stop_loss": max(0.0, stop_loss),
        "r_unit": r_unit,
        "target_price": safe_float(info.get("target_price"), average_cost + (2 * r_unit)),
        "highest_price": max(average_cost, safe_float(info.get("highest_price"), average_cost)),
        "tp1_done": bool(info.get("tp1_done", False)),
        "opened_at": info.get("opened_at") or utc_now_text(),
    }


def normalize_portfolio(raw):
    base = default_portfolio()

    if not isinstance(raw, dict):
        return base

    old_schema = int(safe_float(raw.get("schema_version"), 0))

    for key in [
        "cash", "initial_capital", "contributions_total",
        "withdrawals_total", "realized_pnl_total",
        "fees_total", "high_water_mark", "plan_start_equity",
    ]:
        if key in raw:
            base[key] = safe_float(raw.get(key), base[key])

    mode = str(raw.get("risk_mode", DEFAULT_RISK_MODE)).lower()
    base["risk_mode"] = mode if mode in RISK_PROFILES else "balanced"
    base["plan_start_date"] = raw.get("plan_start_date") or utc_now_text()

    if isinstance(raw.get("financial_plan"), dict):
        for key, value in raw["financial_plan"].items():
            if key in base["financial_plan"]:
                base["financial_plan"][key] = value

    base["financial_plan"]["target_value"] = max(
        safe_float(base["financial_plan"].get("target_value"), 20000.0), 1.0
    )
    base["financial_plan"]["target_months"] = max(
        int(safe_float(base["financial_plan"].get("target_months"), 3)), 1
    )

    if isinstance(raw.get("transactions"), list):
        base["transactions"] = raw["transactions"]

    existing_watchlist = raw.get("watchlist", [])
    if not isinstance(existing_watchlist, list):
        existing_watchlist = []

    if old_schema < 2:
        base["watchlist"] = unique_symbols(SET50_UNIVERSE + existing_watchlist)
    else:
        base["watchlist"] = unique_symbols(existing_watchlist)

    if not base["watchlist"]:
        base["watchlist"] = SET50_UNIVERSE.copy()

    clean_stocks = {}
    raw_stocks = raw.get("stocks", {})

    if isinstance(raw_stocks, dict):
        for symbol, info in raw_stocks.items():
            if not isinstance(info, dict):
                continue
            symbol = format_symbol(symbol)
            holding = normalize_holding(info)
            if symbol and holding:
                clean_stocks[symbol] = holding

    base["stocks"] = clean_stocks
    base["schema_version"] = SCHEMA_VERSION
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
        prefix="portfolio_", suffix=".tmp", dir=directory,
    )

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
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
    return [uid for uid, value in data.items() if isinstance(value, dict)]


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
                frame = downloaded.xs(symbol, axis=1, level=1).copy()
            else:
                return None
        elif total_symbols == 1:
            frame = downloaded.copy()
        else:
            return None

        frame.columns = [str(column).strip().title() for column in frame.columns]
        required = {"Open", "High", "Low", "Close", "Volume"}

        if not required.issubset(frame.columns):
            return None

        frame = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
        frame = frame.dropna(subset=["Close"])

        return None if frame.empty else frame
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
            if not force and item and item["expires_at"] > now:
                if isinstance(item["frame"], pd.DataFrame):
                    results[symbol] = item["frame"].copy()
            else:
                missing.append(symbol)

    if missing:
        try:
            downloaded = yf.download(
                tickers=missing,
                period="2y",
                interval="1d",
                auto_adjust=False,
                group_by="ticker",
                threads=True,
                progress=False,
            )

            for symbol in missing:
                frame = extract_downloaded_frame(downloaded, symbol, len(missing))

                with market_cache_lock:
                    market_cache[symbol] = {
                        "expires_at": now + (CACHE_SECONDS if frame is not None else 60),
                        "frame": frame,
                    }

                if frame is not None:
                    results[symbol] = frame.copy()

        except Exception:
            logger.exception("ดาวน์โหลดข้อมูลตลาดแบบ Batch ไม่สำเร็จ")

    return results


# =========================================================
# ADVANCED TECHNICAL ANALYSIS (5 TECHNIQUES)
# =========================================================

def calculate_rsi(close, period=14):
    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    average_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    relative_strength = average_gain / average_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + relative_strength))

    if rsi.empty or pd.isna(rsi.iloc[-1]):
        return 50.0

    return float(rsi.iloc[-1])


def period_return(close, days):
    if len(close) <= days:
        return 0.0
    past = float(close.iloc[-1 - days])
    if past <= 0:
        return 0.0
    return (float(close.iloc[-1]) / past - 1) * 100


def check_weekly_trend(frame):
    """เทคนิคที่ 2: Multi-Timeframe Trend Filter เช็กกราฟ Weekly ว่ายืนเหนือ EMA 20 สัปดาห์"""
    if frame is None or len(frame) < 120:
        return True
    try:
        weekly = frame.resample('W').agg({
            'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
        }).dropna()
        if len(weekly) < 25:
            return True
        w_close = weekly['Close'].astype(float)
        w_ema20 = w_close.ewm(span=20, adjust=False).mean()
        return float(w_close.iloc[-1]) >= float(w_ema20.iloc[-1])
    except Exception:
        return True


def calculate_bollinger_squeeze(close, window=20):
    """เทคนิคที่ 3: Bollinger Bands Squeeze ตรวจจับการบีบตัวสะสมพลัง"""
    if len(close) < window:
        return False, 0.0
    sma = close.rolling(window).mean()
    std = close.rolling(window).std()
    upper = sma + (2 * std)
    lower = sma - (2 * std)
    bandwidth = (upper - lower) / sma
    recent_bw = bandwidth.tail(60)
    is_squeeze = float(bandwidth.iloc[-1]) <= float(recent_bw.quantile(0.2))
    return is_squeeze, float(bandwidth.iloc[-1])


def check_price_action(data_row):
    """เทคนิคที่ 5: Price Action Trigger ตรวจสอบแรงซื้อปิดตลาดแข็งแกร่ง (อยู่ใน 35% บนของแท่ง)"""
    try:
        high = float(data_row['High'])
        low = float(data_row['Low'])
        close = float(data_row['Close'])
        rng = high - low
        if rng <= 0:
            return False
        close_location = (close - low) / rng
        return close_location >= 0.65
    except Exception:
        return False


def calculate_snapshot(frame):
    if frame is None or len(frame) < 60:
        return None

    data = frame.copy().dropna(subset=["Close"])
    if len(data) < 60:
        return None

    close = data["Close"].astype(float)
    high = data["High"].astype(float)
    low = data["Low"].astype(float)
    volume = data["Volume"].fillna(0).astype(float)

    ema10 = close.ewm(span=10, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    macd_line = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line

    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr_series = true_range.rolling(14).mean()

    previous_20 = data.iloc[:-1].tail(20)
    previous_10 = data.iloc[:-1].tail(10)

    if previous_20.empty or previous_10.empty:
        return None

    price = float(close.iloc[-1])
    atr = float(atr_series.iloc[-1])
    if not math.isfinite(atr) or atr <= 0:
        atr = price * 0.025

    ema20_val = float(ema20.iloc[-1])
    is_squeeze, bw_val = calculate_bollinger_squeeze(close)
    is_strong_close = check_price_action(data.iloc[-1])
    is_weekly_up = check_weekly_trend(data)

    average_volume20 = float(previous_20["Volume"].fillna(0).mean())
    current_volume = float(volume.iloc[-1])
    traded_values = previous_20["Close"] * previous_20["Volume"].fillna(0)

    return {
        "price": price,
        "ema10": float(ema10.iloc[-1]),
        "ema20": ema20_val,
        "ema50": float(ema50.iloc[-1]),
        "rsi": calculate_rsi(close),
        "macd_hist": float(histogram.iloc[-1]),
        "macd_hist_prev": float(histogram.iloc[-2]),
        "atr": atr,
        "atr_pct": (atr / price * 100) if price > 0 else 0.0,
        "previous_high20": float(previous_20["High"].max()),
        "previous_low10": float(previous_10["Low"].min()),
        "volume_ratio": (current_volume / average_volume20) if average_volume20 > 0 else 0.0,
        "average_traded_value": float(traded_values.mean()),
        "ret_5": period_return(close, 5),
        "ret_20": period_return(close, 20),
        "ret_60": period_return(close, 60),
        "extension_pct": ((price - ema20_val) / ema20_val * 100) if ema20_val > 0 else 0.0,
        "is_squeeze": is_squeeze,
        "bandwidth": bw_val,
        "is_strong_close": is_strong_close,
        "is_weekly_up": is_weekly_up,
        "data_date": str(data.index[-1].date()),
    }


def get_market_regime(frames):
    """เทคนิคที่ 4: Market Breadth เช็กสัดส่วนหุ้นที่ยืนเหนือ EMA50 จริงใน Universe แทนการดูแค่ SET Index"""
    index_frame = frames.get(MARKET_INDEX_SYMBOL)
    index_snapshot = calculate_snapshot(index_frame) if index_frame is not None else None

    above_ema50_count = 0
    total_checked = 0

    for sym, fr in frames.items():
        if sym == MARKET_INDEX_SYMBOL:
            continue
        snap = calculate_snapshot(fr)
        if snap:
            total_checked += 1
            if snap["price"] > snap["ema50"]:
                above_ema50_count += 1

    breadth_pct = (above_ema50_count / total_checked * 100) if total_checked > 0 else 50.0

    index_positive = (
        index_snapshot["price"] > index_snapshot["ema50"]
        if index_snapshot else True
    )

    breadth_healthy = breadth_pct >= 50.0
    overall_positive = index_positive and breadth_healthy

    status = "POSITIVE" if overall_positive else "DEFENSIVE"
    message = (
        f"ตลาดรวมแข็งแกร่ง (Breadth: {breadth_pct:.1f}% ของหุ้นยืนเหนือ EMA50)"
        if overall_positive
        else f"ตลาดระมัดระวัง (Breadth อ่อนแอเหลือ {breadth_pct:.1f}% ยืนเหนือ EMA50)"
    )

    return {
        "status": status,
        "is_positive": overall_positive,
        "message": message,
        "breadth_pct": breadth_pct,
        "ret_20": index_snapshot["ret_20"] if index_snapshot else 0.0,
        "ret_60": index_snapshot["ret_60"] if index_snapshot else 0.0,
    }


def compute_sector_rankings(frames):
    """เทคนิคที่ 1: Sector Rotation คำนวณความแข็งแกร่งของแต่ละ Sector"""
    sector_performances = {}
    sector_counts = {}

    for sym, fr in frames.items():
        if sym == MARKET_INDEX_SYMBOL:
            continue
        sec = SECTOR_MAP.get(sym, "ทั่วไป")
        snap = calculate_snapshot(fr)
        if snap:
            ret = snap["ret_20"]
            sector_performances[sec] = sector_performances.get(sec, 0.0) + ret
            sector_counts[sec] = sector_counts.get(sec, 0) + 1

    sector_averages = {
        sec: (sector_performances[sec] / sector_counts[sec])
        for sec in sector_performances if sector_counts[sec] > 0
    }

    sorted_sectors = sorted(sector_averages.keys(), key=lambda x: sector_averages[x], reverse=True)
    top_sectors = set(sorted_sectors[:3])
    return top_sectors, sector_averages


def create_trade_levels(snapshot, setup):
    price = snapshot["price"]
    atr = snapshot["atr"]

    if setup == "PULLBACK":
        atr_stop = price - (1.2 * atr)
    else:
        atr_stop = price - (1.5 * atr)

    structure_stop = snapshot["previous_low10"] * 0.995
    stop_loss = max(atr_stop, structure_stop)
    stop_loss = min(stop_loss, price * 0.985)
    stop_loss = max(stop_loss, price * 0.90)

    risk_per_share = max(price - stop_loss, price * 0.015)
    fee_buffer = price * (round_trip_cost_pct() / 100)
    target_1r = price + risk_per_share + fee_buffer
    target_2r = price + (2 * risk_per_share) + fee_buffer
    days_to_target = math.ceil((target_2r - price) / atr) if atr > 0 else 0

    return {
        "entry_price": round(price, 2),
        "stop_loss": round(stop_loss, 2),
        "target_1r": round(target_1r, 2),
        "target_price": round(target_2r, 2),
        "risk_per_share": risk_per_share,
        "expected_days": max(1, days_to_target),
    }


def detect_setup(snapshot):
    price = snapshot["price"]
    uptrend = price > snapshot["ema50"] and snapshot["ema20"] > snapshot["ema50"]

    if not uptrend:
        return None

    if price > snapshot["previous_high20"] and snapshot["volume_ratio"] >= 1.3:
        return "BREAKOUT"

    if (
        -1.0 <= snapshot["extension_pct"] <= 4.5
        and 40 <= snapshot["rsi"] <= 62
        and snapshot["macd_hist"] > snapshot["macd_hist_prev"]
    ):
        return "PULLBACK"

    if snapshot["macd_hist"] > 0 and 52 <= snapshot["rsi"] <= 72:
        return "MOMENTUM"

    return None


SETUP_LABEL = {
    "BREAKOUT": "เบรกแนวต้าน 20 วัน",
    "PULLBACK": "ย่อตัวในแนวโน้มขาขึ้น",
    "MOMENTUM": "โมเมนตัมต่อเนื่อง",
}


def analyze_swing_symbol(symbol, frame, market_regime, top_sectors=None):
    snapshot = calculate_snapshot(frame)
    if snapshot is None:
        return None

    price = snapshot["price"]
    sector = SECTOR_MAP.get(symbol, "ทั่วไป")
    rs_20 = snapshot["ret_20"] - safe_float(market_regime.get("ret_20"), 0)
    rs_60 = snapshot["ret_60"] - safe_float(market_regime.get("ret_60"), 0)

    setup = detect_setup(snapshot)
    score = 0
    reasons = []

    if price > snapshot["ema20"] > snapshot["ema50"]:
        score += 18
        reasons.append("โครงสร้างขาขึ้น ราคายืนเหนือ EMA20 และ EMA50")
    elif price > snapshot["ema50"]:
        score += 8
        reasons.append("ราคายืนเหนือ EMA50")
    else:
        reasons.append("ราคายังต่ำกว่า EMA50")

    if snapshot["is_weekly_up"]:
        score += 10
        reasons.append("โครงสร้างรายสัปดาห์ (Weekly) ขาขึ้นแข็งแกร่ง")

    if top_sectors and sector in top_sectors:
        score += 12
        reasons.append(f"อยู่ในกลุ่มอุตสาหกรรมนำตลาด ({sector})")

    if rs_20 > 0:
        score += 12
        reasons.append(f"แข็งกว่าตลาดใน 20 วัน {rs_20:+.1f}%")

    if snapshot["macd_hist"] > 0 and snapshot["macd_hist"] > snapshot["macd_hist_prev"]:
        score += 10
        reasons.append("MACD เป็นบวกและกำลังขยายตัว")

    if 50 <= snapshot["rsi"] <= 70:
        score += 8
        reasons.append(f"RSI {snapshot['rsi']:.1f} อยู่ในโซนโมเมนตัม")

    if snapshot["volume_ratio"] >= 1.5:
        score += 10
        reasons.append(f"Volume หนาแน่น {snapshot['volume_ratio']:.2f}x")

    if setup == "BREAKOUT":
        score += 12
        reasons.append("เกิดสัญญาณเบรกแนวต้าน 20 วัน")
    elif setup == "PULLBACK":
        score += 10
        reasons.append("ย่อตัวเข้าหาแนวรับ EMA20")

    if snapshot["is_squeeze"]:
        score += 10
        reasons.append("Bollinger Bands บีบตัวแคบ (Squeeze) เตรียมระเบิดราคา")

    if snapshot["is_strong_close"]:
        score += 8
        reasons.append("แท่งเทียนปิดแข็งแกร่งบริเวณราคาสูงสุดของวัน")

    if MIN_ATR_PCT <= snapshot["atr_pct"] <= 7.0:
        score += 6

    if market_regime.get("is_positive") is True:
        score += 5
    elif market_regime.get("is_positive") is False:
        score -= 15
        reasons.append("หักคะแนนเนื่องจากตลาดรวมอ่อนแอ")

    rejected = False
    if snapshot["average_traded_value"] < MIN_AVG_VALUE:
        rejected = True
    if snapshot["rsi"] >= 80:
        rejected = True
        reasons.append("RSI สูงเกิน 80 เสี่ยงไล่ราคา")
    if price < snapshot["ema50"] and snapshot["macd_hist"] < 0:
        rejected = True
        reasons.append("ราคาและ MACD อยู่ในโครงสร้างขาลง")
    if snapshot["atr_pct"] < MIN_ATR_PCT:
        rejected = True
    if snapshot["atr_pct"] > MAX_ATR_PCT:
        rejected = True
    if not snapshot["is_weekly_up"]:
        rejected = True
        reasons.append("ตกเกณฑ์ Multi-Timeframe: กราฟรายสัปดาห์ (Weekly) เป็นขาลง")

    score = max(0, min(int(score), 100))

    if rejected:
        action = "REJECT"
    elif setup and score >= 65:
        action = "BUY"
    elif score >= 55:
        action = "WATCH"
    else:
        action = "HOLD"

    levels = create_trade_levels(snapshot, setup or "MOMENTUM")

    return {
        "symbol": symbol,
        "sector": sector,
        "score": score,
        "action": action,
        "setup": setup,
        "setup_label": SETUP_LABEL.get(setup, "ยังไม่มีรูปแบบเข้าเทรดชัดเจน"),
        "rs_20": rs_20,
        "rs_60": rs_60,
        "reasons": reasons,
        **snapshot,
        **levels,
    }


# =========================================================
# RISK AND POSITION SIZING
# =========================================================

def recent_loss_streak(portfolio):
    streak = 0
    for transaction in reversed(portfolio.get("transactions", [])):
        if transaction.get("type") != "SELL":
            continue
        if safe_float(transaction.get("realized_pnl"), 0) < 0:
            streak += 1
        else:
            break
    return streak


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


def calculate_open_risk(portfolio, frames):
    open_risk = 0.0
    for symbol, info in portfolio.get("stocks", {}).items():
        shares = int(info.get("shares", 0))
        stop_loss = safe_float(info.get("stop_loss"), 0)
        frame = frames.get(symbol)
        price = safe_float(info.get("average_cost"), 0)
        if frame is not None and not frame.empty:
            price = safe_float(frame["Close"].dropna().iloc[-1], price)
        open_risk += max(0.0, (price - stop_loss) * shares)
    return open_risk


def get_risk_profile(portfolio, equity=None, drawdown=0.0):
    mode = str(portfolio.get("risk_mode", DEFAULT_RISK_MODE)).lower()
    profile = dict(RISK_PROFILES.get(mode, RISK_PROFILES["balanced"]))
    profile["mode"] = mode if mode in RISK_PROFILES else "balanced"

    multiplier = 1.0
    notes = []

    if drawdown >= 0.15:
        multiplier *= 0.5
        notes.append("ลดความเสี่ยงลงครึ่งหนึ่ง เนื่องจาก Drawdown เกิน 15%")
    elif drawdown >= 0.08:
        multiplier *= 0.7
        notes.append("ลดความเสี่ยง 30% เนื่องจาก Drawdown เกิน 8%")

    streak = recent_loss_streak(portfolio)
    if streak >= 3:
        multiplier *= 0.5
        notes.append(f"แพ้ติดกัน {streak} ไม้ ระบบลดขนาดสถานะอัตโนมัติ")
    elif streak == 2:
        multiplier *= 0.7
        notes.append("แพ้ติดกัน 2 ไม้ ระบบลดขนาดสถานะชั่วคราว")

    profile["risk_per_trade"] = max(profile["risk_per_trade"] * multiplier, 0.0025)
    profile["max_total_risk"] = profile["max_total_risk"] * multiplier
    profile["notes"] = notes
    return profile


def calculate_position_size(portfolio, equity, entry_price, stop_loss, profile, open_risk=0.0):
    if entry_price <= 0 or stop_loss >= entry_price or equity <= 0:
        return 0, "ระยะ Stop Loss ไม่สมเหตุสมผล"

    cash = safe_float(portfolio.get("cash"), 0)
    risk_per_share = entry_price - stop_loss

    remaining_risk = max(0.0, (equity * profile["max_total_risk"]) - open_risk)
    risk_budget = min(equity * profile["risk_per_trade"], remaining_risk)

    if risk_budget <= 0:
        return 0, "ความเสี่ยงเปิดอยู่เต็มเพดานพอร์ตแล้ว"

    shares_by_risk = int(risk_budget / risk_per_share)
    shares_by_weight = int((equity * profile["max_weight"]) / entry_price)
    shares_by_cash = int((cash * 0.97) / entry_price)

    quantity = max(0, min(shares_by_risk, shares_by_weight, shares_by_cash))
    if quantity <= 0:
        return 0, "เงินสดหรืองบความเสี่ยงไม่พอสำหรับหุ้นราคานี้"

    lot_quantity = (quantity // BOARD_LOT) * BOARD_LOT
    if lot_quantity >= BOARD_LOT:
        return lot_quantity, "ซื้อแบบกระดานหลัก (Board Lot)"

    if ALLOW_ODD_LOT:
        return quantity, "ซื้อผ่านกระดานหน่วยย่อย (Odd Lot) เนื่องจากพอร์ตยังเล็ก"

    return 0, f"จำนวนต่ำกว่า 1 ล็อต ({BOARD_LOT} หุ้น)"


def portfolio_risk_status(portfolio, equity):
    plan = portfolio.get("financial_plan", {})
    high_water_mark = max(safe_float(portfolio.get("high_water_mark"), equity), equity)
    max_drawdown = safe_float(plan.get("max_portfolio_drawdown_pct"), 0.20)
    stop_value = safe_float(plan.get("trading_paused_at_value"), 8000)

    drawdown = ((high_water_mark - equity) / high_water_mark) if high_water_mark > 0 else 0

    return {
        "paused": equity <= stop_value or drawdown >= max_drawdown,
        "drawdown": drawdown,
        "high_water_mark": high_water_mark,
    }


# =========================================================
# TRADE STATISTICS AND PLAN TRACKING
# =========================================================

def trade_metrics(portfolio):
    sells = [t for t in portfolio.get("transactions", []) if t.get("type") == "SELL"]
    profits = [safe_float(t.get("realized_pnl"), 0) for t in sells]

    if not profits:
        return {
            "count": 0, "win_rate": 0.45, "rr": 2.0, "expectancy_r": 0.35, "is_estimate": True,
        }

    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]

    win_rate = len(wins) / len(profits)
    average_win = (sum(wins) / len(wins)) if wins else 0
    average_loss = (abs(sum(losses)) / len(losses)) if losses else 0
    rr = (average_win / average_loss) if average_loss > 0 else 2.0

    if len(profits) < 10:
        return {
            "count": len(profits), "win_rate": 0.45, "rr": 2.0, "expectancy_r": 0.35, "is_estimate": True,
        }

    return {
        "count": len(profits),
        "win_rate": win_rate,
        "rr": rr,
        "expectancy_r": (win_rate * rr) - (1 - win_rate),
        "is_estimate": False,
    }


def build_plan_report(portfolio, frames=None):
    plan = portfolio.get("financial_plan", {})
    target = safe_float(plan.get("target_value"), 20000)
    months = max(int(safe_float(plan.get("target_months"), 3)), 1)

    symbols = list(portfolio.get("stocks", {}).keys())
    if frames is None:
        frames = get_market_frames(symbols) if symbols else {}

    equity, _ = estimate_portfolio_equity(portfolio, frames)
    start_equity = max(safe_float(portfolio.get("plan_start_equity"), 10000), 1)
    start_date = parse_iso(portfolio.get("plan_start_date"), datetime.now(timezone.utc))

    total_days = months * 30.44
    elapsed_days = max((datetime.now(timezone.utc) - start_date).days, 0)
    remaining_days = max(total_days - elapsed_days, 1)
    remaining_trading_days = max(int(remaining_days * 5 / 7), 1)

    contributions = safe_float(portfolio.get("contributions_total"), 0)
    base_capital = start_equity + contributions

    required_total = (target / equity) - 1 if equity > 0 else 0
    required_monthly = ((target / equity) ** (30.44 / remaining_days) - 1) if equity > 0 else 0

    expected_now = start_equity * ((target / start_equity) ** (min(elapsed_days, total_days) / total_days))
    pace_gap = equity - expected_now

    profile = get_risk_profile(portfolio, equity)
    metrics = trade_metrics(portfolio)

    r_needed = (required_total / profile["risk_per_trade"]) if profile["risk_per_trade"] > 0 else 0
    trades_needed = (r_needed / metrics["expectancy_r"]) if metrics["expectancy_r"] > 0 else float("inf")
    trades_per_week = (trades_needed / (remaining_trading_days / 5)) if remaining_trading_days > 0 else 0

    lines = [
        "แผนการเงินและความคืบหน้า",
        f"เงินทุนตั้งต้น: {start_equity:,.2f} บาท",
        f"มูลค่าพอร์ตปัจจุบัน: {equity:,.2f} บาท",
        f"เป้าหมาย: {target:,.2f} บาท ภายใน {months} เดือน",
        "",
        f"ผ่านไป: {elapsed_days} วัน | เหลือ: {int(remaining_days)} วัน",
        f"กำไรสะสม: {((equity / base_capital - 1) * 100) if base_capital > 0 else 0:+.2f}%",
        f"ควรมีตามแผน: {expected_now:,.2f} บาท (ต่างกัน {pace_gap:+,.2f} บาท)",
        "",
        "สิ่งที่ต้องทำเพิ่ม",
        f"- ผลตอบแทนที่ต้องการ: {required_total * 100:+.2f}% ({required_monthly * 100:+.2f}% ต่อเดือน)",
        f"- ความเสี่ยงต่อไม้: {profile['risk_per_trade'] * 100:.2f}% ({profile['label']})",
        f"- ต้องทำกำไรสุทธิรวม: {r_needed:.1f}R",
    ]

    if math.isfinite(trades_needed):
        lines.append(f"- จำนวนไม้ที่ต้องเทรด: {trades_needed:.0f} ไม้ (~{trades_per_week:.1f} ไม้/สัปดาห์)")

    return "\n".join(lines)


def calculate_trade_statistics(portfolio):
    sells = [t for t in portfolio.get("transactions", []) if t.get("type") == "SELL"]
    if not sells:
        return "ยังไม่มีธุรกรรมขายสำหรับคำนวณสถิติ"

    profits = [safe_float(t.get("realized_pnl"), 0) for t in sells]
    wins = [v for v in profits if v > 0]
    losses = [v for v in profits if v < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    metrics = trade_metrics(portfolio)

    return (
        "สถิติการเทรดขั้นสูง\n"
        f"รายการขาย: {len(sells)} ครั้ง | ชนะ: {len(wins)} | แพ้: {len(losses)}\n"
        f"Win Rate: {len(wins) / len(profits) * 100:.2f}%\n"
        f"กำไร/ขาดทุนรวม: {sum(profits):+,.2f} บาท\n"
        f"Expectancy: {metrics['expectancy_r']:+.2f}R ต่อไม้\n"
        f"ค่าธรรมเนียมสะสม: {safe_float(portfolio.get('fees_total')):,.2f} บาท"
    )


# =========================================================
# REPORT GENERATION (WITH 5 ADVANCED TECHNIQUES)
# =========================================================

def combined_universe(portfolio):
    return unique_symbols(
        portfolio.get("watchlist", []) + list(portfolio.get("stocks", {}).keys())
    )


def build_buy_today_report(portfolio):
    symbols = combined_universe(portfolio)
    frames = get_market_frames(symbols + [MARKET_INDEX_SYMBOL])
    market_regime = get_market_regime(frames)
    top_sectors, sector_avgs = compute_sector_rankings(frames)

    equity, _ = estimate_portfolio_equity(portfolio, frames)
    risk_status = portfolio_risk_status(portfolio, equity)
    profile = get_risk_profile(portfolio, equity, risk_status["drawdown"])
    open_risk = calculate_open_risk(portfolio, frames)
    open_positions = len(portfolio.get("stocks", {}))
    slots = max(0, profile["max_positions"] - open_positions)

    candidates = []
    for symbol in symbols:
        result = analyze_swing_symbol(symbol, frames.get(symbol), market_regime, top_sectors)
        if not result or result["action"] != "BUY":
            continue
        if result["score"] < profile["min_score"]:
            continue
        if symbol in portfolio.get("stocks", {}):
            continue

        quantity, note = calculate_position_size(
            portfolio, equity, result["entry_price"],
            result["stop_loss"], profile, open_risk,
        )
        result["quantity"] = quantity
        result["size_note"] = note
        candidates.append(result)

    candidates.sort(key=lambda item: (item["score"], item["rs_20"]), reverse=True)
    candidates = candidates[:max(slots, 3)]

    today_text = now_local().strftime("%d/%m/%Y %H:%M")
    top_sector_names = ", ".join(list(top_sectors)[:3])

    lines = [
        f"หุ้นควรซื้อวันนี้ (ระบบสแกน 5 เทคนิคขั้นสูง) [{today_text}]",
        f"ตลาดรวม: {market_regime['message']}",
        f"กลุ่มอุตสาหกรรมนำตลาด (Sector Rotation): {top_sector_names}",
        f"มูลค่าพอร์ต: {equity:,.2f} บาท | โหมด: {profile['label']}",
    ]

    if risk_status["paused"]:
        lines.append("พอร์ตถึงเงื่อนไขหยุดเพิ่มความเสี่ยง ระบบแสดงเพื่อศึกษาเท่านั้น")

    if not candidates:
        lines.extend([
            "",
            "วันนี้ไม่มีหุ้นผ่านเกณฑ์เข้มงวดทั้ง 5 เทคนิค",
            "การถือเงินสดรอดจังหวะคือกลยุทธ์ที่ปลอดภัยที่สุดในภาวะนี้",
        ])
        return "\n".join(lines)

    for index, result in enumerate(candidates, 1):
        quantity = 0 if (risk_status["paused"] or slots <= 0) else result["quantity"]
        cost = quantity * result["entry_price"]
        buy_fee = calculate_commission(cost)
        net_target = net_profit_after_fee(result["entry_price"], result["target_price"], quantity)
        reason_text = "\n  - ".join(result["reasons"][:5])
        squeeze_text = "🔥 มีการบีบตัว" if result["is_squeeze"] else "ปกติ"

        lines.extend([
            "",
            f"{index}. {display_symbol(result['symbol'])} [{result['sector']}] "
            f"คะแนน {result['score']}/100",
            f"  รูปแบบ: {result['setup_label']} | Squeeze: {squeeze_text}",
            f"  ราคาล่าสุด: {result['price']:.2f} | RSI: {result['rsi']:.1f} | Volume: {result['volume_ratio']:.2f}x",
            f"  จุดเข้า: {result['entry_price']:.2f} | Stop: {result['stop_loss']:.2f} | เป้า 2R: {result['target_price']:.2f}",
            f"  ขนาดเหมาะสม: {quantity} หุ้น (~{cost:,.2f} ฿) | กำไรคาดหวังสุทธิ: {net_target:,.2f} ฿",
            f"  เหตุผล:\n  - {reason_text}",
        ])

    return "\n".join(lines)


def check_portfolio_sell_alerts(portfolio, frames=None):
    stocks = portfolio.get("stocks", {})
    if not stocks:
        return ""

    symbols = list(stocks.keys())
    if frames is None:
        frames = get_market_frames(symbols + [MARKET_INDEX_SYMBOL])

    market_regime = get_market_regime(frames)
    top_sectors, _ = compute_sector_rankings(frames)
    alerts = []

    for symbol, info in stocks.items():
        shares = int(info.get("shares", 0))
        average_cost = safe_float(info.get("average_cost"), 0)
        stop_loss = safe_float(info.get("stop_loss"), average_cost * 0.95)
        r_unit = max(safe_float(info.get("r_unit"), average_cost * 0.02), 0.01)
        target_price = safe_float(info.get("target_price"), average_cost + 2 * r_unit)

        result = analyze_swing_symbol(symbol, frames.get(symbol), market_regime, top_sectors)
        price = result["price"] if result else average_cost
        pnl_pct = ((price - average_cost) / average_cost * 100) if average_cost > 0 else 0
        r_multiple = (price - average_cost) / r_unit

        actions = []
        if price <= stop_loss:
            actions.append(f"ขายทั้งหมดทันที ราคาหลุด Stop Loss ที่ {stop_loss:.2f} บาท")
        elif price >= target_price:
            actions.append(f"ถึงเป้าหมาย 2R ({target_price:.2f} ฿) ควรขายทำกำไร")
        elif result and not result["is_weekly_up"]:
            actions.append("กราฟรายสัปดาห์ (Weekly) เปลี่ยนแนวโน้มเป็นขาลง ควรพิจารณาขายล็อกกำไร")

        if not actions:
            continue

        alerts.append(
            f"{display_symbol(symbol)} ({shares} หุ้น) | P/L: {pnl_pct:+.2f}% ({r_multiple:+.2f}R)\n"
            + "".join(f"  - {a}\n" for a in actions)
        )

    return "\n".join(alerts)


def build_daily_plan_report(portfolio):
    sections = [build_buy_today_report(portfolio)]
    alerts = check_portfolio_sell_alerts(portfolio)
    if alerts:
        sections.append("แจ้งเตือนจุดขายพอร์ต\n\n" + alerts)
    sections.append(build_plan_report(portfolio))
    return "\n\n==============================\n\n".join(sections)


def build_swing_report(portfolio, symbols, title, limit=10):
    symbols = unique_symbols(symbols)
    frames = get_market_frames(symbols + [MARKET_INDEX_SYMBOL])
    market_regime = get_market_regime(frames)
    top_sectors, _ = compute_sector_rankings(frames)

    equity, _ = estimate_portfolio_equity(portfolio, frames)
    risk_status = portfolio_risk_status(portfolio, equity)
    profile = get_risk_profile(portfolio, equity, risk_status["drawdown"])
    open_risk = calculate_open_risk(portfolio, frames)

    results = []
    for symbol in symbols:
        result = analyze_swing_symbol(symbol, frames.get(symbol), market_regime, top_sectors)
        if result and result["action"] in {"BUY", "WATCH"}:
            quantity, note = calculate_position_size(
                portfolio, equity, result["entry_price"],
                result["stop_loss"], profile, open_risk,
            )
            result["quantity"] = quantity
            result["size_note"] = note
            results.append(result)

    results.sort(key=lambda item: (item["score"], item["rs_20"]), reverse=True)
    results = results[:limit]

    lines = [
        title,
        f"ตลาดรวม: {market_regime['message']}",
        f"มูลค่าพอร์ต: {equity:,.2f} บาท | โหมด {profile['label']}",
    ]

    if not results:
        lines.extend(["", "ยังไม่มีหุ้นผ่านเกณฑ์ 5 เทคนิคขั้นสูง"])
        return "\n".join(lines)

    for index, result in enumerate(results, 1):
        quantity = 0 if risk_status["paused"] else result["quantity"]
        lines.extend([
            "",
            f"{index}. {display_symbol(result['symbol'])} [{result['sector']}]",
            f"สถานะ: {result['action']} | คะแนน {result['score']}/100 | {result['setup_label']}",
            f"ราคา {result['price']:.2f} | RSI {result['rsi']:.1f} | Squeeze: {result['is_squeeze']}",
            f"เข้า {result['entry_price']:.2f} | Stop {result['stop_loss']:.2f} | เป้า {result['target_price']:.2f}",
            f"จำนวน: {quantity} หุ้น ({result['size_note']})",
        ])

    return "\n".join(lines)


def build_single_stock_report(portfolio, symbol):
    formatted_symbol = format_symbol(symbol)
    frames = get_market_frames([formatted_symbol, MARKET_INDEX_SYMBOL])
    market_regime = get_market_regime(frames)
    top_sectors, _ = compute_sector_rankings(frames)
    result = analyze_swing_symbol(formatted_symbol, frames.get(formatted_symbol), market_regime, top_sectors)

    if not result:
        return f"ไม่สามารถวิเคราะห์ {display_symbol(formatted_symbol)} ได้"

    equity, _ = estimate_portfolio_equity(portfolio, frames)
    profile = get_risk_profile(portfolio, equity)
    open_risk = calculate_open_risk(portfolio, frames)
    quantity, size_note = calculate_position_size(
        portfolio, equity, result["entry_price"], result["stop_loss"], profile, open_risk,
    )

    reason_text = "\n".join(f"- {r}" for r in result["reasons"])
    return (
        f"วิเคราะห์ {display_symbol(formatted_symbol)} [{result['sector']}]\n"
        f"ตลาดรวม: {market_regime['message']}\n"
        f"สถานะ: {result['action']} | คะแนน {result['score']}/100\n"
        f"รูปแบบ: {result['setup_label']} | Squeeze: {result['is_squeeze']} | Weekly Up: {result['is_weekly_up']}\n"
        f"ราคา: {result['price']:.2f} | RSI: {result['rsi']:.1f} | Volume: {result['volume_ratio']:.2f}x\n"
        f"จุดเข้า: {result['entry_price']:.2f} | Stop: {result['stop_loss']:.2f} | เป้า: {result['target_price']:.2f}\n"
        f"จำนวนที่เหมาะสม: {quantity} หุ้น ({size_note})\n"
        f"เหตุผลเชิงเทคนิค (5 เทคนิค):\n{reason_text}\n\n"
        "ผลวิเคราะห์ไม่ใช่การรับประกันผลตอบแทน"
    )


def build_portfolio_report(user_id, update_trailing=False):
    portfolio = get_user_portfolio(user_id)
    stocks = portfolio.get("stocks", {})
    symbols = list(stocks.keys())

    frames = get_market_frames(symbols + [MARKET_INDEX_SYMBOL]) if symbols else {}
    equity, stock_value = estimate_portfolio_equity(portfolio, frames)
    cash = safe_float(portfolio.get("cash"), 0)
    risk_status = portfolio_risk_status(portfolio, equity)
    profile = get_risk_profile(portfolio, equity, risk_status["drawdown"])

    lines = [
        "สถานะพอร์ต",
        f"เงินสด: {cash:,.2f} ฿ | หุ้น: {stock_value:,.2f} ฿ | รวม: {equity:,.2f} ฿",
        f"Drawdown: {risk_status['drawdown'] * 100:.2f}% | โหมด: {profile['label']}",
    ]

    if not stocks:
        lines.extend(["", "ยังไม่มีหุ้นในพอร์ต"])
        update_user_portfolio(user_id, portfolio)
        return "\n".join(lines)

    lines.extend(["", "รายการหุ้น"])
    for symbol, info in stocks.items():
        shares = int(info.get("shares", 0))
        average_cost = safe_float(info.get("average_cost"), 0)
        frame = frames.get(symbol)
        price = average_cost
        if frame is not None:
            snap = calculate_snapshot(frame)
            if snap:
                price = snap["price"]

        pnl = (price - average_cost) * shares
        pnl_pct = ((price - average_cost) / average_cost * 100) if average_cost > 0 else 0
        lines.append(f"- {display_symbol(symbol)}: {shares} หุ้น | ต้นทุน {average_cost:.2f} | ล่าสุด {price:.2f} ({pnl_pct:+.2f}%)")

    update_user_portfolio(user_id, portfolio)
    return "\n".join(lines)


# =========================================================
# BACKGROUND QUEUE & COMMANDS
# =========================================================

executor = ThreadPoolExecutor(max_workers=2)
pending_jobs = set()
pending_jobs_lock = threading.RLock()


def run_scan_job(user_id, job_type, payload=None):
    try:
        portfolio = get_user_portfolio(user_id)
        if job_type == "summary":
            result = build_swing_report(portfolio, combined_universe(portfolio), "สรุปหุ้นวันนี้ (Top 5)", limit=5)
        elif job_type == "buy_today":
            result = build_buy_today_report(portfolio)
        elif job_type == "daily_plan":
            result = build_daily_plan_report(portfolio)
        elif job_type == "sell_check":
            alerts = check_portfolio_sell_alerts(portfolio)
            result = "แจ้งเตือนจุดขาย\n\n" + alerts if alerts else "ตรวจสอบแล้ว หุ้นในพอร์ตยังอยู่ในเกณฑ์ปกติ"
        elif job_type == "scan":
            result = build_swing_report(portfolio, combined_universe(portfolio), "ผลสแกนหุ้นขั้นสูง", limit=10)
        elif job_type == "set50":
            result = build_swing_report(portfolio, SET50_UNIVERSE, "ผลสแกน SET50", limit=10)
        elif job_type == "single":
            result = build_single_stock_report(portfolio, payload)
        else:
            result = "ไม่รู้จักประเภทการสแกน"

        push_long_message(user_id, result)
    except Exception:
        logger.exception("Background scan failed")
        push_long_message(user_id, "ระบบสแกนขัดข้องชั่วคราว")
    finally:
        with pending_jobs_lock:
            pending_jobs.discard((user_id, job_type, payload or ""))


def queue_scan(user_id, job_type, payload=None):
    job_key = (user_id, job_type, payload or "")
    with pending_jobs_lock:
        if job_key in pending_jobs:
            return False
        pending_jobs.add(job_key)
    executor.submit(run_scan_job, user_id, job_type, payload)
    return True


def process_buy(portfolio, symbol, shares, price, fee=None):
    symbol = format_symbol(symbol)
    shares = positive_int(shares, "จำนวนหุ้น")
    price = positive_float(price, "ราคา")
    gross = shares * price
    fee = calculate_commission(gross) if fee in (None, "") else non_negative_float(fee, "ค่าธรรมเนียม")
    total_cost = gross + fee
    cash = safe_float(portfolio.get("cash"), 0)

    if total_cost > cash:
        raise ValueError(f"เงินสดไม่พอ ต้องใช้ {total_cost:,.2f} ฿ แต่มี {cash:,.2f} ฿")

    frames = get_market_frames([symbol, MARKET_INDEX_SYMBOL])
    market_regime = get_market_regime(frames)
    analysis = analyze_swing_symbol(symbol, frames.get(symbol), market_regime)
    stop_loss = analysis["stop_loss"] if analysis else round(price * 0.95, 2)

    stocks = portfolio.setdefault("stocks", {})
    old = stocks.get(symbol)
    if old:
        new_shares = int(old["shares"]) + shares
        new_total_cost = safe_float(old.get("total_cost"), 0) + total_cost
        new_average = new_total_cost / new_shares
    else:
        new_shares = shares
        new_total_cost = total_cost
        new_average = total_cost / shares

    r_unit = max(new_average - stop_loss, new_average * 0.015)
    stocks[symbol] = {
        "shares": new_shares, "total_cost": new_total_cost, "average_cost": new_average,
        "initial_stop": stop_loss, "stop_loss": stop_loss, "r_unit": r_unit,
        "target_price": round(new_average + (2 * r_unit), 2),
        "highest_price": max(price, new_average), "tp1_done": False, "opened_at": utc_now_text(),
    }
    portfolio["cash"] = cash - total_cost
    portfolio["fees_total"] = safe_float(portfolio.get("fees_total"), 0) + fee
    portfolio.setdefault("transactions", []).append({
        "type": "BUY", "symbol": symbol, "shares": shares, "price": price, "fee": fee, "net_amount": total_cost, "date": utc_now_text(),
    })
    return f"บันทึกซื้อ {display_symbol(symbol)} {shares} หุ้น สำเร็จ (ต้นทุนเฉลี่ย {new_average:.2f} ฿)"


def process_sell(portfolio, symbol, shares, price, fee=None):
    symbol = format_symbol(symbol)
    shares = positive_int(shares, "จำนวนหุ้น")
    price = positive_float(price, "ราคา")
    stocks = portfolio.setdefault("stocks", {})

    if symbol not in stocks:
        raise ValueError(f"ไม่พบ {display_symbol(symbol)} ในพอร์ต")

    info = stocks[symbol]
    old_shares = int(info.get("shares", 0))
    if shares > old_shares:
        raise ValueError(f"มีหุ้นเพียง {old_shares} หุ้น ไม่พอขาย")

    gross = price * shares
    fee = calculate_commission(gross) if fee in (None, "") else non_negative_float(fee, "ค่าธรรมเนียม")
    old_total_cost = safe_float(info.get("total_cost"), 0)
    average_cost = old_total_cost / old_shares
    net_income = gross - fee
    realized_pnl = net_income - (average_cost * shares)

    portfolio["cash"] = safe_float(portfolio.get("cash"), 0) + net_income
    portfolio["realized_pnl_total"] = safe_float(portfolio.get("realized_pnl_total"), 0) + realized_pnl
    portfolio["fees_total"] = safe_float(portfolio.get("fees_total"), 0) + fee

    remaining = old_shares - shares
    if remaining == 0:
        del stocks[symbol]
    else:
        info["shares"] = remaining
        info["total_cost"] = old_total_cost - (average_cost * shares)

    portfolio.setdefault("transactions", []).append({
        "type": "SELL", "symbol": symbol, "shares": shares, "price": price, "fee": fee, "realized_pnl": realized_pnl, "date": utc_now_text(),
    })
    return f"บันทึกขาย {display_symbol(symbol)} {shares} หุ้น สำเร็จ กำไรสุทธิ {realized_pnl:+,.2f} ฿"


from scanner_upgrade import ScannerUpgrade


upgraded_scanner = ScannerUpgrade(
    get_frames=get_market_frames,
    universe=SET50_UNIVERSE,
    sectors=SECTOR_MAP,
    index_symbol=MARKET_INDEX_SYMBOL,
    commission=calculate_commission,
    get_portfolio=get_user_portfolio,
    risk_profile=get_risk_profile,
    risk_status=portfolio_risk_status,
    now_local=now_local,
    min_avg_value=MIN_AVG_VALUE,
    min_atr_pct=MIN_ATR_PCT,
    max_atr_pct=MAX_ATR_PCT,
    max_extension_pct=MAX_EXTENSION_PCT,
    board_lot=BOARD_LOT,
    allow_odd_lot=ALLOW_ODD_LOT,
)

# เปลี่ยนเฉพาะชั้นวิเคราะห์/รายงาน
# ไม่แก้ข้อมูลธุรกรรม และไม่ส่งคำสั่งซื้อขายจริง
build_buy_today_report = upgraded_scanner.buy_today
build_swing_report = upgraded_scanner.swing_report
build_single_stock_report = upgraded_scanner.single_report
check_portfolio_sell_alerts = upgraded_scanner.sell_alerts
build_portfolio_report = upgraded_scanner.portfolio_report
build_plan_report = upgraded_scanner.plan_report
build_daily_plan_report = upgraded_scanner.daily_plan


# =========================================================
# SCHEDULER
# =========================================================

def scheduled_daily_plan():
    for uid in get_registered_users():
        queue_scan(uid, "daily_plan")


scheduler = None
if ENABLE_SCHEDULER:
    try:
        scheduler = BackgroundScheduler(timezone=TIMEZONE_NAME)
        scheduler.add_job(
            scheduled_daily_plan,
            CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=TIMEZONE_NAME),
            id="daily-plan", replace_existing=True,
        )
        scheduler.start()
        logger.info("เปิด Scheduler เรียบร้อย")
    except Exception:
        logger.exception("เริ่ม Scheduler ไม่สำเร็จ")


# =========================================================
# FLASK & LINE WEBHOOK
# =========================================================

@app.route("/", methods=["GET", "HEAD"])
def health():
    return jsonify({"status": "ok", "service": "Advanced Tech Stock Bot", "version": SCHEMA_VERSION})


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    user_id = getattr(event.source, "user_id", None)
    if not user_id:
        return

    portfolio = get_user_portfolio(user_id)
    parts = text.split()
    reply_text = ""

    try:
        if text == "ดูพอร์ต":
            reply_text = build_portfolio_report(user_id, update_trailing=False)
        elif text == "วิเคราะห์พอร์ต":
            reply_text = build_portfolio_report(user_id, update_trailing=True)
        elif text in {"แผนเทรดวันนี้", "แผนวันนี้"}:
            queue_scan(user_id, "daily_plan")
            reply_text = "กำลังประมวลผลแผนเทรดขั้นสูง..."
        elif text in {"ควรซื้อวันนี้", "ซื้อวันนี้"}:
            queue_scan(user_id, "buy_today")
            reply_text = "กำลังคัดกรองหุ้นด้วย 5 เทคนิคขั้นสูง..."
        elif text in {"เช็กจุดขาย", "เช็คจุดขาย"}:
            queue_scan(user_id, "sell_check")
            reply_text = "กำลังตรวจสอบจุดขาย..."
        elif text in {"แผนการเงิน", "ความคืบหน้า"}:
            reply_text = build_plan_report(portfolio)
        elif text == "สรุปหุ้นวันนี้":
            queue_scan(user_id, "summary")
            reply_text = "กำลังจัดทำสรุปหุ้น..."
        elif text == "สแกนหุ้น":
            queue_scan(user_id, "scan")
            reply_text = "กำลังสแกนหุ้น..."
        elif text.startswith("สแกน ") or (text.startswith("วิเคราะห์ ") and text != "วิเคราะห์พอร์ต"):
            symbol = format_symbol(parts[1])
            queue_scan(user_id, "single", symbol)
            reply_text = f"กำลังวิเคราะห์ {display_symbol(symbol)} ด้วย 5 เทคนิค..."
        elif text == "สถิติการเทรด":
            reply_text = calculate_trade_statistics(portfolio)
        elif text.startswith("ซื้อ "):
            fee = parts[4] if len(parts) >= 5 else None
            reply_text = process_buy(portfolio, parts[1], parts[2], parts[3], fee)
            update_user_portfolio(user_id, portfolio)
        elif text.startswith("ขาย "):
            fee = parts[4] if len(parts) >= 5 else None
            reply_text = process_sell(portfolio, parts[1], parts[2], parts[3], fee)
            update_user_portfolio(user_id, portfolio)
        elif text.startswith("ฝากเงิน "):
            amount = positive_float(parts[1], "จำนวนเงิน")
            portfolio["cash"] = safe_float(portfolio.get("cash"), 0) + amount
            update_user_portfolio(user_id, portfolio)
            reply_text = f"ฝากเงิน {amount:,.2f} ฿ สำเร็จ"
        elif text in {"ช่วยเหลือ", "help", "คำสั่ง"}:
            reply_text = (
                "คำสั่งบอต (ระบบ 5 เทคนิคขั้นสูง):\n"
                "1. ดูพอร์ต / วิเคราะห์พอร์ต\n"
                "2. แผนเทรดวันนี้\n"
                "3. ควรซื้อวันนี้ (สแกน 5 เทคนิค)\n"
                "4. เช็กจุดขาย\n"
                "5. แผนการเงิน\n"
                "6. สรุปหุ้นวันนี้ / สแกนหุ้น\n"
                "7. วิเคราะห์ PTT\n"
                "8. ซื้อ / ขาย PTT [จำนวน] [ราคา]\n"
                "9. ฝากเงิน [จำนวน]"
            )
        else:
            reply_text = "ไม่รู้จักคำสั่ง พิมพ์ 'ช่วยเหลือ'"
    except ValueError as error:
        reply_text = f"ข้อมูลไม่ถูกต้อง: {error}"
    except Exception:
        logger.exception("Error handling message")
        reply_text = "ระบบขัดข้องชั่วคราว"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text[:MAX_LINE_TEXT]))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
