from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import hashlib
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote as url_quote
import tempfile
import threading
import time
import shutil

from apscheduler.schedulers.background import BackgroundScheduler

exchange_calendars_available = True
try:
    import exchange_calendars as xcals
except ImportError:
    exchange_calendars_available = False

from flask import Flask, abort, request
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import FileMessage, MessageEvent, TextMessage, TextSendMessage
import numpy as np
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb
import requests

from fundamental_analysis import analyze_fundamentals
from position_risk import estimate_position
from set100_constituents import (
    SET100_EFFECTIVE_FROM,
    SET100_EFFECTIVE_TO,
    SET100_SYMBOLS,
)


# =========================================================
# CONFIGURATION & LOGGING
# =========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    logger.warning(
        "ยังไม่ได้ตั้งค่า LINE Token หรือ Secret ใน Environment Variables"
    )

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ใช้ฐานข้อมูล PostgreSQL ภายนอกได้ด้วย DATABASE_URL
# หากไม่ตั้งค่า จะใช้ไฟล์ในเครื่อง ซึ่งอาจหายเมื่อ Render Free รีสตาร์ต
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORTFOLIO_FILE_PATH = os.getenv("PORTFOLIO_FILE_PATH", "").strip()
if not DATABASE_URL and not PORTFOLIO_FILE_PATH:
    logger.warning(
        "ยังไม่ได้ตั้ง DATABASE_URL หรือ PORTFOLIO_FILE_PATH; "
        "ข้อมูลพอร์ตอาจหายเมื่อ Deploy หรือรีสตาร์ต"
    )
PORTFOLIO_FILE = Path(
    PORTFOLIO_FILE_PATH or "portfolio.json"
)
ON_RENDER = bool(os.getenv("RENDER"))
def portfolio_storage_status():
    """Describe the configured portfolio storage without exposing secrets."""
    if DATABASE_URL:
        return (
            "PostgreSQL",
            "บันทึกในฐานข้อมูล ไม่ผูกกับไฟล์ชั่วคราวของ Web Service",
        )
    if PORTFOLIO_FILE_PATH:
        if ON_RENDER:
            return (
                "ไฟล์ตาม path ที่กำหนด",
                "บน Render ต้องตรวจสอบว่า path อยู่บน Persistent Disk "
                "(แผน Free ใช้ดิสก์ถาวรไม่ได้)",
            )
        return ("ไฟล์ตาม path ที่กำหนด", str(PORTFOLIO_FILE))
    if ON_RENDER:
        return (
            "พื้นที่ชั่วคราวของ Render",
            "ข้อมูลอาจหายเมื่อ Deploy, รีสตาร์ต หรือบริการพักตัว; "
            "ต้องตั้ง DATABASE_URL เพื่อเก็บพอร์ตถาวร",
        )
    return ("ไฟล์ local", str(PORTFOLIO_FILE))


_portfolio_lock = threading.RLock()
_portfolio_database_lock = threading.Lock()
_portfolio_database_ready = False

UNIVERSE = SET100_SYMBOLS

MAX_POSITIONS = 5
MAX_WATCHLIST = 20
RISK_PER_TRADE = 0.01
MAX_HOLDING_SESSIONS = 20
MIN_UNIVERSE_COVERAGE = 0.80
MOMENTUM_LOOKBACK = 63
VOLATILITY_LOOKBACK = 20
MIN_AVG_DAILY_VALUE_BAHT = float(
    os.getenv("MIN_AVG_DAILY_VALUE_BAHT", "5000000")
)


# =========================================================
# CALENDAR FUNCTIONS (exchange_calendars XBKK)
# =========================================================
def is_set_session(day):
    if not exchange_calendars_available:
        return day.weekday() < 5
    try:
        calendar = xcals.get_calendar("XBKK")
        return bool(calendar.is_session(day.isoformat()))
    except Exception:
        logger.exception("ตรวจวันซื้อขาย SET ไม่สำเร็จ")
        return day.weekday() < 5


def next_set_session(day):
    if not exchange_calendars_available:
        candidate = day + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate
    try:
        calendar = xcals.get_calendar("XBKK")
        next_day = day + timedelta(days=1)
        return calendar.date_to_session(
            next_day.isoformat(),
            direction="next",
        ).date()
    except Exception:
        logger.exception("หาวันซื้อขายถัดไปไม่สำเร็จ")
        candidate = day + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate


def latest_completed_set_session(current):
    """Use a completed SET session, never a live intraday bar."""
    candidate = current.date()
    if current.hour < 19:
        candidate -= timedelta(days=1)
    for _ in range(14):
        if is_set_session(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError("Cannot find a recently completed SET session")


# =========================================================
# TIME & PORTFOLIO STORAGE
# =========================================================
def now_bkk():
    return datetime.utcnow() + timedelta(hours=7)


def utc_text():
    # คงรูปแบบเวลาเดิมไว้เพื่อให้ข้อมูลที่บันทึกเข้ากันได้
    return now_bkk().strftime("%Y-%m-%d %H:%M:%S")


def _new_portfolio():
    return {
        "cash": 10000.0,
        "stocks": {},
        "history": [],
        "watchlist": [],
        "overnight_plan": None,
    }


def _load_portfolios_file_unlocked():
    if not PORTFOLIO_FILE.exists():
        return {}

    # ถ้าไฟล์เสียหรืออ่านไม่ได้ ต้องแจ้งข้อผิดพลาด
    # ห้ามตีความเป็นพอร์ตว่างแล้วเขียนทับข้อมูลเดิม
    with PORTFOLIO_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("ข้อมูลระดับบนของไฟล์พอร์ตต้องเป็น object")

    return data


def _connect_portfolio_database():
    global _portfolio_database_ready
    if not DATABASE_URL:
        raise RuntimeError("ยังไม่ได้ตั้ง DATABASE_URL")

    connection = psycopg.connect(
        DATABASE_URL,
        connect_timeout=10,
    )
    if not _portfolio_database_ready:
        try:
            with _portfolio_database_lock:
                if not _portfolio_database_ready:
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS line_bot_portfolios (
                            user_id TEXT PRIMARY KEY,
                            portfolio JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL
                                DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    connection.commit()
                    _portfolio_database_ready = True
        except Exception:
            connection.close()
            raise
    return connection


def _load_all_portfolios_unlocked():
    if not DATABASE_URL:
        return _load_portfolios_file_unlocked()

    with _connect_portfolio_database() as connection:
        rows = connection.execute(
            "SELECT user_id, portfolio FROM line_bot_portfolios"
        ).fetchall()
        if rows:
            return {
                user_id: portfolio
                for user_id, portfolio in rows
            }

        # ย้ายข้อมูลไฟล์เดิมเข้า DB ครั้งแรก หากไฟล์นั้นยังมีข้อมูล
        data = _load_portfolios_file_unlocked()
        if data:
            connection.executemany(
                """
                INSERT INTO line_bot_portfolios (user_id, portfolio)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET portfolio = EXCLUDED.portfolio,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [
                    (user_id, Jsonb(portfolio))
                    for user_id, portfolio in data.items()
                ],
            )
        return data


def load_all_portfolios():
    with _portfolio_lock:
        return _load_all_portfolios_unlocked()


def _save_all_portfolios_unlocked(data):
    if DATABASE_URL:
        with _connect_portfolio_database() as connection:
            connection.executemany(
                """
                INSERT INTO line_bot_portfolios (user_id, portfolio)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET portfolio = EXCLUDED.portfolio,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [
                    (user_id, Jsonb(portfolio))
                    for user_id, portfolio in data.items()
                ],
            )
        return

    PORTFOLIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None

    try:
        # สร้างไฟล์ชั่วคราวในโฟลเดอร์เดียวกับไฟล์จริง
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=PORTFOLIO_FILE.parent,
            prefix=f".{PORTFOLIO_FILE.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temporary_path = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())

        # เก็บไฟล์เดิมไว้กู้คืนก่อนแทนที่
        if PORTFOLIO_FILE.exists():
            backup_path = PORTFOLIO_FILE.with_name(
                PORTFOLIO_FILE.name + ".bak"
            )
            shutil.copy2(PORTFOLIO_FILE, backup_path)

        # แทนที่ไฟล์จริงเมื่อเขียนไฟล์ใหม่สำเร็จแล้วเท่านั้น
        os.replace(temporary_path, PORTFOLIO_FILE)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_all_portfolios(data):
    with _portfolio_lock:
        _save_all_portfolios_unlocked(data)


def export_portfolio_backup(user_id):
    """Return this user's portable, versioned JSON backup."""
    portfolio = get_portfolio(user_id)
    backup = json.dumps(
        {
            "format": "line-trading-bot-portfolio",
            "version": 1,
            "exported_at": utc_text(),
            "portfolio": {
                "cash": portfolio.get("cash", 0),
                "stocks": portfolio.get("stocks", {}),
                "watchlist": portfolio.get("watchlist", []),
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(backup.encode("utf-8")) > 3000:
        raise ValueError(
            "ข้อมูลพอร์ตใหญ่เกินกว่าจะสำรองในข้อความ LINE "
            "โปรดลดจำนวนหุ้น/รายการติดตามก่อน"
        )
    return backup


def validate_portfolio_backup(raw_text):
    """Parse and validate backup data before any current data is changed."""
    if len(raw_text.encode("utf-8")) > 3000:
        raise ValueError("ไฟล์สำรองใหญ่เกินกำหนด (สูงสุด 3 KB)")
    try:
        payload = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("อ่าน JSON ไม่ได้ ตรวจข้อความแล้วลองใหม่") from exc

    if not isinstance(payload, dict):
        raise ValueError("รูปแบบไฟล์สำรองไม่ถูกต้อง")
    if (
        payload.get("format") != "line-trading-bot-portfolio"
        or payload.get("version") != 1
    ):
        raise ValueError("ไม่รู้จักรูปแบบไฟล์สำรองนี้")

    portfolio = payload.get("portfolio")
    required = {"cash", "stocks", "watchlist"}
    if not isinstance(portfolio, dict) or not required.issubset(portfolio):
        raise ValueError("ข้อมูลพอร์ตในไฟล์สำรองไม่ครบ")

    cash = portfolio.get("cash")
    if (
        isinstance(cash, bool)
        or not isinstance(cash, (int, float))
        or not np.isfinite(cash)
        or cash < 0
    ):
        raise ValueError("ยอดเงินสดในไฟล์สำรองไม่ถูกต้อง")

    stocks = portfolio.get("stocks")
    if not isinstance(stocks, dict) or len(stocks) > 100:
        raise ValueError("รายการหุ้นในไฟล์สำรองไม่ถูกต้อง")
    normalized_stocks = {}
    for raw_symbol, info in stocks.items():
        symbol = normalize_stock_symbol(raw_symbol)
        if symbol is None or not isinstance(info, dict):
            raise ValueError("ข้อมูลหุ้นในไฟล์สำรองไม่ถูกต้อง")
        shares = info.get("shares")
        avg_price = info.get("avg_price")
        if (
            isinstance(shares, bool)
            or not isinstance(shares, int)
            or shares < 0
            or shares > 1_000_000_000
            or isinstance(avg_price, bool)
            or not isinstance(avg_price, (int, float))
            or not np.isfinite(avg_price)
            or avg_price < 0
            or (shares > 0 and avg_price <= 0)
        ):
            raise ValueError(f"ข้อมูลจำนวนหุ้น/ต้นทุน {symbol} ไม่ถูกต้อง")
        normalized_stocks[symbol] = {
            "shares": shares,
            "avg_price": float(avg_price),
        }

    history = portfolio.get("history", [])
    watchlist = portfolio.get("watchlist")
    if not isinstance(history, list) or len(history) > 5000:
        raise ValueError("ประวัติในไฟล์สำรองไม่ถูกต้อง")
    if not isinstance(watchlist, list) or len(watchlist) > MAX_WATCHLIST:
        raise ValueError("รายการติดตามในไฟล์สำรองไม่ถูกต้อง")
    normalized_watchlist = []
    for raw_symbol in watchlist:
        symbol = normalize_stock_symbol(raw_symbol)
        if symbol is None:
            raise ValueError("ชื่อหุ้นในรายการติดตามไม่ถูกต้อง")
        if symbol not in normalized_watchlist:
            normalized_watchlist.append(symbol)

    restored = {
        "cash": float(cash),
        "stocks": normalized_stocks,
        "history": history,
        "watchlist": normalized_watchlist,
        "overnight_plan": None,
    }
    try:
        json.dumps(restored, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("มีข้อมูลบางส่วนในไฟล์สำรองที่ใช้ไม่ได้") from exc
    return restored


def get_portfolio(user_id):
    with _portfolio_lock:
        all_p = _load_all_portfolios_unlocked()
        if user_id not in all_p:
            all_p[user_id] = _new_portfolio()
            _save_all_portfolios_unlocked(all_p)
        return all_p[user_id]


def mutate_portfolio(user_id, func):
    with _portfolio_lock:
        all_p = _load_all_portfolios_unlocked()
        if user_id not in all_p:
            all_p[user_id] = _new_portfolio()

        result = func(all_p[user_id])
        _save_all_portfolios_unlocked(all_p)
        return result


def registered_users():
    return list(load_all_portfolios().keys())


# =========================================================
# MARKET DATA & SELECTION
# =========================================================
# Keep successful Yahoo responses briefly so scheduled retries and LINE requests
# do not re-download the same daily bars repeatedly. This cache is intentionally
# process-local and short-lived; freshness validation still happens downstream.
_MARKET_DATA_CACHE_TTL_SECONDS = 15 * 60
_market_data_cache = {}
_market_data_cache_lock = threading.Lock()


def get_stock_history(symbol, period="3mo"):
    try:
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{url_quote(symbol, safe='')}?range={period}&interval=1d"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        response = None
        for attempt in range(3):
            try:
                response = requests.get(
                    url, headers=headers, timeout=10
                )
            except requests.RequestException as exc:
                if attempt < 2:
                    delay = min(2 ** attempt, 4) + (attempt * 0.25)
                    time.sleep(delay)
                    continue
                logger.warning(
                    "ดึงราคาหุ้นไม่สำเร็จจากเครือข่าย: "
                    "symbol=%s error=%s",
                    symbol,
                    type(exc).__name__,
                )
                return None
            if response.status_code == 200:
                break

            retryable = (
                response.status_code == 429
                or response.status_code >= 500
            )
            if retryable and attempt < 2:
                try:
                    delay = float(response.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    delay = 2 ** (attempt + 1)
                # Honor provider backoff while bounding a single wait.
                time.sleep(min(max(delay, 1), 8))
                continue

            logger.warning(
                "ดึงราคาหุ้นไม่สำเร็จ: symbol=%s status=%d",
                symbol,
                response.status_code,
            )
            return None

        if response is None or response.status_code != 200:
            return None
        res = response.json()
        result = res["chart"]["result"][0]
        timestamp = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        adjusted = (
            result.get("indicators", {})
            .get("adjclose", [{}])[0]
            .get("adjclose")
        )
        close = quote.get("close")

        df = pd.DataFrame(
            {
                "Open": quote.get("open"),
                "High": quote.get("high"),
                "Low": quote.get("low"),
                "Close": close,
                "Adj Close": adjusted if adjusted is not None else close,
                "Volume": quote.get("volume"),
            },
            index=pd.to_datetime(timestamp, unit="s"),
        )
        df.sort_index(inplace=True)
        df.dropna(subset=["Close", "Adj Close"], inplace=True)
        return df if not df.empty else None
    except Exception:
        logger.exception(
            "ดึงหรือแปลงข้อมูลราคาหนึ่งรายการไม่สำเร็จ: symbol=%s",
            symbol,
        )
        return None


def get_market_frames(symbols):
    unique_symbols = sorted(set(symbols))
    if not unique_symbols:
        return {}

    now = time.monotonic()
    frames = {}
    symbols_to_fetch = []
    with _market_data_cache_lock:
        for sym in unique_symbols:
            cached = _market_data_cache.get(sym)
            if cached and now - cached[0] < _MARKET_DATA_CACHE_TTL_SECONDS:
                frames[sym] = cached[1].copy(deep=True)
            else:
                symbols_to_fetch.append(sym)

    def fetch_one(sym):
        return sym, get_stock_history(sym, period="6mo")

    short_history = []
    # Keep provider load modest; failed symbols are retried on the next request.
    worker_count = min(2, len(symbols_to_fetch))
    if symbols_to_fetch:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for sym, df in executor.map(fetch_one, symbols_to_fetch):
                if df is not None and len(df) >= 20:
                    frames[sym] = df
                    with _market_data_cache_lock:
                        _market_data_cache[sym] = (
                            time.monotonic(),
                            df.copy(deep=True),
                        )
                elif df is not None:
                    short_history.append(sym)

    missing = [
        sym for sym in unique_symbols
        if sym not in frames and sym not in short_history
    ]
    logger.info(
        "ผลดึงข้อมูลราคา: received=%d requested=%d fetched=%d cached=%d "
        "no_data=%d short_history=%d sample=%s",
        len(frames),
        len(unique_symbols),
        len(symbols_to_fetch),
        len(unique_symbols) - len(symbols_to_fetch),
        len(missing),
        len(short_history),
        ",".join((missing + short_history)[:5]),
    )
    return frames

def normalize_stock_symbol(value):
    if not isinstance(value, str):
        return None
    base = value.strip().upper().removesuffix(".BK")
    if not re.fullmatch(r"[A-Z0-9]{1,10}", base):
        return None
    return base + ".BK"


def display_stock_symbol(value):
    """Show the SET ticker without Yahoo Finance's provider suffix."""
    if isinstance(value, str) and value.upper().endswith(".BK"):
        return value[:-3]
    return value


def combined_universe(portfolio):
    custom = [
        symbol for value in portfolio.get("watchlist", [])[:MAX_WATCHLIST]
        if (symbol := normalize_stock_symbol(value)) is not None
    ]
    return sorted(set(UNIVERSE + custom))


def _close_and_sma20(df, as_of):
    if (
        df is None
        or len(df) < 20
        or df.index[-1].strftime("%Y-%m-%d") != as_of
    ):
        return None

    close_col = "Adj Close" if "Adj Close" in df.columns else "Close"
    closes = df[close_col].dropna()
    if (
        len(closes) < 20
        or closes.index[-1].strftime("%Y-%m-%d") != as_of
    ):
        return None

    close = float(closes.iloc[-1])
    sma20 = float(closes.rolling(20).mean().iloc[-1])
    if not pd.notna(close) or not pd.notna(sma20) or sma20 <= 0:
        return None
    return close, sma20


def _selection_metrics(df, as_of):
    """Return risk-adjusted momentum, return, volatility, and liquidity."""
    if df is None or "Volume" not in df.columns:
        return None

    history = df.loc[
        df.index.strftime("%Y-%m-%d") <= as_of
    ].copy()
    if (
        history.empty
        or history.index[-1].strftime("%Y-%m-%d") != as_of
    ):
        return None

    close_col = (
        "Adj Close" if "Adj Close" in history.columns else "Close"
    )
    closes = pd.to_numeric(
        history[close_col], errors="coerce"
    ).dropna()
    if len(closes) < MOMENTUM_LOOKBACK + 1:
        return None

    recent = history.loc[
        closes.index
    ].tail(MOMENTUM_LOOKBACK + 1)
    recent_close = pd.to_numeric(
        recent[close_col], errors="coerce"
    )
    returns = recent_close.pct_change().dropna()
    if len(returns) < MOMENTUM_LOOKBACK:
        return None

    momentum = float(
        recent_close.iloc[-1] / recent_close.iloc[0] - 1
    )
    volatility = float(
        returns.tail(VOLATILITY_LOOKBACK).std(ddof=1)
        * np.sqrt(252)
    )
    traded_value = (
        pd.to_numeric(recent["Close"], errors="coerce")
        * pd.to_numeric(recent["Volume"], errors="coerce")
    ).tail(VOLATILITY_LOOKBACK)
    valid_traded_value = traded_value.replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    if len(valid_traded_value) < VOLATILITY_LOOKBACK:
        return None

    avg_value = float(valid_traded_value.median())
    if not all(
        np.isfinite(x)
        for x in (momentum, volatility, avg_value)
    ):
        return None
    if volatility <= 0 or avg_value <= 0:
        return None

    return (
        momentum / volatility,
        momentum,
        volatility,
        avg_value,
    )


def _risk_reference(df, as_of):
    """Use completed raw-price bars for an indicative two-ATR stop."""
    if df is None or not {"High", "Low", "Close"}.issubset(df.columns):
        return None
    history = df.loc[df.index.strftime("%Y-%m-%d") <= as_of]
    if len(history) < 15 or history.index[-1].strftime("%Y-%m-%d") != as_of:
        return None
    prices = history[["High", "Low", "Close"]].tail(15).apply(
        pd.to_numeric, errors="coerce"
    )
    if prices.isna().any().any():
        return None
    prior_close = prices["Close"].shift(1)
    true_range = pd.concat(
        [
            prices["High"] - prices["Low"],
            (prices["High"] - prior_close).abs(),
            (prices["Low"] - prior_close).abs(),
        ],
        axis=1,
    ).max(axis=1).iloc[1:]
    close = float(prices["Close"].iloc[-1])
    atr = float(true_range.mean())
    stop = close - 2 * atr
    if not all(np.isfinite(value) for value in (close, atr, stop)):
        return None
    return (close, stop) if close > 0 and atr > 0 and stop > 0 else None


def _portfolio_market_value(portfolio, frames, as_of):
    """Return cash and marked equity only when every holding has current data."""
    try:
        cash = float(portfolio["cash"])
        if not np.isfinite(cash) or cash < 0:
            return None
        equity = cash
        for symbol, holding in portfolio.get("stocks", {}).items():
            shares = float(holding["shares"])
            if not np.isfinite(shares) or shares < 0:
                return None
            if shares == 0:
                continue
            frame = frames.get(symbol)
            if frame is None or frame.empty:
                return None
            if frame.index[-1].strftime("%Y-%m-%d") != as_of:
                return None
            price = float(frame["Close"].iloc[-1])
            if not np.isfinite(price) or price <= 0:
                return None
            equity += shares * price
        return cash, equity
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def market_context(frames):
    """Measure SET100 breadth from constituents sharing a qualified date."""
    candidate_dates = set()
    valid_by_date = {}
    for sym in UNIVERSE:
        frame = frames.get(sym)
        if frame is None or frame.empty:
            continue
        as_of = frame.index[-1].strftime("%Y-%m-%d")
        candidate_dates.add(as_of)
        values = _close_and_sma20(frame, as_of)
        if values is not None:
            valid_by_date.setdefault(as_of, {})[sym] = values

    if not candidate_dates:
        return {
            "sufficient": False,
            "date": "",
            "breadth": 0,
            "coverage": 0,
            "pass": False,
            "valid_count": 0,
            "total_count": len(UNIVERSE),
            "missing_symbols": list(UNIVERSE),
        }

    qualified_dates = [
        day
        for day, valid in valid_by_date.items()
        if UNIVERSE
        and len(valid) / len(UNIVERSE) >= MIN_UNIVERSE_COVERAGE
    ]
    if qualified_dates:
        as_of = max(qualified_dates)
    else:
        # Report the date with the most usable rows; break ties toward newer data.
        as_of = max(
            candidate_dates,
            key=lambda day: (len(valid_by_date.get(day, {})), day),
        )

    valid = valid_by_date.get(as_of, {})
    coverage = (
        len(valid) / len(UNIVERSE) if UNIVERSE else 0
    )
    sufficient = coverage >= MIN_UNIVERSE_COVERAGE
    above_count = sum(
        close > sma20 for close, sma20 in valid.values()
    )
    breadth = (
        above_count / len(valid) * 100
        if sufficient and valid
        else 0
    )
    return {
        "sufficient": sufficient,
        "date": as_of,
        "breadth": breadth,
        "coverage": coverage,
        "pass": sufficient and breadth >= 50.0,
        "valid_count": len(valid),
        "total_count": len(UNIVERSE),
        "missing_symbols": [
            sym for sym in UNIVERSE if sym not in valid
        ],
    }

def build_daily_plan(portfolio, frames=None):
    symbols = combined_universe(portfolio)
    if frames is None:
        symbols_to_load = sorted(
            set(
                UNIVERSE
                + symbols
                + list(portfolio["stocks"])
            )
        )
        frames = get_market_frames(symbols_to_load)

    ctx = market_context(frames)
    report_lines = []
    selected = []
    now_str = now_bkk().strftime("%Y-%m-%d %H:%M")
    data_date = ctx["date"] if ctx["date"] else "ไม่ทราบ"

    report_lines.append(
        f"📊 แผนหมุนทุน | จัดทำ {now_str}"
    )
    report_lines.append(
        f"📅 วันที่ข้อมูลราคา: {data_date}"
    )
    report_lines.append(
        "🌐 ภาพตลาด: "
        + (
            "ผ่านเกณฑ์"
            if ctx["pass"]
            else "ยังไม่ผ่านเกณฑ์"
        )
    )
    report_lines.append(
        f"   Breadth {ctx['breadth']:.1f}%  •  "
        f"ข้อมูลครบ {ctx['coverage'] * 100:.0f}%"
    )

    if not ctx["sufficient"]:
        report_lines.append("")
        report_lines.append(
            "⚠️ ข้อมูลหุ้นไม่ครบ "
            f"(ต้องมีอย่างน้อย {MIN_UNIVERSE_COVERAGE:.0%} "
            "ของหุ้นหลักในวันเดียวกัน)"
        )
        report_lines.append(
            "จึงยังไม่สร้างสัญญาณ และควรรอข้อมูลรอบถัดไป"
        )
    elif ctx["pass"]:
        ranked = []
        for sym in symbols:
            values = _close_and_sma20(
                frames.get(sym), data_date
            )
            metrics = _selection_metrics(
                frames.get(sym), data_date
            )
            if values is None or metrics is None:
                continue

            close, sma20 = values
            score, momentum, volatility, avg_value = metrics
            if (
                close > sma20
                and momentum > 0
                and avg_value >= MIN_AVG_DAILY_VALUE_BAHT
            ):
                ranked.append(
                    (
                        score,
                        sym,
                        momentum,
                        volatility,
                        avg_value,
                    )
                )

        ranked.sort(
            key=lambda item: (-item[0], item[1])
        )
        selected_rows = ranked[:MAX_POSITIONS]
        selected = [
            row[1] for row in selected_rows
        ]

        fundamentals = {}
        if selected_rows:
            with ThreadPoolExecutor(
                max_workers=min(2, len(selected_rows))
            ) as executor:
                fundamentals = dict(
                    zip(
                        selected,
                        executor.map(
                            analyze_fundamentals,
                            selected,
                        ),
                    )
                )

        report_lines.append("")
        if selected:
            portfolio_value = _portfolio_market_value(
                portfolio, frames, data_date
            )
            report_lines.append(
                f"✅ หุ้นที่ผ่านเกณฑ์ ({len(selected)} ตัว)"
            )
            report_lines.append(
                "กรอบทดลอง: ถือประมาณ 1–4 สัปดาห์ "
                f"และตั้งงบขาดทุนตาม Stop ประมาณ {RISK_PER_TRADE:.0%} "
                "ของพอร์ตต่อรายการ"
            )
            for index, (
                _,
                sym,
                momentum,
                volatility,
                avg_value,
            ) in enumerate(selected_rows, 1):
                fundamental_text = (
                    fundamentals.get(sym, {}).get(
                        "summary",
                        "ข้อมูลพื้นฐานไม่พร้อม",
                    )
                )
                detail = (
                    f"{index}. 📈 {display_stock_symbol(sym)}\n"
                    f"   ผลตอบแทน 63 วัน: "
                    f"{momentum * 100:+.1f}%\n"
                    f"   ความผันผวนต่อปี: "
                    f"{volatility * 100:.1f}%\n"
                    f"   มูลค่าซื้อขายมัธยฐาน: "
                    f"{avg_value:,.0f} บาท/วัน\n"
                    f"   พื้นฐาน: {fundamental_text}"
                )
                if sym in portfolio.get("stocks", {}):
                    detail += "\n   มีหุ้นอยู่ในพอร์ตแล้ว: ไม่คำนวณซื้อเพิ่ม"
                elif portfolio_value is None:
                    detail += "\n   ยังประเมินขนาดซื้อไม่ได้: มูลค่าพอร์ตหรือราคาหุ้นที่ถือไม่ครบ"
                else:
                    reference = _risk_reference(frames.get(sym), data_date)
                    if reference is None:
                        detail += "\n   ยังประเมิน Stop และขนาดซื้อไม่ได้: ข้อมูลราคาไม่ครบ"
                    else:
                        close, stop = reference
                        cash, equity = portfolio_value
                        estimate = estimate_position(
                            equity, cash, close, stop, 1,
                            risk_fraction=RISK_PER_TRADE,
                        )
                        if estimate["shares"]:
                            detail += (
                                f"\n   Stop อ้างอิงประมาณ {stop:.2f} บาท"
                                f" | ขนาดสูงสุดประมาณ {estimate['shares']} หุ้น"
                                f" | ขาดทุนตามแผนประมาณ "
                                f"{estimate['estimated_loss']:,.0f} บาท"
                            )
                        else:
                            detail += (
                                "\n   งบเสี่ยง/เงินสดไม่พอสำหรับซื้อหนึ่งล็อต "
                                "จึงไม่เสนอจำนวนซื้อ"
                            )
                report_lines.append(detail)
            report_lines.append("")
            report_lines.append(
                "ขนาดซื้อเป็นเพียงประมาณการจากราคาปิด "
                "ของหุ้นแต่ละตัวแยกกัน ห้ามนำจำนวนซื้อทุกตัวมารวมกัน "
                "ต้องคำนวณใหม่ที่ราคาเปิดจริงและหลังซื้อแต่ละครั้ง"
            )
            report_lines.append(
                "🔁 ใช้สัญญาณราคาปิดเพื่อวางแผน"
                "วันซื้อขายถัดไป"
            )
            report_lines.append(
                "ทบทวนขายเมื่อหลุด 5 อันดับแรก"
                "หรือปิดต่ำกว่า SMA20 และทบทวนเมื่อถือครบ "
                f"{MAX_HOLDING_SESSIONS} วันซื้อขาย"
            )
            report_lines.append(
                "Stop อาจขายได้ต่ำกว่าราคาที่วางไว้เมื่อราคาเปิดกระโดด "
                "บอตยังไม่บันทึกวันซื้อขายจริง; "
                "โปรดอัปเดตเงินสดและหุ้นหลังซื้อขาย"
            )
        else:
            report_lines.append(
                "💵 ยังไม่มีหุ้นผ่านเกณฑ์ "
                "จึงแนะนำถือเงินสด"
            )
    else:
        report_lines.append("")
        report_lines.append(
            "🛡️ ตลาดไม่ผ่านเกณฑ์ Breadth"
        )
        report_lines.append(
            "แนะนำถือเงินสดและงดเปิดสถานะใหม่"
        )

    return "\n".join(report_lines), selected


# =========================================================
# OVERNIGHT PLAN SYSTEM
# =========================================================
_on_demand_lock = threading.Lock()
_on_demand_users = set()


def _prepare_on_demand(user_id, data_day):
    try:
        prepare_overnight_plans(
            target_data_day=data_day,
            only_user_ids=[user_id],
        )
        portfolio = get_portfolio(user_id)
        plan = portfolio.get("overnight_plan")
        if (
            isinstance(plan, dict)
            and plan.get("data_date") == data_day.isoformat()
            and plan.get("status") in {"READY", "NO_DATA"}
        ):
            if plan.get("status") == "NO_DATA":
                result_text = (
                    _format_plan_failure(plan)
                    + "\nระบบจะลองใหม่ตามรอบเตรียมแผนถัดไป"
                )
            else:
                result_text = read_overnight_plan(user_id)
            line_bot_api.push_message(
                user_id,
                TextSendMessage(text=result_text),
            )
            if plan.get("status") == "NO_DATA":
                def mark_failure_notified(p):
                    current_plan = p.get("overnight_plan")
                    if (
                        isinstance(current_plan, dict)
                        and current_plan.get("status") == "NO_DATA"
                        and current_plan.get("data_date")
                        == data_day.isoformat()
                    ):
                        current_plan["failure_notified_for"] = (
                            current_plan.get("delivery_day")
                        )

                mutate_portfolio(user_id, mark_failure_notified)
    except Exception:
        logger.exception("ส่งผลการเตรียมแผนตามคำขอไม่สำเร็จ")
    finally:
        with _on_demand_lock:
            _on_demand_users.discard(user_id)


def queue_on_demand_plan(user_id):
    """Start one background preparation without holding the LINE webhook."""
    ensure_scheduler()
    data_day = latest_completed_set_session(now_bkk())
    with _on_demand_lock:
        if user_id in _on_demand_users:
            return data_day, False
        _on_demand_users.add(user_id)
    try:
        job_id = "on-demand-" + hashlib.sha256(
            user_id.encode("utf-8")
        ).hexdigest()[:20]
        scheduler.add_job(
            _prepare_on_demand,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=1),
            args=[user_id, data_day],
            id=job_id,
            replace_existing=True,
            max_instances=1,
        )
    except Exception:
        with _on_demand_lock:
            _on_demand_users.discard(user_id)
        raise
    return data_day, True


def _format_plan_failure(plan, title="⚠️ รอบล่าสุดยังสร้างแผนไม่ได้"):
    diagnostic = plan.get("diagnostic", {})
    coverage = float(diagnostic.get("coverage", 0) or 0)
    actual_date = diagnostic.get("actual_date") or "ไม่พบข้อมูลราคาหุ้น"
    missing_sample = diagnostic.get("missing_sample", [])
    response = (
        f"{title}\n"
        f"วันที่ราคาที่พบ: {actual_date}\n"
        f"ข้อมูลหุ้นครบ {coverage:.0%} "
        f"({diagnostic.get('valid_count', 0)}/"
        f"{diagnostic.get('total_count', len(UNIVERSE))} ตัว)\n"
        f"สาเหตุ: {diagnostic.get('reason', 'ข้อมูลไม่ครบ')}"
    )
    if missing_sample:
        response += (
            "\nตัวอย่างหุ้นที่ข้อมูลขาด: "
            + ", ".join(display_stock_symbol(sym) for sym in missing_sample[:5])
        )
    return response


def read_overnight_plan(user_id, retry_no_data=False):
    portfolio = get_portfolio(user_id)
    plan = portfolio.get("overnight_plan")

    if not isinstance(plan, dict):
        try:
            data_day, _ = queue_on_demand_plan(user_id)
        except Exception:
            logger.exception("เริ่มเตรียมแผนตามคำขอไม่สำเร็จ")
            return "ยังเริ่มวิเคราะห์หุ้นไม่ได้ โปรดลองอีกครั้งภายหลัง"
        return (
            f"กำลังวิเคราะห์หุ้นจากราคาปิด {data_day.isoformat()}\n"
            "พิมพ์ ควรซื้อวันนี้ อีกครั้งหลังระบบเตรียมแผนเสร็จ "
            "ระหว่างนี้ยังไม่มีคำแนะนำซื้อที่ตรวจสอบได้"
        )

    today = now_bkk().date().isoformat()
    delivery_day = plan.get("delivery_day")
    status = plan.get("status")

    if status == "NO_DATA":
        try:
            expected_data_day = latest_completed_set_session(
                now_bkk()
            ).isoformat()
        except Exception:
            expected_data_day = None

        if plan.get("data_date") == expected_data_day:
            if retry_no_data:
                try:
                    data_day, started = queue_on_demand_plan(user_id)
                except Exception:
                    logger.exception(
                        "เริ่มเตรียมแผนตามคำขอไม่สำเร็จ"
                    )
                    return (
                        "ยังเริ่มวิเคราะห์หุ้นไม่ได้ "
                        "โปรดลองอีกครั้งภายหลัง"
                    )
                action = "เริ่มตรวจข้อมูลใหม่" if started else "กำลังตรวจข้อมูลอยู่"
                return (
                    f"รอบก่อนข้อมูลไม่พอ {action} "
                    f"จากราคาปิด {data_day.isoformat()} "
                    "เมื่อเสร็จระบบจะแจ้งผลใน LINE"
                )
            return (
                _format_plan_failure(plan)
                + "\nระบบจะลองใหม่ตามรอบเตรียมแผนถัดไป"
            )

        if not retry_no_data:
            return (
                _format_plan_failure(plan)
                + "\nข้อมูลชุดนี้ไม่ใช่รอบล่าสุด "
                "พิมพ์ ควรซื้อวันนี้ เพื่อเริ่มวิเคราะห์ใหม่"
            )

        try:
            data_day, started = queue_on_demand_plan(user_id)
        except Exception:
            logger.exception("เริ่มเตรียมแผนตามคำขอไม่สำเร็จ")
            return "ยังเริ่มวิเคราะห์หุ้นไม่ได้ โปรดลองอีกครั้งภายหลัง"
        action = "เริ่มตรวจข้อมูลใหม่" if started else "กำลังตรวจข้อมูลอยู่"
        return (
            f"ข้อมูลแผนเดิมเก่าแล้ว {action} "
            f"จากราคาปิด {data_day.isoformat()} "
            "เมื่อเสร็จระบบจะแจ้งผลใน LINE"
        )

    if status == "READY":
        return (
            f"แผนสำหรับวันซื้อขาย {delivery_day}\n"
            f"ใช้ราคาปิด {plan.get('data_date', 'ไม่ทราบ')}\n\n"
            + plan.get("text", "ไม่พบข้อความแผน")
        )

    if status in ("SENT", "DELIVERY_UNKNOWN"):
        prefix = (
            f"แผนสำหรับวันซื้อขาย {delivery_day}\n"
            "ใช้ราคาปิด "
            f"{plan.get('data_date', 'ไม่ทราบ')}\n\n"
        )
        if status == "DELIVERY_UNKNOWN":
            prefix = (
                "ยังยืนยันไม่ได้ว่าส่งแผนอัตโนมัติสำเร็จ\n\n"
                + prefix
            )
        return prefix + plan.get(
            "text", "ไม่พบข้อความแผน"
        )

    return "ยังไม่มีแผนที่ใช้ได้สำหรับวันนี้"


def prepare_overnight_plans(target_data_day=None, only_user_ids=None):
    current_day = target_data_day or latest_completed_set_session(now_bkk())
    logger.info(
        "เริ่มรอบเตรียมแผน: data_date=%s",
        current_day,
    )

    if not is_set_session(current_day):
        logger.info(
            "ข้ามรอบเตรียมแผน: ไม่ใช่วันซื้อขาย SET"
        )
        return

    try:
        users = only_user_ids if only_user_ids is not None else registered_users()
        logger.info(
            "รอบเตรียมแผน: registered=%d",
            len(users),
        )

        pending = {}
        read_failed = 0
        for user_id in users:
            try:
                portfolio = get_portfolio(user_id)
                if portfolio.get("investment_strategy", {}).get("style") == "LONG_TERM":
                    logger.info(
                        "ข้ามแผนรายวันสำหรับผู้ใช้โหมดลงทุนระยะยาว"
                    )
                    continue
                previous = portfolio.get(
                    "overnight_plan"
                )
                reusable_statuses = {
                    "READY",
                    "SENT",
                    "DELIVERY_UNKNOWN",
                }
                if not (
                    isinstance(previous, dict)
                    and previous.get("data_date")
                    == current_day.isoformat()
                    and previous.get("status")
                    in reusable_statuses
                ):
                    pending[user_id] = portfolio
            except Exception:
                read_failed += 1
                # ไม่บันทึก LINE user ID ใน Log
                logger.exception(
                    "อ่านพอร์ตหนึ่งรายการเพื่อเตรียมแผน"
                    "ไม่สำเร็จ"
                )

        if not pending:
            logger.info(
                "จบรอบเตรียมแผน: pending=0 "
                "read_failed=%d",
                read_failed,
            )
            return

        symbols = set(UNIVERSE)
        for portfolio in pending.values():
            symbols.update(
                combined_universe(portfolio)
            )
            symbols.update(
                portfolio.get("stocks", {}).keys()
            )

        logger.info(
            "เริ่มดึงข้อมูลราคา: symbols=%d "
            "pending=%d",
            len(symbols),
            len(pending),
        )
        frames = get_market_frames(symbols)
        if target_data_day is not None:
            cutoff = current_day.isoformat()
            frames = {
                symbol: frame.loc[
                    frame.index.strftime("%Y-%m-%d") <= cutoff
                ]
                for symbol, frame in frames.items()
            }
        ctx = market_context(frames)

        if (
            not ctx["sufficient"]
            or ctx["date"]
            != current_day.isoformat()
        ):
            missing = ctx.get("missing_symbols", [])
            if not ctx["date"]:
                reason = "ไม่มีข้อมูลราคาหุ้น SET100"
            elif not ctx["sufficient"]:
                reason = "ข้อมูลหุ้น SET100 ไม่ครบเกณฑ์"
            else:
                reason = "ข้อมูลราคาล่าสุดยังไม่ถึงวันที่ต้องใช้"
            diagnostic = {
                "reason": reason,
                "coverage": ctx["coverage"],
                "valid_count": ctx.get("valid_count", 0),
                "total_count": ctx.get(
                    "total_count", len(UNIVERSE)
                ),
                "missing_count": len(missing),
                "missing_sample": missing[:5],
                "actual_date": ctx["date"],
            }
            logger.warning(
                "ยังสร้างแผนไม่ได้: expected_date=%s "
                "actual_date=%s reason=%s coverage=%.1f%% "
                "valid=%d/%d missing=%d sample=%s frames=%d",
                current_day,
                ctx["date"],
                reason,
                ctx["coverage"] * 100,
                diagnostic["valid_count"],
                diagnostic["total_count"],
                diagnostic["missing_count"],
                ",".join(diagnostic["missing_sample"]),
                len(frames),
            )

            save_failed = 0
            delivery_day = next_set_session(
                current_day
            ).isoformat()
            failure_plan = {
                "data_date": current_day.isoformat(),
                "delivery_day": delivery_day,
                "prepared_at": utc_text(),
                "status": "NO_DATA",
                "diagnostic": diagnostic,
            }
            for user_id in pending:
                try:
                    def save_failure(portfolio):
                        previous = portfolio.get("overnight_plan")
                        if (
                            isinstance(previous, dict)
                            and str(previous.get("data_date", ""))
                            > current_day.isoformat()
                        ):
                            return
                        portfolio["overnight_plan"] = failure_plan

                    mutate_portfolio(
                        user_id,
                        save_failure,
                    )
                except Exception:
                    save_failed += 1
                    logger.exception(
                        "บันทึกสถานะข้อมูลไม่ครบ"
                        "ของหนึ่งพอร์ตไม่สำเร็จ"
                    )
            logger.info(
                "บันทึกสถานะแผนไม่มีข้อมูล: users=%d "
                "save_failed=%d",
                len(pending),
                save_failed,
            )
            return

        prepared = 0
        failed = 0

        for user_id, portfolio in pending.items():
            try:
                # A balance or watchlist may change while market data loads.
                portfolio = get_portfolio(user_id)
                portfolio_state = json.dumps(
                    {
                        key: portfolio.get(key)
                        for key in ("cash", "stocks", "watchlist")
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                )
                report, selected = build_daily_plan(
                    portfolio, frames=frames
                )
                new_plan = {
                    "data_date": (
                        current_day.isoformat()
                    ),
                    "delivery_day": (
                        next_set_session(
                            current_day
                        ).isoformat()
                    ),
                    "prepared_at": utc_text(),
                    "status": "READY",
                    "text": report,
                    "selected": selected,
                }

                def save_plan(p):
                    current_state = json.dumps(
                        {
                            key: p.get(key)
                            for key in ("cash", "stocks", "watchlist")
                        },
                        sort_keys=True,
                        ensure_ascii=False,
                    )
                    if current_state != portfolio_state:
                        return "พอร์ตเปลี่ยนระหว่างเตรียมแผน"
                    previous = p.get("overnight_plan")
                    if (
                        isinstance(previous, dict)
                        and str(previous.get("data_date", ""))
                        > current_day.isoformat()
                    ):
                        return "ข้ามแผนเก่ากว่า"
                    p["overnight_plan"] = new_plan
                    return "เตรียมแผนแล้ว"

                result = mutate_portfolio(user_id, save_plan)
                if result == "เตรียมแผนแล้ว":
                    prepared += 1
                else:
                    logger.info("ข้ามแผนหนึ่งรายการ: %s", result)
            except Exception:
                failed += 1
                logger.exception(
                    "เตรียมหรือบันทึกแผนหนึ่งรายการ"
                    "ไม่สำเร็จ"
                )

        logger.info(
            "จบรอบเตรียมแผน: prepared=%d "
            "failed=%d read_failed=%d "
            "pending=%d",
            prepared,
            failed,
            read_failed,
            len(pending),
        )
    except Exception:
        logger.exception(
            "รอบเตรียมแผนล้มเหลว"
        )


def send_overnight_plans():
    current = now_bkk()
    today = current.date()

    if not is_set_session(today):
        return
    if not (
        (
            current.hour == 8
            and current.minute >= 30
        )
        or (
            current.hour == 9
            and current.minute <= 5
        )
    ):
        return

    logger.info(
        "เริ่มรอบส่งแผน: delivery_day=%s",
        today,
    )

    try:
        users = registered_users()
    except Exception:
        logger.exception(
            "อ่านรายชื่อผู้ใช้เพื่อส่งแผน"
            "ไม่สำเร็จ"
        )
        return

    sent = 0
    failed = 0
    notified_no_data = 0
    skipped = 0

    for user_id in users:
        try:
            portfolio = get_portfolio(user_id)
            if portfolio.get("investment_strategy", {}).get("style") == "LONG_TERM":
                skipped += 1
                continue
            plan = portfolio.get(
                "overnight_plan"
            )
            if not isinstance(plan, dict):
                skipped += 1
                continue
            if plan.get("delivery_day") != today.isoformat():
                skipped += 1
                continue

            if plan.get("status") == "NO_DATA":
                if plan.get("failure_notified_for") == today.isoformat():
                    skipped += 1
                    continue
                message = (
                    _format_plan_failure(
                        plan,
                        title="⚠️ วันนี้ระบบยังจัดทำแผนหุ้นไม่ได้",
                    )
                    + "\nระบบงดแนะนำจนกว่าจะได้ข้อมูลครบ "
                    "ลองพิมพ์ ควรซื้อวันนี้ เพื่อตรวจใหม่"
                )
                line_bot_api.push_message(
                    user_id,
                    TextSendMessage(text=message),
                )

                def mark_failure_notified(p):
                    current_plan = p.get("overnight_plan")
                    if (
                        isinstance(current_plan, dict)
                        and current_plan.get("status") == "NO_DATA"
                        and current_plan.get("delivery_day")
                        == today.isoformat()
                    ):
                        current_plan["failure_notified_for"] = (
                            today.isoformat()
                        )

                mutate_portfolio(user_id, mark_failure_notified)
                notified_no_data += 1
                continue

            if plan.get("status") != "READY":
                skipped += 1
                continue

            def claim(p):
                cur = p.get(
                    "overnight_plan"
                )
                if (
                    not isinstance(cur, dict)
                    or cur.get(
                        "delivery_day"
                    )
                    != today.isoformat()
                    or cur.get("status")
                    != "READY"
                ):
                    return "SKIP"

                # ไม่ระบุว่า SENT จนกว่า LINE
                # ตอบกลับว่าส่งสำเร็จ
                cur["status"] = (
                    "DELIVERY_UNKNOWN"
                )
                return "CLAIMED"

            if (
                mutate_portfolio(
                    user_id, claim
                )
                != "CLAIMED"
            ):
                skipped += 1
                continue

            line_bot_api.push_message(
                user_id,
                TextSendMessage(
                    text=(
                        "แผนเช้าวันซื้อขาย "
                        f"{today.isoformat()}\n"
                        "ข้อมูลราคาปิด "
                        f"{plan['data_date']}\n\n"
                        + plan["text"]
                    )
                ),
            )

            def confirm_sent(p):
                cur = p.get(
                    "overnight_plan"
                )
                if (
                    isinstance(cur, dict)
                    and cur.get(
                        "delivery_day"
                    )
                    == today.isoformat()
                    and cur.get("status")
                    == "DELIVERY_UNKNOWN"
                ):
                    cur["status"] = "SENT"

            mutate_portfolio(
                user_id, confirm_sent
            )
            sent += 1
        except Exception:
            failed += 1
            logger.exception(
                "ส่งหรือบันทึกสถานะ"
                "แผนหนึ่งรายการไม่สำเร็จ; "
                "ตรวจสถานะ DELIVERY_UNKNOWN"
            )

    logger.info(
        "จบรอบส่งแผน: sent=%d "
        "notified_no_data=%d failed=%d skipped=%d "
        "registered=%d",
        sent,
        notified_no_data,
        failed,
        skipped,
        len(users),
    )


# =========================================================
# LINE WEBHOOK & COMMANDS
# =========================================================
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get(
        "X-Line-Signature", ""
    )
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text.strip()

    response_text = command_reply(
        user_id, text
    )
    if response_text:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=response_text
            ),
        )


@handler.add(MessageEvent, message=FileMessage)
def handle_backup_file(event):
    user_id = event.source.user_id
    message = event.message
    filename = (message.file_name or "").lower()
    if not filename.endswith(".json"):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="รับคืนได้เฉพาะไฟล์สำรอง .json จากบอต"),
        )
        return
    if message.file_size and message.file_size > 900_000:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="⚠️ ไฟล์สำรองใหญ่เกินกำหนด (สูงสุด 900 KB)"
            ),
        )
        return
    try:
        content = line_bot_api.get_message_content(message.id)
        raw_backup = b"".join(content.iter_content())
        if len(raw_backup) > 900_000:
            raise ValueError("ไฟล์สำรองใหญ่เกินกำหนด (สูงสุด 900 KB)")
        restored = validate_portfolio_backup(
            raw_backup.decode("utf-8-sig")
        )

        def restore_portfolio(current):
            restore_history = list(restored["history"])
            restore_history.append({
                "type": "PORTFOLIO_RESTORED",
                "restored_at": utc_text(),
                "previous_cash": current.get("cash"),
                "previous_stock_count": len(current.get("stocks", {})),
            })
            restored["history"] = restore_history
            current.clear()
            current.update(restored)
            return (
                "✅ กู้คืนพอร์ตแล้ว\n"
                f"เงินสด: {restored['cash']:,.2f} บาท\n"
                f"หุ้น: {len(restored['stocks'])} รายการ\n"
                f"รายการติดตาม: {len(restored['watchlist'])} รายการ\n"
                "ระบบจะเตรียมแผนใหม่จากข้อมูลตลาดล่าสุด"
            )

        result = mutate_portfolio(user_id, restore_portfolio)
    except ValueError as exc:
        result = f"⚠️ กู้คืนไม่ได้: {exc}"
    except Exception:
        logger.exception("กู้คืนพอร์ตจากไฟล์ LINE ไม่สำเร็จ")
        result = "⚠️ อ่านหรือบันทึกไฟล์สำรองไม่สำเร็จ พอร์ตเดิมไม่ได้รับการยืนยันว่าเปลี่ยนแปลง"
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=result),
    )


def build_long_term_accumulation_plan(portfolio):
    """Screen SET100 candidates for a cautious, user-triggered accumulation review."""
    strategy = portfolio.get("investment_strategy", {})
    expected_session = latest_completed_set_session(now_bkk())
    # Free quote sources can publish the latest SET close one session late.
    # For a long-term screen, accept the latest session or the immediately
    # preceding SET session, while preserving the universe coverage threshold.
    previous_session = latest_completed_set_session(
        datetime.combine(expected_session, datetime.min.time())
    )
    allowed_data_dates = {
        expected_session.isoformat(),
        previous_session.isoformat(),
    }
    symbols = sorted(set(UNIVERSE) | set(portfolio.get("stocks", {}).keys()))
    frames = get_market_frames(symbols)
    ctx = market_context(frames)
    if not ctx["sufficient"]:
        return (
            "⚠️ ยังจัดแผนสะสมหุ้นไม่ได้ เพราะข้อมูล SET100 "
            "ไม่ครบเกณฑ์\n"
            f"วันที่ข้อมูล: {ctx.get('date') or 'ไม่มีข้อมูล'} | "
            f"ข้อมูลครบ {ctx.get('coverage', 0):.0%} "
            f"({ctx.get('valid_count', 0)}/{ctx.get('total_count', len(UNIVERSE))}) "
            "ต้องครบอย่างน้อย 80% ในวันเดียวกัน"
        )
    if ctx["date"] not in allowed_data_dates:
        return (
            "⚠️ ยังจัดแผนสะสมหุ้นไม่ได้ เพราะข้อมูลราคาล่าช้าเกินหนึ่งวันซื้อขาย\n"
            f"วันที่ข้อมูลล่าสุด: {ctx.get('date') or 'ไม่มีข้อมูล'} | "
            f"วันซื้อขายล่าสุดที่ควรมีข้อมูล: {expected_session.isoformat()}\n"
            "ระบบงดแนะนำจนกว่าจะได้ข้อมูลที่ใหม่กว่านี้"
        )

    cash = max(0.0, float(portfolio.get("cash", 0) or 0))
    holdings = portfolio.get("stocks", {})
    current_equity_value = 0.0
    unpriced_holdings = []
    for symbol, info in holdings.items():
        shares = int(info.get("shares", 0))
        if shares <= 0:
            continue
        frame = frames.get(symbol)
        if frame is None or frame.empty or frame.index[-1].strftime("%Y-%m-%d") != ctx["date"]:
            unpriced_holdings.append(symbol)
            continue
        close_col = "Close" if "Close" in frame.columns else "Adj Close"
        close = pd.to_numeric(frame[close_col], errors="coerce").dropna()
        if close.empty or close.index[-1].strftime("%Y-%m-%d") != ctx["date"]:
            unpriced_holdings.append(symbol)
            continue
        current_equity_value += shares * float(close.iloc[-1])

    total_value = cash + current_equity_value
    starting_capital = float(strategy.get("starting_capital", 10000.0))
    target_equity = starting_capital * float(
        strategy.get("target_equity_pct", 30.0)
    ) / 100.0
    remaining_target = max(0.0, target_equity - current_equity_value)
    budget = min(cash, remaining_target)

    header = [
        "🧭 แผนสะสมหุ้นไทยระยะยาว (ข้อมูลประกอบ ไม่ใช่คำสั่งซื้อ)",
        f"📅 ราคาปิดข้อมูลล่าสุด: {ctx['date']}",
        f"📚 รายชื่อ SET100: {SET100_EFFECTIVE_FROM} ถึง {SET100_EFFECTIVE_TO} | "
        "ราคาจาก Yahoo Finance ไม่ใช่ feed โดยตรงจาก SET",
        *(
            ["⚠️ ราคาปิดช้ากว่าวันซื้อขายล่าสุดหนึ่งรอบ ใช้เป็นข้อมูลคัดกรองแผนถือยาว"]
            if ctx["date"] != expected_session.isoformat()
            else []
        ),
        f"ภาพตลาด SET100: Breadth {ctx['breadth']:.1f}% "
        f"({ctx['valid_count']}/{ctx['total_count']} ตัว)",
        f"พอร์ตที่ประเมินจากข้อมูลในบอต: {total_value:,.2f} บาท",
        f"หุ้นตามเป้าหมาย: {target_equity:,.2f} บาท | "
        f"มูลค่าหุ้นปัจจุบัน: {current_equity_value:,.2f} บาท",
    ]
    if unpriced_holdings:
        return "\n".join(
            header
            + [
                "⚠️ ยังประเมินหุ้นที่ถืออยู่ไม่ได้ครบ: "
                + ", ".join(unpriced_holdings[:5]),
                "จึงยังไม่เสนอซื้อเพิ่ม เพื่อไม่คำนวณสัดส่วนผิด",
            ]
        )
    if budget <= 0:
        return "\n".join(
            header
            + [
                "ยังไม่แนะนำสะสมเพิ่ม เพราะเงินสดที่บันทึกหรือวงเงินหุ้นเป้าหมายไม่เหลือ",
                "ทบทวนพอร์ตอีกครั้งเดือนหน้า หรืออัปเดตยอดเงินจริงก่อน",
            ]
        )

    eligible = []
    for symbol in UNIVERSE:
        if symbol in holdings:
            continue
        frame = frames.get(symbol)
        if frame is None or frame.empty or frame.index[-1].strftime("%Y-%m-%d") != ctx["date"]:
            continue
        close_col = "Close" if "Close" in frame.columns else "Adj Close"
        closes = pd.to_numeric(frame[close_col], errors="coerce").dropna()
        if len(closes) < 100 or closes.index[-1].strftime("%Y-%m-%d") != ctx["date"]:
            continue
        recent = closes.tail(60)
        close = float(recent.iloc[-1])
        sma50 = float(recent.tail(50).mean())
        if not np.isfinite(close) or not np.isfinite(sma50) or close <= sma50:
            continue
        volume = pd.to_numeric(frame.loc[recent.index, "Volume"], errors="coerce")
        traded = (
            pd.to_numeric(frame.loc[recent.index, "Close"], errors="coerce")
            * volume
        ).tail(20).dropna()
        if len(traded) < 15 or float(traded.median()) < MIN_AVG_DAILY_VALUE_BAHT:
            continue
        returns = recent.pct_change().dropna()
        volatility = float(returns.tail(20).std(ddof=1) * np.sqrt(252))
        if not np.isfinite(volatility) or volatility <= 0:
            continue
        eligible.append({
            "symbol": symbol,
            "close": close,
            "sma50": sma50,
            "volatility": volatility,
            "avg_value": float(traded.median()),
        })

    # Limit fundamental-data calls to a liquid, lower-volatility shortlist.
    eligible.sort(key=lambda item: (-item["avg_value"], item["volatility"]))
    shortlist = eligible[:15]
    if not shortlist:
        return "\n".join(
            header
            + [
                "⚠️ รอบนี้ไม่มีหุ้นที่ผ่านเกณฑ์แนวโน้มราคาและสภาพคล่อง",
                "ระบบไม่ฝืนเลือกหุ้น และจะให้ตรวจใหม่ในรอบเดือนถัดไป",
            ]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        fundamentals = list(executor.map(
            analyze_fundamentals,
            [item["symbol"] for item in shortlist],
        ))
    candidates = []
    for item, fundamental in zip(shortlist, fundamentals):
        available = int(fundamental.get("available", 0) or 0)
        score = fundamental.get("score")
        # Require at least three observed positive checks; missing data is not
        # treated as a pass. This does not include a valuation check.
        if available < 3 or score is None or int(score) != available:
            continue
        item = dict(item)
        item["fundamental"] = fundamental
        candidates.append(item)

    candidates.sort(key=lambda item: (
        -int(item["fundamental"].get("available", 0)),
        item["volatility"],
        -item["avg_value"],
    ))
    if not candidates:
        return "\n".join(
            header
            + [
                "⚠️ รอบนี้ไม่มีหุ้นที่ผ่านเกณฑ์พื้นฐานพร้อมข้อมูลอย่างน้อย 3 รายการ",
                "ข้อมูลพื้นฐานที่ขาดจะไม่นับเป็นคะแนนผ่าน จึงงดแนะนำซื้อ",
            ]
        )

    # Keep at least two names to avoid turning the small equity allocation
    # into a single-stock bet. Use the standard 100-share lot as a conservative
    # affordability check; verify the actual board lot and fees with the broker.
    chosen = []
    per_name_budget = 0.0
    for count in (3, 2):
        per_name_budget = budget / count
        affordable = [
            item for item in candidates
            if item["close"] * 100 <= per_name_budget
        ]
        if len(affordable) >= count:
            chosen = affordable[:count]
            break

    if not chosen:
        odd_lot_budget = budget / 2
        odd_lot_candidates = [
            item for item in candidates
            if item["close"] <= odd_lot_budget
        ][:2]
        if len(odd_lot_candidates) == 2:
            lines = header + [
                f"งบสูงสุดรอบนี้ {budget:,.2f} บาท "
                f"แบ่งสองบริษัทประมาณ {odd_lot_budget:,.2f} บาทต่อบริษัท",
                "ตัวเลือกซื้อหน่วยย่อย (Odd Lot) หากโบรกเกอร์รองรับ:",
            ]
            for index, item in enumerate(odd_lot_candidates, 1):
                shares = int(odd_lot_budget // item["close"])
                estimated_cost = shares * item["close"]
                lines.append(
                    f"{index}. {display_stock_symbol(item['symbol'])} — "
                    f"ราคาปิด {item['close']:.2f} บาท; ประมาณ {shares:,} หุ้น "
                    f"({estimated_cost:,.2f} บาท ก่อนค่าธรรมเนียม)"
                )
                lines.append(
                    f"   พื้นฐานผ่าน {item['fundamental']['score']}/"
                    f"{item['fundamental']['available']}: "
                    f"{item['fundamental']['summary']}"
                )
            lines.extend([
                "Odd Lot อาจมีสภาพคล่อง/ส่วนต่างราคาและโอกาสจับคู่ต่างจากกระดานหลัก "
                "ตรวจสอบกับโบรกเกอร์ก่อนส่งคำสั่ง",
                "เป็นเพียงตัวเลือกเพื่อพิจารณาสะสม ไม่รับประกันผลตอบแทน "
                "หรือจำกัดขาดทุนไว้ที่ 10%",
            ])
            return "\n".join(lines)
        return "\n".join(
            header
            + [
                f"เงินสดที่จัดสรรรอบนี้ได้ {budget:,.2f} บาท "
                "ยังไม่พอซื้ออย่างน้อย 1 board lot ต่อหุ้นให้กระจายอย่างน้อย 2 บริษัท",
                "ถือเงินสดสะสมต่อก่อน ไม่จำเป็นต้องรีบซื้อ; "
                "หากใช้กระดาน Odd Lot ให้ตรวจราคาและค่าธรรมเนียมกับโบรกเกอร์",
                "หุ้นที่ผ่านเกณฑ์เพื่อเฝ้าดู: "
                + ", ".join(
                    f"{display_stock_symbol(item['symbol'])} {item['close']:.2f} บาท"
                    for item in candidates[:3]
                ),
            ]
        )

    lines = header + [
        f"งบสะสมรอบนี้ไม่เกิน {budget:,.2f} บาท "
        f"(แบ่งประมาณ {per_name_budget:,.2f} บาทต่อบริษัท)",
        "หุ้นที่ผ่านหน้าจอข้อมูลรอบนี้:",
    ]
    for index, item in enumerate(chosen, 1):
        shares = int(per_name_budget // (item["close"] * 100)) * 100
        estimated_cost = shares * item["close"]
        fundamental = item["fundamental"]
        lines.append(
            f"{index}. {display_stock_symbol(item['symbol'])} — ราคาปิด {item['close']:.2f} บาท; "
            f"ประมาณ {shares:,} หุ้น / {estimated_cost:,.2f} บาท "
            "(ก่อนค่าธรรมเนียม)"
        )
        lines.append(
            f"   พื้นฐานผ่าน {fundamental['score']}/{fundamental['available']}: "
            f"{fundamental['summary']}"
        )
        lines.append(
            f"   ราคาอยู่เหนือ SMA50; "
            f"ความผันผวนย้อนหลัง 20 วันต่อปี {item['volatility'] * 100:.1f}%"
        )

    lines.extend([
        "แนวทาง: ใช้เป็นรายการพิจารณาสะสมเดือนนี้ ไม่ใช่ให้ซื้อทุกตัวทันที "
        "ตรวจงบล่าสุด ราคา ณ เวลาส่งคำสั่ง และ board lot/ค่าธรรมเนียมกับโบรกเกอร์",
        "หน้าจอนี้ไม่ประเมินมูลค่าเหมาะสมหรือรับประกันผลตอบแทน "
        "และไม่รับประกันว่าพอร์ตจะขาดทุนไม่เกิน 10%",
    ])
    return "\n".join(lines)


_long_term_users = set()


def _prepare_long_term_recommendation(user_id):
    try:
        portfolio = get_portfolio(user_id)
        if portfolio.get("investment_strategy", {}).get("style") != "LONG_TERM":
            result = "ยังไม่ได้เปิดแผนลงทุนระยะยาว จึงไม่ได้จัดแผนสะสมหุ้น"
        else:
            result = build_long_term_accumulation_plan(portfolio)
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=result),
        )
    except Exception:
        logger.exception("จัดทำหรือส่งแผนสะสมระยะยาวไม่สำเร็จ")
        try:
            line_bot_api.push_message(
                user_id,
                TextSendMessage(
                    text="⚠️ รอบนี้จัดทำแผนสะสมหุ้นไม่สำเร็จ "
                    "ระบบไม่ได้ส่งคำแนะนำซื้อ โปรดลองใหม่ภายหลัง"
                ),
            )
        except Exception:
            logger.exception("แจ้งผลการจัดทำแผนสะสมระยะยาวไม่สำเร็จ")
    finally:
        with _on_demand_lock:
            _long_term_users.discard(user_id)


def queue_long_term_recommendation(user_id):
    ensure_scheduler()
    with _on_demand_lock:
        if user_id in _long_term_users or user_id in _on_demand_users:
            return False
        _long_term_users.add(user_id)
    try:
        job_id = "long-term-" + hashlib.sha256(
            user_id.encode("utf-8")
        ).hexdigest()[:20]
        scheduler.add_job(
            _prepare_long_term_recommendation,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=1),
            args=[user_id],
            id=job_id,
            replace_existing=True,
            max_instances=1,
        )
    except Exception:
        with _on_demand_lock:
            _long_term_users.discard(user_id)
        raise
    return True


def help_text():
    return (
        "📊 PORTFOLIO BOT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📈 แผนหุ้นและการลงทุน\n"
        "ตั้งแผนลงทุนระยะยาว — ตั้งเป้าหมายหุ้นไทย 30% "
        "และเงินสด/ความเสี่ยงต่ำ 70%\n"
        "แนะนำหุ้นสะสม — คัดหุ้นเข้าพอร์ต (เรียกทบทวนเดือนละครั้ง)\n"
        "ตรวจพอร์ตระยะยาว — ดูสัดส่วนและกำไร/ขาดทุนที่ยังไม่รับรู้\n"
        "ดูแนวทางลงทุน — ดูเป้าหมายที่บันทึกไว้\n"
        "แผนวันนี้ / ควรซื้อวันนี้ — แผนซื้อขายรายวัน "
        "(หยุดส่งเมื่อเปิดโหมดระยะยาว)\n"
        "สถานะแผน — ดูวันที่ข้อมูลและสถานะเตรียมแผน\n\n"
        "💼 พอร์ตและรายการติดตาม\n"
        "ดูพอร์ต — ดูเงินสดและหุ้นที่บอตบันทึก\n"
        "สถานะข้อมูล — ดูว่าวิธีเก็บพอร์ตถาวรหรือไม่\n"
        "สำรองพอร์ต — รับ JSON สำหรับบันทึกเป็นไฟล์ .json\n"
        "กู้คืน — ส่งไฟล์สำรอง .json กลับเข้าแชต\n"
        "ตั้งเงินสด 10000 — บันทึกยอดเงินสดตามบัญชีจริง\n"
        "ตั้งหุ้น AOT 100 35.50 — บันทึกจำนวนและต้นทุนเฉลี่ย\n"
        "ตั้งหุ้น AOT 0 0 — ลบหุ้นที่ขายหมดออกจากพอร์ต\n"
        "รายการติดตาม — ดูหุ้นที่ติดตาม\n"
        "วิเคราะห์ COM7 — ดูแนวโน้มราคา"
        "และข้อมูลงบรายตัว\n"
        "ติดตาม AOT — เพิ่มหุ้นในรายการ"
        f"และใช้พิจารณาแผน (สูงสุด {MAX_WATCHLIST} หุ้น)\n"
        "เลิกติดตาม AOT — นำหุ้นออกจากรายการ\n\n"
        "❔ เมนูคำสั่ง\n"
        "พิมพ์: เมนู หรือ ช่วยเหลือ\n\n"
        "โหมดระยะยาวเป็นเป้าหมายเริ่มต้น ไม่รับประกันขาดทุนไม่เกิน 10% "
        "และไม่ส่งคำสั่งซื้อขายจริง "
        "ข้อมูลราคาพอร์ตอิงข้อมูลจากผู้ให้บริการภายนอก "
        "และต้องบันทึกเงินสด/หุ้นใหม่หลังซื้อขายที่โบรกเกอร์"
    )


def command_reply(user_id, text):
    if text in {
        "เมนู",
        "ช่วยเหลือ",
        "คำสั่ง",
        "menu",
        "help",
    }:
        return help_text()

    if text.startswith("วิเคราะห์ "):
        symbol = normalize_stock_symbol(text[len("วิเคราะห์ "):])
        if symbol is None:
            return (
                "รูปแบบชื่อหุ้นไม่ถูกต้อง "
                "ลองพิมพ์: วิเคราะห์ COM7"
            )

        frame = get_stock_history(
            symbol, period="6mo"
        )
        lines = [
            f"📊 วิเคราะห์ {display_stock_symbol(symbol)}",
            "━━━━━━━━━━━━━━━━━━",
        ]
        if (
            frame is not None
            and not frame.empty
        ):
            data_date = (
                frame.index[-1]
                .strftime("%Y-%m-%d")
            )
            values = _close_and_sma20(
                frame, data_date
            )
            if values:
                close, sma20 = values
                lines.append(
                    f"📅 ราคาปิด {data_date}: "
                    f"{close:.2f} บาท"
                )
                lines.append(
                    f"📈 เทียบ SMA20 "
                    f"{sma20:.2f}: "
                    + (
                        "อยู่เหนือแนวโน้มเฉลี่ย"
                        if close > sma20
                        else "อยู่ต่ำกว่า"
                        "แนวโน้มเฉลี่ย"
                    )
                )
            metrics = _selection_metrics(
                frame, data_date
            )
            if metrics:
                (
                    _,
                    momentum,
                    volatility,
                    avg_value,
                ) = metrics
                lines.append(
                    "📐 ผลตอบแทน 63 วัน: "
                    f"{momentum * 100:+.1f}%"
                )
                lines.append(
                    "ความผันผวนต่อปี: "
                    f"{volatility * 100:.1f}%"
                )
                lines.append(
                    "มูลค่าซื้อขายมัธยฐาน: "
                    f"{avg_value:,.0f} บาท/วัน"
                )
            else:
                lines.append(
                    "ข้อมูลราคา/ปริมาณซื้อขาย"
                    "ย้อนหลังยังไม่ครบสำหรับคำนวณ"
                )
        else:
            lines.append(
                "ไม่พบข้อมูลราคาล่าสุด"
                "จากผู้ให้บริการ"
            )

        fundamental = analyze_fundamentals(
            symbol
        )
        lines.append("")
        lines.append(
            "🏢 ข้อมูลงบรายไตรมาส"
        )
        lines.append(
            fundamental.get(
                "summary",
                "ข้อมูลพื้นฐานไม่พร้อม",
            )
        )
        lines.append("")
        lines.append(
            "หมายเหตุ: เป็นข้อมูลประกอบ "
            "ไม่ใช่คำสั่งซื้อขาย"
        )
        return "\n".join(lines)

    portfolio = get_portfolio(user_id)

    if text == "ตั้งแผนลงทุนระยะยาว":
        def enable_long_term(p):
            p["investment_strategy"] = {
                "style": "LONG_TERM",
                "starting_capital": 10000.0,
                "max_temporary_loss_pct": 10.0,
                "target_equity_pct": 30.0,
                "target_cash_pct": 70.0,
                "market_scope": "TH",
                "review_frequency": "monthly",
                "updated_at": utc_text(),
            }
            p["overnight_plan"] = None
            return (
                "✅ บันทึกแนวทางลงทุนระยะยาวแล้ว\n"
                "เงินตั้งต้นอ้างอิง 10,000 บาท\n"
                "เป้าหมายเบื้องต้น: หุ้นไทย 30% (3,000 บาท) "
                "และเงินสด/สินทรัพย์ความเสี่ยงต่ำ 70% (7,000 บาท)\n"
                "ปิดแผนซื้อขายรายวันและการแจ้งหุ้นรายเช้า "
                "ทบทวนด้วยคำสั่ง ตรวจพอร์ตระยะยาว เดือนละครั้ง\n"
                "⚠️ สัดส่วนนี้เป็นกรอบตั้งต้น ไม่รับประกันขาดทุนไม่เกิน 10% "
                "บอตไม่ซื้อขายแทนและไม่ทราบสินทรัพย์นอกพอร์ตที่บันทึก"
            )

        return mutate_portfolio(user_id, enable_long_term)

    if text == "ยกเลิกแผนลงทุนระยะยาว":
        def disable_long_term(p):
            p.pop("investment_strategy", None)
            p["overnight_plan"] = None
            return (
                "ปิดโหมดลงทุนระยะยาวแล้ว "
                "แผนรายวันจะกลับมาทำงานตามรอบปกติ "
                "เมื่อมีข้อมูลตลาดเพียงพอ"
            )

        return mutate_portfolio(user_id, disable_long_term)

    if text == "แนะนำหุ้นสะสม":
        strategy = portfolio.get("investment_strategy")
        if not isinstance(strategy, dict) or strategy.get("style") != "LONG_TERM":
            return (
                "ยังไม่ได้เปิดแผนลงทุนระยะยาว "
                "พิมพ์ ตั้งแผนลงทุนระยะยาว ก่อน"
            )
        try:
            started = queue_long_term_recommendation(user_id)
        except Exception:
            logger.exception("เริ่มจัดแผนสะสมหุ้นไม่สำเร็จ")
            return "ยังเริ่มจัดแผนสะสมหุ้นไม่ได้ โปรดลองอีกครั้งภายหลัง"
        if not started:
            return (
                "กำลังวิเคราะห์คำขอล่าสุดอยู่ "
                "เมื่อเสร็จระบบจะส่งผลให้ใน LINE"
            )
        return (
            "รับคำขอจัดแผนสะสมหุ้นไทยแล้ว "
            "กำลังตรวจข้อมูลราคาและงบพื้นฐานของ SET100 "
            "เมื่อเสร็จระบบจะส่งผลในแชตนี้ "
            "(อาจใช้เวลาสักครู่)"
        )

    if text == "ดูแนวทางลงทุน":
        strategy = portfolio.get("investment_strategy")
        if not isinstance(strategy, dict) or strategy.get("style") != "LONG_TERM":
            return (
                "ยังไม่ได้ตั้งแนวทางระยะยาว "
                "พิมพ์ ตั้งแผนลงทุนระยะยาว เพื่อเริ่มต้น"
            )
        return (
            "📌 แนวทางลงทุนที่บันทึกไว้\n"
            "ตลาด: หุ้นไทย\n"
            f"เงินตั้งต้นอ้างอิง: {strategy.get('starting_capital', 10000):,.0f} บาท\n"
            f"หุ้นเป้าหมาย: {strategy.get('target_equity_pct', 30):.0f}% | "
            f"เงินสด/เสี่ยงต่ำ: {strategy.get('target_cash_pct', 70):.0f}%\n"
            f"ระดับขาดทุนชั่วคราวที่ตั้งใจรับ: "
            f"{strategy.get('max_temporary_loss_pct', 10):.0f}%\n"
            "รอบทบทวน: รายเดือน"
        )

    if text == "ตรวจพอร์ตระยะยาว":
        strategy = portfolio.get("investment_strategy")
        if not isinstance(strategy, dict) or strategy.get("style") != "LONG_TERM":
            return (
                "ยังไม่ได้เปิดแนวทางระยะยาว "
                "พิมพ์ ตั้งแผนลงทุนระยะยาว เพื่อเริ่มต้น"
            )

        cash = float(portfolio.get("cash", 0) or 0)
        holdings = portfolio.get("stocks", {})
        total_cost = sum(
            float(info.get("shares", 0)) * float(info.get("avg_price", 0))
            for info in holdings.values()
        )
        total_market = 0.0
        holding_lines = []
        for symbol, info in holdings.items():
            shares = int(info.get("shares", 0))
            avg_price = float(info.get("avg_price", 0))
            if shares <= 0 or avg_price <= 0:
                continue
            frame = get_stock_history(symbol, period="6mo")
            latest = None
            if frame is not None and not frame.empty:
                day = frame.index[-1].strftime("%Y-%m-%d")
                values = _close_and_sma20(frame, day)
                if values:
                    latest = (day, values[0])
            if latest is None:
                market_value = shares * avg_price
                total_market += market_value
                holding_lines.append(
                    f"• {display_stock_symbol(symbol)}: ไม่มีราคาล่าสุด; แสดงตามต้นทุน "
                    f"{market_value:,.2f} บาท (ไม่ใช่มูลค่าตลาด)"
                )
                continue
            day, price = latest
            market_value = shares * price
            pnl = market_value - (shares * avg_price)
            total_market += market_value
            holding_lines.append(
                f"• {display_stock_symbol(symbol)}: {shares:,} หุ้น | ปิด {day} "
                f"{price:.2f} บาท | กำไร/ขาดทุนค้าง "
                f"{pnl:+,.2f} บาท ({pnl / (shares * avg_price) * 100:+.1f}%)"
            )

        total_value = cash + total_market
        target_equity_pct = float(strategy.get("target_equity_pct", 30))
        target_equity = total_value * target_equity_pct / 100
        actual_equity_pct = total_market / total_value * 100 if total_value > 0 else 0
        lines = [
            "🧭 ทบทวนพอร์ตระยะยาว",
            "━━━━━━━━━━━━━━━━━━",
            f"เงินสดตามบันทึก: {cash:,.2f} บาท",
            f"มูลค่าหุ้นตามราคาล่าสุดที่หาได้: {total_market:,.2f} บาท",
            f"รวมที่ประเมินได้: {total_value:,.2f} บาท",
            f"สัดส่วนหุ้นปัจจุบันโดยประมาณ: {actual_equity_pct:.1f}%",
            f"กรอบหุ้น {target_equity_pct:.0f}%: ประมาณ {target_equity:,.2f} บาท",
            "",
        ]
        if holding_lines:
            lines.extend(holding_lines)
        else:
            lines.append("ยังไม่มีหุ้นในพอร์ตที่บอตบันทึก")
        lines.extend([
            "",
            "ทบทวนเดือนละครั้งก็เพียงพอสำหรับแนวทางนี้",
            "ข้อมูลนี้ใช้เฉพาะเงินสด/หุ้นที่บันทึกในบอต "
            "และไม่ใช่การคำนวณ drawdown ของทั้งบัญชี",
            "การจำกัดขาดทุน 10% ทำไม่ได้ด้วยการเตือนอย่างเดียว "
            "ราคาหุ้นอาจลดเกินระดับดังกล่าวได้",
        ])
        return "\n".join(lines)

    if text == "สถานะข้อมูล":
        storage_name, storage_detail = portfolio_storage_status()
        return (
            "🗄️ สถานะการเก็บข้อมูลพอร์ต\n"
            f"วิธีเก็บ: {storage_name}\n"
            f"รายละเอียด: {storage_detail}"
        )

    if text == "สำรองพอร์ต":
        try:
            backup = export_portfolio_backup(user_id)
        except Exception:
            logger.exception("สร้างไฟล์สำรองพอร์ตไม่สำเร็จ")
            return "⚠️ สร้างข้อมูลสำรองไม่สำเร็จ กรุณาลองใหม่ภายหลัง"
        return (
        "🔐 JSON สำรองพอร์ตของคุณ — คัดลอกทั้งหมดนี้บันทึกเป็นไฟล์ "
            "`portfolio-backup.json` เก็บไว้เป็นความลับ\n"
            "กู้คืนโดยส่งไฟล์ .json นี้เข้าแชตกับบอต "
            "(ไม่รวมประวัติรายการและแผนเก่า)\n\n"
            f"{backup}"
        )

    if text in {
        "แผนเทรดวันนี้",
        "แผนวันนี้",
        "ควรซื้อวันนี้",
        "ซื้อวันนี้",
    }:
        if portfolio.get("investment_strategy", {}).get("style") == "LONG_TERM":
            return (
                "ℹ️ เปิดแผนลงทุนระยะยาวอยู่ "
                "จึงปิดคำแนะนำซื้อขายรายวันไว้\n"
                "พิมพ์ ตรวจพอร์ตระยะยาว เพื่อทบทวนพอร์ต "
                "หรือ ยกเลิกแผนลงทุนระยะยาว เพื่อกลับไปใช้แผนรายวัน"
            )
        return read_overnight_plan(
            user_id, retry_no_data=True
        )

    if text == "สถานะแผน":
        plan = portfolio.get("overnight_plan")
        if not isinstance(plan, dict):
            return (
                "ℹ️ ยังไม่มีประวัติการเตรียมแผน\n"
                "ระบบจะเตรียมแผนในรอบหลังตลาดปิด "
                "เมื่อมีข้อมูลราคาเพียงพอ"
            )
        status = plan.get("status", "UNKNOWN")
        if status == "NO_DATA":
            return read_overnight_plan(user_id)
        if status == "READY":
            return (
                "✅ เตรียมแผนแล้ว\n"
                f"วันที่ข้อมูล: {plan.get('data_date', 'ไม่ทราบ')}\n"
                f"วันใช้แผน: {plan.get('delivery_day', 'ไม่ทราบ')}\n"
                f"เวลาเตรียม: {plan.get('prepared_at', 'ไม่ทราบ')}"
            )
        if status in ("SENT", "DELIVERY_UNKNOWN"):
            delivery_note = (
                "ส่งแล้ว"
                if status == "SENT"
                else "ยังยืนยันผลการส่งไม่ได้"
            )
            return (
                f"📨 สถานะแผน: {delivery_note}\n"
                f"วันที่ข้อมูล: {plan.get('data_date', 'ไม่ทราบ')}\n"
                f"วันใช้แผน: {plan.get('delivery_day', 'ไม่ทราบ')}"
            )
        return (
            f"สถานะแผน: {status}\n"
            f"วันที่ข้อมูล: {plan.get('data_date', 'ไม่ทราบ')}"
        )

    if text == "ดูพอร์ต":
        cash = portfolio["cash"]
        stocks = portfolio["stocks"]
        total_stock_cost = sum(
            stock["shares"]
            * stock["avg_price"]
            for stock in stocks.values()
        )
        total_value = (
            cash + total_stock_cost
        )
        lines = [
            "💼 สรุปพอร์ต",
            "━━━━━━━━━━━━━━━━━━",
            f"💵 เงินสด: "
            f"{cash:,.2f} บาท",
            "📦 มูลค่าหุ้นตามต้นทุนเฉลี่ย: "
            f"{total_stock_cost:,.2f} บาท",
            "💰 รวมตามข้อมูลที่บันทึก: "
            f"{total_value:,.2f} บาท",
            "",
        ]
        if stocks:
            lines.append(
                "📌 หุ้นที่บันทึกไว้"
            )
            for sym, info in (
                stocks.items()
            ):
                lines.append(
                    f"• {display_stock_symbol(sym)}  |  "
                    f"{info['shares']} หุ้น  |  "
                    "ต้นทุนเฉลี่ย "
                    f"{info['avg_price']:.2f} บาท"
                )
        else:
            lines.append(
                "📌 ยังไม่มีหุ้นที่บันทึกไว้"
            )

        lines.extend(
            [
                "",
                "หมายเหตุ: มูลค่าหุ้น"
                "คำนวณจากต้นทุนเฉลี่ย "
                "ไม่ใช่ราคาตลาดปัจจุบัน",
            ]
        )
        return "\n".join(lines)

    if text.startswith("ตั้งเงินสด "):
        parts = text.split()
        if len(parts) != 2:
            return "รูปแบบ: ตั้งเงินสด 10000"
        try:
            amount = float(parts[1].replace(",", ""))
        except ValueError:
            return "ยอดเงินสดต้องเป็นตัวเลขตั้งแต่ 0 บาท"
        if not np.isfinite(amount) or amount < 0:
            return "ยอดเงินสดต้องเป็นตัวเลขตั้งแต่ 0 บาท"

        def set_cash(p):
            p["cash"] = amount
            p["overnight_plan"] = None
            p.setdefault("history", []).append({
                "type": "MANUAL_CASH_BALANCE",
                "cash": amount,
                "recorded_at": utc_text(),
            })
            return (
                f"บันทึกเงินสด {amount:,.2f} บาทแล้ว "
                "(เป็นข้อมูลพอร์ตในบอต ไม่ใช่การฝากเงินจริง) "
                "แผนเดิมถูกยกเลิกและจะเตรียมใหม่หลังตลาดปิด"
            )

        return mutate_portfolio(user_id, set_cash)

    if text.startswith("ตั้งหุ้น "):
        parts = text.split()
        if len(parts) != 4:
            return "รูปแบบ: ตั้งหุ้น AOT 100 35.50 (จำนวนและต้นทุนเฉลี่ย)"
        symbol = normalize_stock_symbol(parts[1])
        try:
            shares = int(parts[2])
            avg_price = float(parts[3].replace(",", ""))
        except ValueError:
            return "จำนวนหุ้นต้องเป็นจำนวนเต็ม และต้นทุนเฉลี่ยต้องเป็นตัวเลข"
        if (
            symbol is None or shares < 0 or shares > 1_000_000_000
            or not np.isfinite(avg_price)
            or (shares > 0 and avg_price <= 0)
            or (shares == 0 and avg_price != 0)
        ):
            return "ข้อมูลหุ้นไม่ถูกต้อง ลองพิมพ์: ตั้งหุ้น AOT 100 35.50"

        def set_holding(p):
            holdings = p.setdefault("stocks", {})
            if shares:
                holdings[symbol] = {
                    "shares": shares,
                    "avg_price": avg_price,
                }
            else:
                holdings.pop(symbol, None)
            p["overnight_plan"] = None
            p.setdefault("history", []).append({
                "type": "MANUAL_HOLDING_BALANCE",
                "symbol": symbol,
                "shares": shares,
                "avg_price": avg_price,
                "recorded_at": utc_text(),
            })
            return (
                f"บันทึก {display_stock_symbol(symbol)} {shares:,} หุ้นแล้ว "
                "(เป็นข้อมูลพอร์ตในบอต ไม่ใช่คำสั่งซื้อขายจริง) "
                "แผนเดิมถูกยกเลิกและจะเตรียมใหม่หลังตลาดปิด"
            )

        return mutate_portfolio(user_id, set_holding)

    if text == "รายการติดตาม":
        watchlist = portfolio.get(
            "watchlist", []
        )
        if not watchlist:
            return (
                "🔎 รายการติดตาม"
                "ยังว่างอยู่\n"
                "ลองพิมพ์: ติดตาม AOT"
            )
        return (
            "🔎 หุ้นที่ติดตาม\n"
            + "\n".join(
                f"• {display_stock_symbol(symbol)}"
                for symbol in watchlist
            )
        )

    if text.startswith("ติดตาม "):
        sym = normalize_stock_symbol(text[len("ติดตาม "):])
        if sym is None:
            return "รูปแบบชื่อหุ้นไม่ถูกต้อง ลองพิมพ์: ติดตาม AOT"

        def add_w(p):
            if "watchlist" not in p:
                p["watchlist"] = []
            if sym not in p["watchlist"] and len(p["watchlist"]) >= MAX_WATCHLIST:
                return f"รายการติดตามเต็มแล้ว (สูงสุด {MAX_WATCHLIST} หุ้น)"
            if (
                sym
                not in p["watchlist"]
            ):
                p["watchlist"].append(
                    sym
                )
                p["overnight_plan"] = None
                return (
                    f"เพิ่ม {display_stock_symbol(sym)} "
                    "ลงในรายการติดตามแล้ว "
                    "แผนเดิมถูกยกเลิกและจะเตรียมใหม่หลังตลาดปิด"
                )
            return (
                f"{display_stock_symbol(sym)} อยู่ในรายการ"
                "ติดตามอยู่แล้ว"
            )

        return mutate_portfolio(
            user_id, add_w
        )

    if text.startswith(
        "เลิกติดตาม "
    ):
        sym = normalize_stock_symbol(text[len("เลิกติดตาม "):])
        if sym is None:
            return "รูปแบบชื่อหุ้นไม่ถูกต้อง ลองพิมพ์: เลิกติดตาม AOT"

        def remove_w(p):
            if sym in p.get(
                "watchlist", []
            ):
                p["watchlist"].remove(
                    sym
                )
                p["overnight_plan"] = None
                return (
                    f"นำ {display_stock_symbol(sym)} ออกจาก"
                    "รายการติดตามแล้ว "
                    "แผนเดิมถูกยกเลิกและจะเตรียมใหม่หลังตลาดปิด"
                )
            return (
                f"ไม่พบ {display_stock_symbol(sym)} "
                "ในรายการติดตาม"
            )

        return mutate_portfolio(
            user_id, remove_w
        )

    return (
        "ไม่รู้จักคำสั่งนี้"
        + "\n\n"
        + help_text()
    )


# =========================================================
# SCHEDULER STARTUP
# =========================================================
scheduler = None
_scheduler_pid = None
_scheduler_lock = threading.Lock()


def ensure_scheduler():
    """Start jobs inside the serving worker, after Gunicorn forks."""
    global scheduler, _scheduler_pid
    pid = os.getpid()
    if scheduler is not None and _scheduler_pid == pid and scheduler.running:
        return
    with _scheduler_lock:
        if scheduler is not None and _scheduler_pid == pid and scheduler.running:
            return
        worker_scheduler = BackgroundScheduler(timezone="Asia/Bangkok")
        worker_scheduler.add_job(
            prepare_overnight_plans,
            "cron",
            day_of_week="mon-fri",
            hour="19,20,21",
            minute=15,
            id="prepare-overnight-plan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        worker_scheduler.add_job(
            prepare_overnight_plans,
            "cron",
            day_of_week="mon-fri",
            hour=8,
            minute="15,20,25",
            id="prepare-overnight-plan-morning-retry",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        worker_scheduler.add_job(
            send_overnight_plans,
            "cron",
            day_of_week="mon-fri",
            hour=8,
            minute="30,35,40,45,50,55",
            id="send-overnight-plan-8",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        worker_scheduler.add_job(
            send_overnight_plans,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute="0,5",
            id="send-overnight-plan-9",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        worker_scheduler.start()
        scheduler = worker_scheduler
        _scheduler_pid = pid
        logger.info("เริ่ม scheduler ใน worker pid=%d", pid)


@app.before_request
def start_scheduler_in_worker():
    try:
        ensure_scheduler()
    except Exception:
        logger.exception("เริ่ม scheduler ใน worker ไม่สำเร็จ")

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
    )

