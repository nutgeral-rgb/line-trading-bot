from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import copy
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
from long_term_review import HORIZONS, assess_holding, evaluate_recommendations
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
# การรันในเครื่องใช้ไฟล์ได้ แต่บน Render ต้องตั้ง DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORTFOLIO_FILE_PATH = os.getenv("PORTFOLIO_FILE_PATH", "").strip()
if not DATABASE_URL and not PORTFOLIO_FILE_PATH:
    logger.warning(
        "ยังไม่ได้ตั้ง DATABASE_URL; บน Render จะงดบันทึกพอร์ต "
        "จนกว่าจะเชื่อมฐานข้อมูลถาวร"
    )
PORTFOLIO_FILE = Path(
    PORTFOLIO_FILE_PATH or "portfolio.json"
)
ON_RENDER = bool(os.getenv("RENDER"))
def portfolio_storage_status():
    """Describe the configured portfolio storage without exposing secrets."""
    if DATABASE_URL:
        try:
            with _connect_portfolio_database() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception:
            logger.exception("ตรวจการเชื่อมฐานข้อมูลพอร์ตไม่สำเร็จ")
            return ("PostgreSQL ยังไม่พร้อม", "ตรวจ DATABASE_URL และสถานะฐานข้อมูล")
        return (
            "PostgreSQL",
            "เชื่อมต่อได้และบันทึกนอกไฟล์ชั่วคราวของ Web Service",
        )
    if ON_RENDER:
        return (
            "ยังไม่เปิดการบันทึกบน Render",
            "ตั้ง DATABASE_URL ให้เชื่อม PostgreSQL ภายนอกก่อนใช้พอร์ต",
        )
    if PORTFOLIO_FILE_PATH:
        return ("ไฟล์ตาม path ที่กำหนด", str(PORTFOLIO_FILE))
    return ("ไฟล์ local", str(PORTFOLIO_FILE))


_portfolio_lock = threading.RLock()
_portfolio_database_lock = threading.Lock()
_portfolio_database_ready = False

UNIVERSE = SET100_SYMBOLS


def set100_universe_warning(as_of=None):
    """Fail closed when the bundled index membership is outside its term."""
    day = (as_of or now_bkk().date()).isoformat()
    if SET100_EFFECTIVE_FROM <= day <= SET100_EFFECTIVE_TO:
        return None
    return (
        f"⚠️ รายชื่อ SET100 ในบอตใช้ได้เฉพาะ {SET100_EFFECTIVE_FROM} "
        f"ถึง {SET100_EFFECTIVE_TO}; วันนี้ {day} จึงงดคัดหุ้นใหม่ "
        "จนกว่าจะอัปเดตรายชื่อจากตลาดหลักทรัพย์"
    )

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
try:
    ESTIMATED_BUY_COST_RATE = float(os.getenv("ESTIMATED_BUY_COST_RATE", "0.004"))
except (TypeError, ValueError):
    ESTIMATED_BUY_COST_RATE = 0.004
if not np.isfinite(ESTIMATED_BUY_COST_RATE) or not 0 <= ESTIMATED_BUY_COST_RATE <= 0.05:
    ESTIMATED_BUY_COST_RATE = 0.004


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
        # A plan amount is not a deposit. Start with zero until LINE records
        # the actual balance, so a new user never sees fictitious buying power.
        "cash": 0.0,
        "stocks": {},
        "history": [],
        "watchlist": [],
        "long_term_recommendations": [],
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
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS line_bot_portfolio_revisions (
                            revision_id BIGSERIAL PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            portfolio JSONB NOT NULL,
                            saved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                    # Import a local file only while the database is empty.
                    # Existing database rows always take precedence.
                    has_rows = connection.execute(
                        "SELECT EXISTS (SELECT 1 FROM line_bot_portfolios)"
                    ).fetchone()[0]
                    if not has_rows:
                        previous_data = _load_portfolios_file_unlocked()
                        if previous_data:
                            connection.executemany(
                                """
                                INSERT INTO line_bot_portfolios (user_id, portfolio)
                                VALUES (%s, %s)
                                ON CONFLICT (user_id) DO NOTHING
                                """,
                                [
                                    (user_id, Jsonb(portfolio))
                                    for user_id, portfolio in previous_data.items()
                                ],
                            )
                    connection.commit()
                    _portfolio_database_ready = True
        except Exception:
            connection.close()
            raise
    return connection


def _load_all_portfolios_unlocked():
    if not DATABASE_URL:
        if ON_RENDER:
            raise RuntimeError("Render ต้องมี DATABASE_URL เพื่อเก็บพอร์ตถาวร")
        return _load_portfolios_file_unlocked()

    with _connect_portfolio_database() as connection:
        rows = connection.execute(
            "SELECT user_id, portfolio FROM line_bot_portfolios"
        ).fetchall()
        return {user_id: portfolio for user_id, portfolio in rows}


def load_all_portfolios():
    with _portfolio_lock:
        return _load_all_portfolios_unlocked()


def _save_all_portfolios_unlocked(data):
    if DATABASE_URL:
        raise RuntimeError("ใช้ mutate_portfolio สำหรับการแก้ไขพอร์ตในฐานข้อมูล")

    if ON_RENDER:
        raise RuntimeError("Render ต้องมี DATABASE_URL เพื่อเก็บพอร์ตถาวร")

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
                "investment_strategy": (
                    {
                        "style": "LONG_TERM",
                        "starting_capital": portfolio.get(
                            "investment_strategy", {}
                        ).get("starting_capital", 6000.0),
                        "target_equity_pct": 100.0,
                        "target_cash_pct": 0.0,
                        "allocation_version": 2,
                        "monthly_contribution_baht": portfolio.get(
                            "investment_strategy", {}
                        ).get("monthly_contribution_baht", 500.0),
                    }
                    if isinstance(portfolio.get("investment_strategy"), dict)
                    and portfolio.get("investment_strategy", {}).get("style") == "LONG_TERM"
                    else None
                ),
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
    strategy = portfolio.get("investment_strategy")
    if strategy is not None:
        if not isinstance(strategy, dict) or strategy.get("style") != "LONG_TERM":
            raise ValueError("แผนลงทุนในไฟล์สำรองไม่ถูกต้อง")
        contribution = strategy.get("monthly_contribution_baht", 500.0)
        starting_capital = strategy.get("starting_capital", 6000.0)
        if (
            isinstance(contribution, bool)
            or not isinstance(contribution, (int, float))
            or not np.isfinite(contribution)
            or not 500 <= contribution <= 1000
        ):
            raise ValueError("ยอดเงินเติมรายเดือนในไฟล์สำรองต้องอยู่ระหว่าง 500–1,000 บาท")
        if (
            isinstance(starting_capital, bool)
            or not isinstance(starting_capital, (int, float))
            or not np.isfinite(starting_capital)
            or starting_capital < 0
        ):
            raise ValueError("เงินตั้งต้นอ้างอิงในไฟล์สำรองไม่ถูกต้อง")
        restored["investment_strategy"] = {
            "style": "LONG_TERM",
            "starting_capital": float(starting_capital),
            "max_temporary_loss_pct": 10.0,
            "target_equity_pct": 100.0,
            "target_cash_pct": 0.0,
            "allocation_version": 2,
            "monthly_contribution_baht": float(contribution),
            "market_scope": "TH",
            "review_frequency": "on_demand",
        }
    try:
        json.dumps(restored, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("มีข้อมูลบางส่วนในไฟล์สำรองที่ใช้ไม่ได้") from exc
    return restored


def get_portfolio(user_id):
    if DATABASE_URL:
        with _connect_portfolio_database() as connection:
            connection.execute(
                """
                INSERT INTO line_bot_portfolios (user_id, portfolio)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id, Jsonb(_new_portfolio())),
            )
            portfolio = connection.execute(
                "SELECT portfolio FROM line_bot_portfolios WHERE user_id = %s FOR UPDATE",
                (user_id,),
            ).fetchone()[0]
            strategy = portfolio.get("investment_strategy")
            try:
                version = (
                    int(strategy.get("allocation_version", 0) or 0)
                    if isinstance(strategy, dict) else 0
                )
            except (TypeError, ValueError, OverflowError):
                version = 0
            if isinstance(strategy, dict) and strategy.get("style") == "LONG_TERM" and version < 2:
                strategy.update({
                    "target_equity_pct": 100.0,
                    "target_cash_pct": 0.0,
                    "allocation_version": 2,
                    "updated_at": utc_text(),
                })
                portfolio["overnight_plan"] = None
                connection.execute(
                    """
                    UPDATE line_bot_portfolios
                    SET portfolio = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s
                    """,
                    (Jsonb(portfolio), user_id),
                )
            return portfolio

    with _portfolio_lock:
        all_p = _load_all_portfolios_unlocked()
        if user_id not in all_p:
            all_p[user_id] = _new_portfolio()
            _save_all_portfolios_unlocked(all_p)
        portfolio = all_p[user_id]
        strategy = portfolio.get("investment_strategy")
        try:
            allocation_version = int(strategy.get("allocation_version", 0) or 0) if isinstance(strategy, dict) else 0
        except (TypeError, ValueError, OverflowError):
            allocation_version = 0
        if (
            isinstance(strategy, dict)
            and strategy.get("style") == "LONG_TERM"
            and allocation_version < 2
        ):
            migrated = dict(strategy)
            migrated.update({
                "target_equity_pct": 100.0,
                "target_cash_pct": 0.0,
                "allocation_version": 2,
                "updated_at": utc_text(),
            })
            portfolio["investment_strategy"] = migrated
            portfolio["overnight_plan"] = None
            _save_all_portfolios_unlocked(all_p)
        return portfolio


def mutate_portfolio(user_id, func):
    if DATABASE_URL:
        # Lock only this user. Updating every user's JSON on each command can
        # overwrite another request, even when the database itself is durable.
        with _connect_portfolio_database() as connection:
            connection.execute(
                """
                INSERT INTO line_bot_portfolios (user_id, portfolio)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO NOTHING
                """,
                (user_id, Jsonb(_new_portfolio())),
            )
            portfolio = connection.execute(
                """
                SELECT portfolio FROM line_bot_portfolios
                WHERE user_id = %s FOR UPDATE
                """,
                (user_id,),
            ).fetchone()[0]
            previous = copy.deepcopy(portfolio)
            result = func(portfolio)
            if portfolio != previous:
                connection.execute(
                    """
                    INSERT INTO line_bot_portfolio_revisions (user_id, portfolio)
                    VALUES (%s, %s)
                    """,
                    (user_id, Jsonb(previous)),
                )
                connection.execute(
                    """
                    UPDATE line_bot_portfolios
                    SET portfolio = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = %s
                    """,
                    (Jsonb(portfolio), user_id),
                )
                connection.execute(
                    """
                    DELETE FROM line_bot_portfolio_revisions
                    WHERE user_id = %s AND revision_id NOT IN (
                        SELECT revision_id FROM line_bot_portfolio_revisions
                        WHERE user_id = %s ORDER BY revision_id DESC LIMIT 30
                    )
                    """,
                    (user_id, user_id),
                )
            return result

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


def get_market_frames(symbols, period="6mo"):
    unique_symbols = sorted(set(symbols))
    if not unique_symbols:
        return {}

    now = time.monotonic()
    frames = {}
    symbols_to_fetch = []
    with _market_data_cache_lock:
        for sym in unique_symbols:
            cached = _market_data_cache.get((sym, period))
            if cached and now - cached[0] < _MARKET_DATA_CACHE_TTL_SECONDS:
                frames[sym] = cached[1].copy(deep=True)
            else:
                symbols_to_fetch.append(sym)

    def fetch_one(sym):
        return sym, get_stock_history(sym, period=period)

    short_history = []
    # Keep provider load modest; failed symbols are retried on the next request.
    worker_count = min(2, len(symbols_to_fetch))
    if symbols_to_fetch:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for sym, df in executor.map(fetch_one, symbols_to_fetch):
                if df is not None and len(df) >= 20:
                    frames[sym] = df
                    with _market_data_cache_lock:
                        _market_data_cache[(sym, period)] = (
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
    universe_warning = set100_universe_warning()
    if universe_warning:
        return universe_warning, []
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

    universe_warning = set100_universe_warning()
    if universe_warning:
        logger.warning("ข้ามแผนรายวัน: รายชื่อ SET100 หมดอายุ")
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


def build_long_term_accumulation_plan(portfolio, recommendation_snapshots=None):
    """Screen SET100 candidates for a cautious, user-triggered accumulation review."""
    universe_warning = set100_universe_warning()
    if universe_warning:
        return universe_warning
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
    # Six calendar months often contain fewer than the 127 trading closes
    # required by the trend screen. Fetch one year for this plan.
    frames = get_market_frames(symbols, period="1y")
    ctx = market_context(frames)
    if not ctx["sufficient"]:
        return (
            "⚠️ ข้อมูล SET100 ยังไม่ครบ 80% ในวันเดียวกัน "
            f"({ctx.get('valid_count', 0)}/{ctx.get('total_count', len(UNIVERSE))}) "
            "รอข้อมูลรอบถัดไปแล้วลองใหม่"
        )
    if ctx["date"] not in allowed_data_dates:
        return (
            "⚠️ ข้อมูลราคาล่าสุดเก่าเกิน 1 วันซื้อขาย "
            f"(ข้อมูล {ctx.get('date') or 'ไม่มีข้อมูล'}); รอบนี้งดแนะนำ"
        )

    cash = max(0.0, float(portfolio.get("cash", 0) or 0))
    holdings = portfolio.get("stocks", {})
    current_equity_value = 0.0
    holding_values = {}
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
        market_value = shares * float(close.iloc[-1])
        current_equity_value += market_value
        holding_values[symbol] = market_value

    total_value = cash + current_equity_value
    target_equity_pct = float(strategy.get("target_equity_pct", 100.0))
    target_equity = total_value * target_equity_pct / 100.0
    planned_contribution = float(
        strategy.get("monthly_contribution_baht", 500.0) or 500.0
    )
    remaining_target = max(0.0, target_equity - current_equity_value)
    budget = min(cash, remaining_target)

    header = [
        "🧭 แผนสะสมหุ้นระยะยาว | หุ้นเป้าหมาย 100%",
        f"📅 ราคาปิด {ctx['date']} (Yahoo Finance)",
        f"📊 หุ้น SET100 เหนือ SMA20 {ctx['breadth']:.1f}% "
        f"({ctx['valid_count']}/{ctx['total_count']})",
        f"💰 พอร์ตตามข้อมูลบอต {total_value:,.0f} | "
        f"เงินสดบันทึก {cash:,.0f} | งบรอบนี้ {budget:,.0f} บาท",
        f"📥 แผนเติมเงิน {planned_contribution:,.0f} บาท/เดือน",
        *(
            ["⚠️ ราคาปิดช้ากว่าวันซื้อขายล่าสุด 1 วัน"]
            if ctx["date"] != expected_session.isoformat()
            else []
        ),
        *(
            [
                "⚠️ หุ้นตัวใหญ่สุดคิดเป็น "
                f"{max(holding_values.values()) / current_equity_value * 100:.0f}% ของหุ้นในพอร์ต"
            ]
            if holding_values
            and current_equity_value > 0
            and max(holding_values.values()) / current_equity_value >= 0.4
            else []
        ),
    ]

    def concise_fundamental(fundamental):
        score = fundamental.get("score")
        required = int(fundamental.get("required", 5) or 5)
        available = int(fundamental.get("available", 0) or 0)
        if score is None:
            summary = f"งบยังไม่ครบ ({available}/{required})"
        else:
            summary = f"งบผ่าน {int(score)}/{required}"
        details_text = str(fundamental.get("summary") or "")
        details_text = details_text.partition("|")[2]
        details = [part.strip() for part in details_text.split(";")]
        for prefix, label in (
            ("รายได้ YoY", "รายได้"),
            ("กำไรบวก", "กำไร"),
            ("กระแสเงินสดดำเนินงาน", "CFO"),
            ("FCF 4 ไตรมาส", "FCF"),
        ):
            match = next((part for part in details if part.startswith(prefix)), None)
            if match:
                value = match[len(prefix):].strip()
                summary += f" · {label} {value}"
        warnings = fundamental.get("quality_warnings") or []
        if warnings:
            summary += " · ⚠️ " + str(warnings[0])
        elif int(fundamental.get("valuation_concern", 0) or 0) > 0:
            summary += " · ⚠️ ควรทบทวนมูลค่า"
        return summary

    def note_recommendations(items, odd_lot=False):
        if recommendation_snapshots is None:
            return
        for item in items:
            recommendation_snapshots.append({
                "signal_date": ctx["date"],
                "symbol": item["symbol"],
                "signal_close": item["close"],
                "fundamental_period": item["fundamental"].get("statement_period"),
                "estimated_odd_lot": odd_lot,
                "delivery_status": "PENDING",
            })

    if unpriced_holdings:
        return "\n".join(
            header
            + [
                "⚠️ ประเมินหุ้นในพอร์ตได้ไม่ครบ: "
                + ", ".join(unpriced_holdings[:5]),
                "รอบนี้งดแนะนำซื้อเพื่อป้องกันคำนวณสัดส่วนคลาดเคลื่อน",
            ]
        )
    if budget <= 0:
        return "\n".join(
            header
            + [
                "เงินสดหรือวงเงินสะสมไม่พอในรอบนี้ จึงงดแนะนำซื้อ",
                "อัปเดตยอดพอร์ตแล้วเรียกคำสั่งนี้ใหม่ได้",
            ]
        )

    market_returns = []
    for market_symbol in UNIVERSE:
        market_frame = frames.get(market_symbol)
        if market_frame is None or market_frame.empty:
            continue
        if market_frame.index[-1].strftime("%Y-%m-%d") != ctx["date"]:
            continue
        market_col = "Adj Close" if "Adj Close" in market_frame.columns else "Close"
        market_close = pd.to_numeric(market_frame[market_col], errors="coerce").dropna()
        if len(market_close) >= 127:
            value = float(market_close.iloc[-1] / market_close.iloc[-127] - 1)
            if np.isfinite(value):
                market_returns.append(value)
    set100_median_return = (
        float(np.median(market_returns)) if market_returns else None
    )

    def portfolio_correlation(candidate_symbol):
        candidate_frame = frames.get(candidate_symbol)
        if candidate_frame is None or candidate_frame.empty:
            return None
        candidate_col = "Adj Close" if "Adj Close" in candidate_frame.columns else "Close"
        candidate_close = pd.to_numeric(
            candidate_frame[candidate_col], errors="coerce"
        ).dropna()
        candidate_returns = candidate_close.pct_change().tail(60).dropna()
        if len(candidate_returns) < 30:
            return None
        weighted = []
        for held_symbol, market_value in holding_values.items():
            held_frame = frames.get(held_symbol)
            if held_frame is None or held_frame.empty or market_value <= 0:
                continue
            held_col = "Adj Close" if "Adj Close" in held_frame.columns else "Close"
            held_close = pd.to_numeric(held_frame[held_col], errors="coerce").dropna()
            held_returns = held_close.pct_change().tail(60).dropna()
            aligned = pd.concat(
                [candidate_returns.rename("candidate"), held_returns.rename("holding")],
                axis=1,
                join="inner",
            ).dropna()
            if len(aligned) < 30:
                continue
            correlation = float(aligned["candidate"].corr(aligned["holding"]))
            if np.isfinite(correlation):
                weighted.append((correlation, market_value))
        total_weight = sum(weight for _, weight in weighted)
        if total_weight <= 0:
            return None
        return sum(value * weight for value, weight in weighted) / total_weight

    eligible = []
    for symbol in UNIVERSE:
        if symbol in holdings:
            continue
        frame = frames.get(symbol)
        if frame is None or frame.empty or frame.index[-1].strftime("%Y-%m-%d") != ctx["date"]:
            continue
        close_col = "Close" if "Close" in frame.columns else "Adj Close"
        closes = pd.to_numeric(frame[close_col], errors="coerce").dropna()
        if len(closes) < 127 or closes.index[-1].strftime("%Y-%m-%d") != ctx["date"]:
            continue
        close = float(closes.iloc[-1])
        sma20 = float(closes.tail(20).mean())
        sma50 = float(closes.tail(50).mean())
        adjusted_col = "Adj Close" if "Adj Close" in frame.columns else close_col
        adjusted_closes = pd.to_numeric(frame[adjusted_col], errors="coerce").dropna()
        adjusted_closes = adjusted_closes.loc[adjusted_closes.index <= closes.index[-1]]
        if len(adjusted_closes) < 127:
            continue
        six_month_return = float(
            adjusted_closes.iloc[-1] / adjusted_closes.iloc[-127] - 1
        )
        six_month_history = adjusted_closes.tail(127)
        running_high = six_month_history.cummax()
        max_drawdown = float((six_month_history / running_high - 1).min())
        if (
            not np.isfinite(close) or not np.isfinite(sma20)
            or not np.isfinite(sma50) or not np.isfinite(six_month_return)
            or close <= sma50 or sma20 <= sma50 or six_month_return <= 0
        ):
            continue
        recent = closes.tail(20)
        volume = pd.to_numeric(frame.loc[recent.index, "Volume"], errors="coerce")
        traded = (
            pd.to_numeric(frame.loc[recent.index, "Close"], errors="coerce")
            * volume
        ).dropna()
        traded = traded[traded > 0]
        if len(traded) < 15 or float(traded.median()) < MIN_AVG_DAILY_VALUE_BAHT:
            continue
        returns = adjusted_closes.tail(21).pct_change().dropna()
        volatility = float(returns.tail(20).std(ddof=1) * np.sqrt(252))
        if not np.isfinite(volatility) or volatility <= 0:
            continue
        atr_pct = None
        if {"High", "Low", "Close"}.issubset(frame.columns):
            price_bars = frame.loc[
                closes.tail(15).index, ["High", "Low", "Close"]
            ].apply(pd.to_numeric, errors="coerce").dropna()
            if len(price_bars) >= 14:
                previous_close = price_bars["Close"].shift(1)
                true_range = pd.concat(
                    [
                        price_bars["High"] - price_bars["Low"],
                        (price_bars["High"] - previous_close).abs(),
                        (price_bars["Low"] - previous_close).abs(),
                    ],
                    axis=1,
                ).max(axis=1).dropna()
                if len(true_range) >= 14 and close > 0:
                    atr_pct = float(true_range.tail(14).mean() / close)
        eligible.append({
            "symbol": symbol,
            "close": close,
            "sma20": sma20,
            "sma50": sma50,
            "six_month_return": six_month_return,
            "relative_strength": (
                six_month_return - set100_median_return
                if set100_median_return is not None else None
            ),
            "max_drawdown": max_drawdown,
            "atr_pct": atr_pct,
            "volatility": volatility,
            "avg_value": float(traded.median()),
            "liquidity_days": int(len(traded)),
            "portfolio_correlation": portfolio_correlation(symbol),
        })

    # Include affordable board-lot names so an expensive momentum leader does
    # not crowd out every candidate this small portfolio can actually buy.
    eligible.sort(key=lambda item: (-item["six_month_return"], -item["avg_value"], item["volatility"]))
    shortlist = eligible[:12]
    seen_symbols = {item["symbol"] for item in shortlist}
    affordable_board_lots = [
        item for item in eligible
        if item["close"] * 100 * (1 + ESTIMATED_BUY_COST_RATE) <= budget / 2
    ]
    for item in affordable_board_lots + eligible:
        if len(shortlist) >= 24:
            break
        if item["symbol"] not in seen_symbols:
            shortlist.append(item)
            seen_symbols.add(item["symbol"])
    header.append(
        f"🔎 ผ่านแนวโน้ม/สภาพคล่อง {len(eligible)}/{len(UNIVERSE)} ตัว "
        f"| ตรวจงบ {len(shortlist)} ตัว"
    )
    if not shortlist:
        return "\n".join(
            header
            + [
                "⚠️ รอบนี้ไม่มีหุ้นผ่านเกณฑ์แนวโน้มและสภาพคล่อง จึงงดแนะนำซื้อ",
            ]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        fundamentals = list(executor.map(
            analyze_fundamentals,
            [item["symbol"] for item in shortlist],
        ))
    candidates = []
    watch_candidates = []
    for item, fundamental in zip(shortlist, fundamentals):
        available = int(fundamental.get("available", 0) or 0)
        score = fundamental.get("score")
        required = int(fundamental.get("required", 5) or 5)
        # Require every applicable fundamental check. Missing evidence is not a pass.
        item = dict(item)
        item["fundamental"] = fundamental
        if available != required or score is None or int(score) != required:
            watch_candidates.append(item)
            continue
        candidates.append(item)

    # Compare valuation only when the screened list has at least three names
    # from the same sector; otherwise report the peer comparison unavailable.
    screened_items = candidates + watch_candidates
    for item in screened_items:
        sector = str(item["fundamental"].get("sector") or "").strip().lower()
        item["peer_valuation"] = {}
        if not sector:
            continue
        peers = [
            peer for peer in screened_items
            if peer is not item
            and str(peer["fundamental"].get("sector") or "").strip().lower() == sector
        ]
        for metric in ("trailing_pe", "price_to_book"):
            own_value = item["fundamental"].get("valuation", {}).get(metric)
            peer_values = [
                peer["fundamental"].get("valuation", {}).get(metric)
                for peer in peers
            ]
            peer_values = [
                float(value) for value in peer_values
                if isinstance(value, (int, float)) and np.isfinite(value) and value > 0
            ]
            if (
                isinstance(own_value, (int, float))
                and np.isfinite(own_value) and own_value > 0
                and len(peer_values) >= 2
            ):
                median_value = float(np.median(peer_values))
                item["peer_valuation"][metric] = {
                    "median": median_value,
                    "ratio": float(own_value / median_value) if median_value > 0 else None,
                    "peer_count": len(peer_values) + 1,
                }
        ratios = [
            entry["ratio"] for entry in item["peer_valuation"].values()
            if entry.get("ratio") is not None
        ]
        item["sector_valuation_ratio"] = max(ratios) if ratios else None

    candidates.sort(key=lambda item: (
        item["fundamental"].get("valuation_concern")
        if item["fundamental"].get("valuation_concern") is not None else 99,
        int(item["fundamental"].get("quality_penalty", 0) or 0),
        item.get("sector_valuation_ratio")
        if item.get("sector_valuation_ratio") is not None else 99,
        item.get("portfolio_correlation")
        if item.get("portfolio_correlation") is not None else 0.75,
        -float(item.get("max_drawdown", -1.0)),
        float(item.get("atr_pct") if item.get("atr_pct") is not None else 1.0),
        -float(item.get("relative_strength") if item.get("relative_strength") is not None else -1.0),
        item["volatility"],
        -item["avg_value"],
    ))
    if not candidates:
        watch_lines = []
        if watch_candidates:
            watch_lines = ["หุ้นเฝ้าดูที่แนวโน้มผ่าน แต่ข้อมูลงบยังไม่ครบเกณฑ์:"]
            watch_lines.extend(
                f"• {display_stock_symbol(item['symbol'])} — "
                f"งบ {item['fundamental'].get('score') or 0}/"
                f"{item['fundamental'].get('required', 5)}"
                for item in sorted(
                    watch_candidates,
                    key=lambda candidate: (
                        -int(candidate["fundamental"].get("available", 0) or 0),
                        -(int(candidate["fundamental"].get("score", 0) or 0)),
                    ),
                )[:3]
            )
        return "\n".join(
            header
            + [
                f"⚠️ ตรวจงบ {len(shortlist)} ตัวแล้ว ยังไม่มีหุ้นผ่านพื้นฐานครบ จึงงดแนะนำซื้อ",
                "เงินสดคงไว้ก่อน; เรียกคำสั่งนี้ใหม่ได้เมื่อมีข้อมูลตลาดรอบถัดไป",
            ]
            + watch_lines
        )

    # Use the standard 100-share lot as an affordability check and spread
    # purchases across distinct sectors where possible.
    def pick_distinct_sectors(items, count):
        selected = []
        sectors = set()
        for item in items:
            sector = str(item["fundamental"].get("sector") or "").strip().lower()
            sector_key = sector or "__unknown__"
            if sector_key in sectors:
                continue
            selected.append(item)
            sectors.add(sector_key)
            if len(selected) == count:
                return selected
        return []

    chosen = []
    per_name_budget = 0.0
    for count in (3, 2):
        per_name_budget = budget / count
        affordable = [
            item for item in candidates
            if item["close"] * 100 * (1 + ESTIMATED_BUY_COST_RATE) <= per_name_budget
        ]
        chosen = pick_distinct_sectors(affordable, count)
        if chosen:
            break

    if not chosen:
        odd_lot_budget = budget / 2
        odd_lot_candidates = pick_distinct_sectors(
            [
                item for item in candidates
                if item["close"] * (1 + ESTIMATED_BUY_COST_RATE) <= odd_lot_budget
            ],
            2,
        )
        if len(odd_lot_candidates) == 2:
            lines = header + [
                f"🪙 หน่วยย่อย (Odd Lot) หากโบรกเกอร์รองรับ | งบ {budget:,.0f} บาท",
            ]
            for index, item in enumerate(odd_lot_candidates, 1):
                shares = int(
                    odd_lot_budget // (item["close"] * (1 + ESTIMATED_BUY_COST_RATE))
                )
                estimated_cost = shares * item["close"]
                estimated_fee = estimated_cost * ESTIMATED_BUY_COST_RATE
                relative_strength = item.get("relative_strength")
                lines.append(
                    f"{index}. {display_stock_symbol(item['symbol'])} — "
                    f"{item['close']:.2f} บ. | {shares:,} หุ้น | "
                    f"ซื้อ ~{estimated_cost:,.0f} + ค่าใช้จ่าย ~{estimated_fee:,.0f} บ."
                )
                lines.append(
                    "   " + concise_fundamental(item["fundamental"])
                )
                lines.append(
                    f"   แนวโน้ม 6 เดือน {item['six_month_return'] * 100:+.1f}%"
                    + (
                        f" | เทียบ SET100 {relative_strength * 100:+.1f}%"
                        if relative_strength is not None else ""
                    )
                    + f" | ซื้อขาย {item['liquidity_days']}/20 วัน"
                )
            lines.append(
                "Odd Lot อาจมีส่วนต่างราคา/สภาพคล่องต่างจากกระดานหลัก; "
                "ตรวจสอบกับโบรกเกอร์ก่อนซื้อ"
            )
            lines.append(
                "ข้อมูลประกอบการตัดสินใจ ไม่ใช่คำสั่งซื้ออัตโนมัติ; "
                "หุ้น 100% อาจขาดทุนเกิน 10%"
            )
            note_recommendations(odd_lot_candidates, odd_lot=True)
            return "\n".join(lines)
        return "\n".join(
            header
            + [
                f"งบ {budget:,.0f} บาทยังไม่พอซื้อ 100 หุ้นอย่างน้อย 2 บริษัท",
                "แนะนำเก็บเงินสดไว้ก่อน; หุ้นที่ผ่านเกณฑ์: "
                + ", ".join(
                    f"{display_stock_symbol(item['symbol'])} {item['close']:.2f} บ."
                    for item in candidates[:3]
                ),
            ]
        )

    lines = header + ["✅ หุ้นผ่านเกณฑ์ (ใช้พิจารณาสะสม):"]
    for index, item in enumerate(chosen, 1):
        shares = int(
            per_name_budget // (item["close"] * 100 * (1 + ESTIMATED_BUY_COST_RATE))
        ) * 100
        estimated_cost = shares * item["close"]
        estimated_fee = estimated_cost * ESTIMATED_BUY_COST_RATE
        fundamental = item["fundamental"]
        relative_strength = item.get("relative_strength")
        lines.append(
            f"{index}. {display_stock_symbol(item['symbol'])} — "
            f"{item['close']:.2f} บ. | {shares:,} หุ้น | "
            f"ซื้อ ~{estimated_cost:,.0f} + ค่าใช้จ่าย ~{estimated_fee:,.0f} บ."
        )
        lines.append("   " + concise_fundamental(fundamental))
        lines.append(
            f"   แนวโน้ม 6 เดือน {item['six_month_return'] * 100:+.1f}%"
            + (
                f" | เทียบ SET100 {relative_strength * 100:+.1f}%"
                if relative_strength is not None else ""
            )
            + f" | สภาพคล่อง {item['liquidity_days']}/20 วัน, "
            f"~{item['avg_value']:,.0f} บ./วัน"
        )
        risk_text = (
            f"   ความเสี่ยง: ลดจากจุดสูงสุด {abs(item['max_drawdown']) * 100:.1f}%"
        )
        if item.get("portfolio_correlation") is not None:
            risk_text += f" | สัมพันธ์กับหุ้นเดิม {item['portfolio_correlation']:.2f}"
        peer = item.get("peer_valuation", {}).get("trailing_pe")
        if peer:
            current_pe = fundamental.get("valuation", {}).get("trailing_pe")
            if isinstance(current_pe, (int, float)):
                risk_text += (
                    f" | P/E {current_pe:.1f}x (ค่ากลางกลุ่ม {peer['median']:.1f}x)"
                )
        lines.append(risk_text)

    lines.append(
        "ตรวจราคาและค่าธรรมเนียมกับโบรกเกอร์ก่อนซื้อ; "
        "ข้อมูลนี้ไม่ใช่คำสั่งซื้ออัตโนมัติหรือการรับประกันผลตอบแทน"
    )
    lines.append("หุ้น 100% อาจขาดทุนเกิน 10%")
    note_recommendations(chosen)
    return "\n".join(lines)

_long_term_users = set()


def _prepare_long_term_recommendation(user_id):
    try:
        portfolio = get_portfolio(user_id)
        snapshots = []
        if portfolio.get("investment_strategy", {}).get("style") != "LONG_TERM":
            result = "ยังไม่ได้เปิดแผนลงทุนระยะยาว จึงไม่ได้จัดแผนสะสมหุ้น"
        else:
            result = build_long_term_accumulation_plan(portfolio, snapshots)
        if snapshots:
            def save_pending(current):
                if (
                    current.get("cash") != portfolio.get("cash")
                    or current.get("stocks") != portfolio.get("stocks")
                ):
                    return False
                records = current.setdefault("long_term_recommendations", [])
                existing = {
                    (row.get("signal_date"), row.get("symbol")): row
                    for row in records if isinstance(row, dict)
                }
                for snapshot in snapshots:
                    key = (snapshot["signal_date"], snapshot["symbol"])
                    if key not in existing:
                        records.append(snapshot)
                current["long_term_recommendations"] = records[-600:]
                return True

            if not mutate_portfolio(user_id, save_pending):
                result = "พอร์ตเปลี่ยนระหว่างวิเคราะห์ กรุณาเรียก แนะนำหุ้นสะสม ใหม่"
                snapshots = []
        line_bot_api.push_message(
            user_id,
            TextSendMessage(text=result),
        )
        if snapshots:
            def confirm_sent(current):
                keys = {(row["signal_date"], row["symbol"]) for row in snapshots}
                for row in current.get("long_term_recommendations", []):
                    if (row.get("signal_date"), row.get("symbol")) in keys:
                        row["delivery_status"] = "SENT"
                        row["sent_at"] = utc_text()

            try:
                mutate_portfolio(user_id, confirm_sent)
            except Exception:
                # The recommendation was already delivered. Reporting that no
                # advice was sent would be false; keep the unconfirmed record
                # out of measured results until it can be reconciled.
                logger.exception("ส่งคำแนะนำแล้ว แต่ยืนยันสถานะบันทึกไม่สำเร็จ")
        return True
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
        return False
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


def send_weekly_long_term_recommendations():
    """Send one long-term screen per user each trading week after the close."""
    current = now_bkk()
    today = current.date()
    if current.hour < 19 or not is_set_session(today):
        return
    universe_warning = set100_universe_warning(today)
    year, week, _ = today.isocalendar()
    week_key = f"{year}-W{week:02d}"
    try:
        users = registered_users()
    except Exception:
        logger.exception("อ่านรายชื่อผู้ใช้เพื่อส่งหุ้นสะสมรายสัปดาห์ไม่สำเร็จ")
        return

    for user_id in users:
        try:
            portfolio = get_portfolio(user_id)
            if portfolio.get("investment_strategy", {}).get("style") != "LONG_TERM":
                continue
            if float(portfolio.get("cash", 0) or 0) <= 0:
                continue

            def claim(current_portfolio):
                previous = current_portfolio.get("weekly_long_term_recommendation") or {}
                if previous.get("week") == week_key and (
                    previous.get("status") == "SENT"
                    or previous.get("date") == today.isoformat()
                ):
                    return False
                current_portfolio["weekly_long_term_recommendation"] = {
                    "week": week_key,
                    "date": today.isoformat(),
                    "status": "PENDING",
                }
                return True

            if not mutate_portfolio(user_id, claim):
                continue

            with _on_demand_lock:
                busy = user_id in _long_term_users or user_id in _on_demand_users
                if not busy:
                    _long_term_users.add(user_id)
            if busy:
                def mark_busy(current_portfolio):
                    current_portfolio["weekly_long_term_recommendation"]["status"] = "FAILED"
                mutate_portfolio(user_id, mark_busy)
                continue

            if universe_warning:
                try:
                    line_bot_api.push_message(user_id, TextSendMessage(text=universe_warning))
                    sent = True
                except Exception:
                    sent = False
                    logger.exception("แจ้งรายชื่อ SET100 หมดอายุไม่สำเร็จ")
                finally:
                    with _on_demand_lock:
                        _long_term_users.discard(user_id)
            else:
                sent = _prepare_long_term_recommendation(user_id)

            def record_result(current_portfolio):
                previous = current_portfolio.get("weekly_long_term_recommendation") or {}
                if previous.get("week") == week_key and previous.get("date") == today.isoformat():
                    previous["status"] = "SENT" if sent else "FAILED"
            mutate_portfolio(user_id, record_result)
        except Exception:
            logger.exception("ส่งหุ้นสะสมรายสัปดาห์หนึ่งรายการไม่สำเร็จ")


_long_term_review_users = set()


def build_long_term_review(portfolio):
    """Review saved signals and actual holdings after a completed SET session."""
    expected = latest_completed_set_session(now_bkk())
    previous = latest_completed_set_session(
        datetime.combine(expected, datetime.min.time())
    )
    allowed_dates = {expected.isoformat(), previous.isoformat()}
    cutoff = (now_bkk().date() - timedelta(days=370)).isoformat()
    records = [
        row for row in portfolio.get("long_term_recommendations", [])
        if isinstance(row, dict)
        and row.get("delivery_status") == "SENT"
        and isinstance(row.get("signal_date"), str)
        and row["signal_date"] >= cutoff
    ]
    holdings = portfolio.get("stocks", {})
    symbols = sorted(
        {row["symbol"] for row in records if isinstance(row.get("symbol"), str)}
        | set(holdings)
    )
    frames = get_market_frames(symbols, period="2y") if symbols else {}
    benchmark = get_stock_history("^SET.BK", period="2y") if records else None
    observations, summaries = evaluate_recommendations(
        records, frames, benchmark, cost_rate=ESTIMATED_BUY_COST_RATE
    )
    lines = [
        "📊 ทบทวนแผนลงทุนระยะยาว",
        f"ข้อมูลราคาถึงวันซื้อขาย {expected.isoformat()} (เมื่อผู้ให้บริการมีข้อมูล)",
        "ผลติดตามหุ้นที่บอตเคยแนะนำและส่งสำเร็จ:",
    ]
    if not records:
        lines.append("ยังไม่มีบันทึกคำแนะนำเดิม เริ่มเก็บตั้งแต่ระบบรุ่นนี้")
    elif not summaries:
        lines.append("ยังไม่มีคำแนะนำที่ครบ 20 วันซื้อขาย หรือข้อมูลราคายังไม่พอ")
    else:
        for horizon in HORIZONS:
            metric = summaries.get(horizon)
            if metric is None:
                continue
            line = (
                f"• {horizon} วัน: บวก {metric['positive_count']}/"
                f"{metric['count']} ตัว | ผลตอบแทนเฉลี่ยหลังต้นทุนประมาณ "
                f"{metric['mean_net_return'] * 100:+.1f}%"
            )
            if metric["comparable_count"]:
                line += (
                    f" | ชนะ SET {metric['beat_benchmark_count']}/"
                    f"{metric['comparable_count']} ตัว"
                )
            lines.append(line)
        latest_rows = sorted(
            observations,
            key=lambda row: (row["signal_date"], row["horizon"]),
            reverse=True,
        )
        latest_by_symbol = {}
        for row in latest_rows:
            latest_by_symbol.setdefault(row["symbol"], row)
        for row in list(latest_by_symbol.values())[:3]:
            lines.append(
                f"  {display_stock_symbol(row['symbol'])} "
                f"({row['horizon']} วัน) {row['net_return'] * 100:+.1f}%"
            )
    if records:
        lines.append(
            "นับจากราคาเปิดวันซื้อขายถัดไป; หักต้นทุนซื้อ/ขายโดยประมาณ "
            "ผลนี้ไม่รวมงบย้อนหลังหรือการซื้อจริงของคุณ"
        )

    lines.append("\nหุ้นที่บันทึกว่าถืออยู่:")
    if not holdings:
        lines.append("ยังไม่มีหุ้นที่บันทึกไว้")
    else:
        symbols_held = [
            symbol for symbol, info in holdings.items()
            if int(info.get("shares", 0) or 0) > 0
        ]
        if not symbols_held:
            lines.append("ยังไม่มีหุ้นที่บันทึกไว้")
        reviewed_symbols = symbols_held[:10]
        if len(symbols_held) > len(reviewed_symbols):
            lines.append(f"แสดง 10 จาก {len(symbols_held)} ตัวที่ถือ")
        if reviewed_symbols:
            with ThreadPoolExecutor(max_workers=min(2, len(reviewed_symbols))) as executor:
                fundamentals = list(executor.map(analyze_fundamentals, reviewed_symbols))
        else:
            fundamentals = []
        for symbol, fundamental in zip(reviewed_symbols, fundamentals):
            frame = frames.get(symbol)
            if frame is None or frame.empty:
                lines.append(f"• {display_stock_symbol(symbol)}: ข้อมูลราคาไม่พอ ยังไม่สรุปขาย")
                continue
            latest_day = frame.index[-1].strftime("%Y-%m-%d")
            if latest_day not in allowed_dates:
                lines.append(f"• {display_stock_symbol(symbol)}: ราคาล่าสุดเก่า ({latest_day}) ยังไม่สรุปขาย")
                continue
            review = assess_holding(frame, fundamental, holdings[symbol].get("avg_price"))
            pnl = review.get("pnl_pct")
            pnl_text = f" | เทียบต้นทุน {pnl * 100:+.1f}%" if pnl is not None else ""
            lines.append(
                f"• {display_stock_symbol(symbol)}: {review['status']}"
                f"{pnl_text} — {', '.join(review['reasons'])}"
            )
    recent_unheld = []
    for record in sorted(records, key=lambda row: row["signal_date"], reverse=True):
        symbol = record["symbol"]
        if symbol not in holdings and symbol not in recent_unheld:
            recent_unheld.append(symbol)
        if len(recent_unheld) == 3:
            break
    if recent_unheld:
        lines.append("\nหุ้นที่เคยแนะนำ แต่ไม่ได้บันทึกว่าถือ (ไม่ทราบว่าซื้อจริงหรือไม่):")
        with ThreadPoolExecutor(max_workers=min(2, len(recent_unheld))) as executor:
            past_fundamentals = list(executor.map(analyze_fundamentals, recent_unheld))
        for symbol, fundamental in zip(recent_unheld, past_fundamentals):
            frame = frames.get(symbol)
            if frame is None or frame.empty or frame.index[-1].strftime("%Y-%m-%d") not in allowed_dates:
                lines.append(f"• {display_stock_symbol(symbol)}: ราคาล่าสุดไม่พอ ยังไม่สรุป")
                continue
            review = assess_holding(frame, fundamental, None)
            lines.append(
                f"• {display_stock_symbol(symbol)}: {review['status']} — "
                + ", ".join(review["reasons"])
            )
    lines.append(
        "คำว่า พิจารณาขาย/ลด เป็นสัญญาณให้ตรวจงบและราคาอีกครั้ง "
        "บอตไม่ส่งคำสั่งซื้อขาย"
    )
    return "\n".join(lines)


def _send_long_term_review(user_id):
    try:
        portfolio = get_portfolio(user_id)
        if portfolio.get("investment_strategy", {}).get("style") != "LONG_TERM":
            return
        result = build_long_term_review(portfolio)
        line_bot_api.push_message(user_id, TextSendMessage(text=result))
    except Exception:
        logger.exception("ทบทวนพอร์ตระยะยาวไม่สำเร็จ")
    finally:
        with _on_demand_lock:
            _long_term_review_users.discard(user_id)


def queue_long_term_review(user_id):
    ensure_scheduler()
    with _on_demand_lock:
        if user_id in _long_term_review_users:
            return False
        _long_term_review_users.add(user_id)
    try:
        scheduler.add_job(
            _send_long_term_review,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=1),
            args=[user_id],
            id="long-term-review-" + hashlib.sha256(user_id.encode()).hexdigest()[:20],
            replace_existing=True,
            max_instances=1,
        )
    except Exception:
        with _on_demand_lock:
            _long_term_review_users.discard(user_id)
        raise
    return True


def send_closed_day_reviews():
    """Send at most one automatic review per user per week on closed days."""
    today = now_bkk().date()
    if is_set_session(today):
        return
    for user_id in registered_users():
        try:
            portfolio = get_portfolio(user_id)
            if portfolio.get("investment_strategy", {}).get("style") != "LONG_TERM":
                continue
            if not portfolio.get("long_term_recommendations") and not portfolio.get("stocks"):
                continue
            previous = portfolio.get("closed_day_review") or {}
            last_day = previous.get("date")
            if isinstance(last_day, str):
                try:
                    days_ago = (today - datetime.fromisoformat(last_day).date()).days
                except ValueError:
                    days_ago = 99
                if 0 <= days_ago < 6 and previous.get("status") in {"SENT", "UNKNOWN"}:
                    continue
                if 0 <= days_ago < 1 and previous.get("status") == "PENDING":
                    continue

            def mark_pending(current):
                current["closed_day_review"] = {
                    "date": today.isoformat(), "status": "PENDING",
                }

            mutate_portfolio(user_id, mark_pending)
            report = build_long_term_review(portfolio)
            try:
                line_bot_api.push_message(user_id, TextSendMessage(text=report))
            except Exception:
                def mark_unknown(current):
                    current["closed_day_review"] = {
                        "date": today.isoformat(), "status": "UNKNOWN",
                    }
                mutate_portfolio(user_id, mark_unknown)
                raise

            def mark_sent(current):
                current["closed_day_review"] = {
                    "date": today.isoformat(), "status": "SENT",
                }

            mutate_portfolio(user_id, mark_sent)
        except Exception:
            logger.exception("ส่งทบทวนวันตลาดปิดไม่สำเร็จ")


def help_text():
    return (
        "📊 PORTFOLIO BOT\n"
        "เมนูคำสั่ง LINE\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📈 แผนลงทุน\n"
        "ตั้งแผนลงทุนระยะยาว — ทุนอ้างอิง 6,000 บาท\n"
        "แนะนำหุ้นสะสม — คัดหุ้นใหม่ได้ทุกเมื่อ\n"
        "สถานะหุ้นสะสม — ดูรอบส่งอัตโนมัติและข้อมูลที่บันทึก\n"
        "ทบทวนหุ้นระยะยาว — ผลคำแนะนำเดิมและหุ้นที่ควรทบทวนขาย\n"
        "ตั้งเงินเติมรายเดือน 500 — กำหนด 500–1,000 บาท/เดือน\n"
        "ตรวจพอร์ตระยะยาว — ดูสัดส่วนและผลกำไร/ขาดทุน\n"
        "ดูแนวทางลงทุน — ดูเป้าหมายที่ตั้งไว้\n\n"
        "🗓 แผนรายวัน\n"
        "แผนวันนี้ หรือ ควรซื้อวันนี้ — ดูแผนรายวัน\n"
        "สถานะแผน — ตรวจวันที่ข้อมูลและสถานะเตรียมแผน\n"
        "หมายเหตุ: ปิดแผนรายวันเมื่อเปิดโหมดระยะยาว\n\n"
        "💰 พอร์ตและเงินสด\n"
        "ดูพอร์ต — ดูเงินสดและหุ้นที่บันทึก\n"
        "ดูพอร์ตแสดงยอดเงินเติมสะสมที่แจ้งบอตด้วย\n"
        "ตั้งเงินสด 6000 — ตั้งยอดเงินสดรวมให้ตรงบัญชีจริง\n"
        "บันทึกเงินเติม 500 — เพิ่มยอดหลังฝากเงินจริงแล้ว\n"
        "บันทึกซื้อ AOT 100 35.50 — เพิ่มหุ้นและหักเงินสด\n"
        "บันทึกขาย AOT 100 40 — ลดหุ้นและเพิ่มเงินสด\n"
        "ตั้งหุ้น AOT 100 35.50 — บันทึกหุ้นและต้นทุน\n"
        "ตั้งหุ้น AOT 0 0 — ลบหุ้นที่ขายหมดแล้ว\n"
        "ตรวจพอร์ตหลังซื้อขายจริง เพื่อให้ยอดในบอตตรงบัญชี\n\n"
        "🔎 วิเคราะห์และติดตาม\n"
        "วิเคราะห์ COM7 — ดูแนวโน้มราคาและข้อมูลงบ\n"
        "ติดตาม AOT — เพิ่มหุ้นในรายการติดตาม\n"
        "เลิกติดตาม AOT — นำหุ้นออกจากรายการ\n"
        "รายการติดตาม — ดูหุ้นที่ติดตาม\n\n"
        "📦 ข้อมูลพอร์ต\n"
        "สำรองพอร์ต — รับ JSON สำหรับการย้ายข้อมูลครั้งเดียว\n"
        "กู้คืน — ส่งไฟล์สำรอง JSON กลับเข้าแชต\n"
        "สถานะข้อมูล — ตรวจวิธีจัดเก็บพอร์ต\n\n"
        "พิมพ์ เมนู หรือ ช่วยเหลือ เพื่อเปิดหน้านี้อีกครั้ง\n"
        "⚠️ บอตให้ข้อมูลประกอบ ไม่ส่งคำสั่งซื้อขาย "
        "และไม่รับประกันผลตอบแทน"
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

    if text == "สถานะข้อมูล":
        storage_name, storage_detail = portfolio_storage_status()
        universe_warning = set100_universe_warning()
        return (
            "🗄️ สถานะการเก็บข้อมูลพอร์ต\n"
            f"วิธีเก็บ: {storage_name}\n"
            f"รายละเอียด: {storage_detail}\n"
            + (universe_warning or f"รายชื่อ SET100 ใช้ถึง {SET100_EFFECTIVE_TO}")
        )

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

    try:
        portfolio = get_portfolio(user_id)
    except Exception:
        logger.exception("เปิดข้อมูลพอร์ตไม่สำเร็จ")
        return (
            "⚠️ ยังเข้าถึงที่เก็บพอร์ตถาวรไม่ได้ "
            "ระบบงดบันทึกและแนะนำจากยอดเงินที่อาจหาย "
            "พิมพ์ สถานะข้อมูล เพื่อตรวจสอบ"
        )

    if text == "สถานะหุ้นสะสม":
        strategy = portfolio.get("investment_strategy") or {}
        weekly = portfolio.get("weekly_long_term_recommendation") or {}
        sent_records = [
            row for row in portfolio.get("long_term_recommendations", [])
            if isinstance(row, dict) and row.get("delivery_status") == "SENT"
        ]
        status_text = {
            "SENT": "ส่งข้อความแล้ว",
            "PENDING": "กำลังจัดทำหรือรอบก่อนถูกขัดจังหวะ",
            "FAILED": "รอบล่าสุดไม่สำเร็จ จะลองใหม่ในวันซื้อขายถัดไป",
        }.get(weekly.get("status"), "ยังไม่มีรอบส่ง")
        return (
            "🧭 สถานะหุ้นสะสม\n"
            f"แผนระยะยาว: {'เปิด' if strategy.get('style') == 'LONG_TERM' else 'ยังไม่เปิด'}\n"
            f"รอบอัตโนมัติ: {weekly.get('week', 'ยังไม่มี')} — {status_text}\n"
            f"คำแนะนำที่ส่งและบันทึกแล้ว: {len(sent_records)} รายการ\n"
            "ระบบพยายามส่งสัปดาห์ละหนึ่งครั้งหลังตลาดปิด; "
            "สั่ง แนะนำหุ้นสะสม เองได้ทุกเมื่อ"
        )

    if text == "ตั้งแผนลงทุนระยะยาว":
        def enable_long_term(p):
            existing = p.get("investment_strategy") or {}
            monthly = existing.get("monthly_contribution_baht", 500.0)
            if not isinstance(monthly, (int, float)) or not 500 <= monthly <= 1000:
                monthly = 500.0
            p["investment_strategy"] = {
                "style": "LONG_TERM",
                "starting_capital": 6000.0,
                "max_temporary_loss_pct": 10.0,
                "target_equity_pct": 100.0,
                "target_cash_pct": 0.0,
                "allocation_version": 2,
                "monthly_contribution_baht": float(monthly),
                "market_scope": "TH",
                "review_frequency": "on_demand",
                "updated_at": utc_text(),
            }
            p["overnight_plan"] = None
            return (
                "✅ บันทึกแนวทางลงทุนระยะยาวแล้ว\n"
                f"เงินตั้งต้นอ้างอิง 6,000 บาท เติมตามแผน {monthly:,.0f} บาท/เดือน\n"
                "ยอดเงินสดจริงใช้ค่าที่บันทึกไว้ในพอร์ต "
                "หากเริ่มใหม่ให้พิมพ์ ตั้งเงินสด 6000 หลังมีเงินลงทุนจริง\n"
                "เป้าหมาย: หุ้นไทย 100% ของมูลค่าพอร์ตที่บันทึก "
                "เงินสดเป้าหมาย 0% (เงินสดจริงที่ยังไม่มีหุ้นผ่านเกณฑ์จะคงไว้ชั่วคราว)\n"
                "ปรับเงินเติมได้ 500–1,000 บาทด้วยคำสั่ง ตั้งเงินเติมรายเดือน\n"
                "ทบทวนได้ทุกเมื่อด้วยคำสั่ง แนะนำหุ้นสะสม; "
                "บอตไม่ซื้อขายหรือฝากเงินแทน\n"
                "⚠️ หุ้น 100% ไม่สอดคล้องกับเป้าหมายขาดทุนไม่เกิน 10% "
                "และไม่รับประกันว่าจะจำกัดการขาดทุนได้"
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

    if text in {"ทบทวนหุ้นระยะยาว", "ผลคำแนะนำ", "หุ้นไหนควรขาย"}:
        if portfolio.get("investment_strategy", {}).get("style") != "LONG_TERM":
            return "เปิดแผนก่อนด้วยคำสั่ง ตั้งแผนลงทุนระยะยาว"
        try:
            started = queue_long_term_review(user_id)
        except Exception:
            logger.exception("เริ่มทบทวนหุ้นระยะยาวไม่สำเร็จ")
            return "ยังเริ่มทบทวนไม่ได้ โปรดลองใหม่ภายหลัง"
        return (
            "กำลังตรวจผลคำแนะนำและหุ้นที่ถือ เมื่อเสร็จจะส่งในแชตนี้"
            if started else "กำลังทบทวนอยู่ เมื่อเสร็จจะส่งผลในแชตนี้"
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
            f"เงินตั้งต้นอ้างอิง: {strategy.get('starting_capital', 6000):,.0f} บาท\n"
            f"หุ้นเป้าหมาย: {strategy.get('target_equity_pct', 100):.0f}% | "
            f"เงินสดเป้าหมาย: {strategy.get('target_cash_pct', 0):.0f}%\n"
            f"แผนเติมเงิน: {strategy.get('monthly_contribution_baht', 500):,.0f} บาท/เดือน "
            "(บันทึกหลังเติมเงินจริงด้วยคำสั่ง บันทึกเงินเติม)\n"
            f"ระดับขาดทุนชั่วคราวที่ตั้งใจรับ: "
            f"{strategy.get('max_temporary_loss_pct', 10):.0f}%\n"
            "⚠️ หุ้น 100% อาจขาดทุนเกิน 10% ได้ตามความผันผวนตลาด\n"
            "ทบทวนได้ทุกเมื่อ: พิมพ์ แนะนำหุ้นสะสม ไม่ต้องรอเดือนหน้า"
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
        target_equity_pct = float(strategy.get("target_equity_pct", 100))
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
            "⚠️ หุ้น 100% ไม่สามารถรับประกันขาดทุนไม่เกิน 10% ได้",
            "",
        ]
        if holding_lines:
            lines.extend(holding_lines)
        else:
            lines.append("ยังไม่มีหุ้นในพอร์ตที่บอตบันทึก")
        lines.extend([
            "",
            "ตรวจและขอคำแนะนำใหม่ได้ทุกเมื่อด้วยคำสั่ง แนะนำหุ้นสะสม",
            "ข้อมูลนี้ใช้เฉพาะเงินสด/หุ้นที่บันทึกในบอต "
            "และไม่ใช่การคำนวณ drawdown ของทั้งบัญชี",
            "การจำกัดขาดทุน 10% ทำไม่ได้ด้วยการเตือนอย่างเดียว "
            "ราคาหุ้นอาจลดเกินระดับดังกล่าวได้",
        ])
        return "\n".join(lines)

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
        contribution_total = 0.0
        contribution_month = 0.0
        current_month = now_bkk().strftime("%Y-%m")
        for entry in portfolio.get("history", []):
            if not isinstance(entry, dict) or entry.get("type") != "MONTHLY_CONTRIBUTION":
                continue
            try:
                amount = float(entry.get("amount", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            if not np.isfinite(amount) or amount <= 0:
                continue
            contribution_total += amount
            if entry.get("month") == current_month:
                contribution_month += amount
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
            f"📥 เงินเติมที่บันทึกสะสม: {contribution_total:,.2f} บาท "
            f"(เดือนนี้ {contribution_month:,.2f} บาท)",
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

    if text.startswith("ตั้งเงินเติมรายเดือน "):
        parts = text.split()
        if len(parts) != 2:
            return "รูปแบบ: ตั้งเงินเติมรายเดือน 500 (กำหนด 500–1,000 บาท)"
        try:
            amount = float(parts[1].replace(",", ""))
        except ValueError:
            return "ยอดเงินเติมรายเดือนต้องอยู่ระหว่าง 500–1,000 บาท"
        if not np.isfinite(amount) or not 500 <= amount <= 1000:
            return "ยอดเงินเติมรายเดือนต้องอยู่ระหว่าง 500–1,000 บาท"
        configured_strategy = portfolio.get("investment_strategy")
        if (
            not isinstance(configured_strategy, dict)
            or configured_strategy.get("style") != "LONG_TERM"
        ):
            return "เปิดแผนก่อนด้วยคำสั่ง ตั้งแผนลงทุนระยะยาว"
        
        def set_monthly_contribution(p):
            strategy = p.setdefault("investment_strategy", {})
            strategy["monthly_contribution_baht"] = amount
            strategy["updated_at"] = utc_text()
            return (
                f"บันทึกแผนเติมเงิน {amount:,.0f} บาท/เดือนแล้ว "
                "เป็นเพียงเป้าหมาย ไม่ได้เพิ่มเงินสดในบอตอัตโนมัติ "
                "เมื่อฝากเงินจริง ให้พิมพ์ บันทึกเงินเติม ตามยอดที่ฝาก"
            )

        return mutate_portfolio(user_id, set_monthly_contribution)

    if text.startswith("บันทึกเงินเติม "):
        parts = text.split()
        if len(parts) != 2:
            return "รูปแบบ: บันทึกเงินเติม 500 (ใช้หลังฝากเงินจริงแล้ว)"
        try:
            amount = float(parts[1].replace(",", ""))
        except ValueError:
            return "ยอดเงินเติมต้องเป็นตัวเลขมากกว่า 0 บาท"
        if not np.isfinite(amount) or amount <= 0:
            return "ยอดเงินเติมต้องเป็นตัวเลขมากกว่า 0 บาท"

        def record_contribution(p):
            strategy = p.get("investment_strategy")
            if not isinstance(strategy, dict):
                strategy = {}
            month = now_bkk().strftime("%Y-%m")
            history = p.setdefault("history", [])
            if strategy.get("style") == "LONG_TERM":
                month_total = 0.0
                for entry in history:
                    if (
                        isinstance(entry, dict)
                        and entry.get("type") == "MONTHLY_CONTRIBUTION"
                        and entry.get("month") == month
                    ):
                        try:
                            month_total += float(entry.get("amount", 0) or 0)
                        except (TypeError, ValueError, OverflowError):
                            continue
                if month_total + amount > 1000:
                    remaining = max(0.0, 1000 - month_total)
                    return (
                        f"เดือนนี้บันทึกเงินเติมแล้ว {month_total:,.2f} บาท "
                        f"บันทึกเพิ่มได้อีกไม่เกิน {remaining:,.2f} บาท "
                        "ตามกรอบแผน 1,000 บาท/เดือน"
                    )
            p["cash"] = max(0.0, float(p.get("cash", 0) or 0)) + amount
            history.append({
                "type": "MONTHLY_CONTRIBUTION",
                "amount": amount,
                "month": month,
                "recorded_at": utc_text(),
            })
            p["overnight_plan"] = None
            return (
                f"บันทึกเงินสดเพิ่ม {amount:,.2f} บาทแล้ว "
                f"ยอดเงินสดในบอต {p['cash']:,.2f} บาท "
                "(ต้องฝากเงินจริงก่อน บอตไม่ได้โอนเงิน) "
                "พิมพ์ แนะนำหุ้นสะสม เพื่อคัดหุ้นใหม่ได้ทันที"
            )

        return mutate_portfolio(user_id, record_contribution)

    if text.startswith("บันทึกซื้อ ") or text.startswith("บันทึกขาย "):
        parts = text.split()
        if len(parts) not in {4, 5}:
            return "รูปแบบ: บันทึกซื้อ AOT 100 35.50 [ค่าใช้จ่ายจริง] หรือ บันทึกขาย AOT 100 40 [ค่าใช้จ่ายจริง]"
        buying = parts[0] == "บันทึกซื้อ"
        symbol = normalize_stock_symbol(parts[1])
        try:
            shares = int(parts[2].replace(",", ""))
            price = float(parts[3].replace(",", ""))
            gross = shares * price
            fee = (
                float(parts[4].replace(",", ""))
                if len(parts) == 5 else gross * ESTIMATED_BUY_COST_RATE
            )
        except ValueError:
            return "จำนวนหุ้น ราคา และค่าใช้จ่ายต้องเป็นตัวเลขที่ถูกต้อง"
        if (
            symbol is None or shares <= 0 or shares > 1_000_000_000
            or not np.isfinite(price) or price <= 0
            or not np.isfinite(fee) or fee < 0 or fee > gross * 0.05
        ):
            return "ข้อมูลรายการซื้อขายไม่ถูกต้อง ตรวจชื่อหุ้น จำนวน ราคา และค่าใช้จ่าย"

        def record_trade(p):
            cash = float(p.get("cash", 0) or 0)
            holdings = p.setdefault("stocks", {})
            old = holdings.get(symbol, {})
            old_shares = int(old.get("shares", 0) or 0)
            old_cost = float(old.get("avg_price", 0) or 0)
            if buying:
                total = gross + fee
                if total > cash + 0.005:
                    return (
                        f"เงินสดในบอต {cash:,.2f} บาท ไม่พอซื้อรวมค่าใช้จ่าย "
                        f"{total:,.2f} บาท; ตรวจยอดด้วยคำสั่ง ดูพอร์ต"
                    )
                new_shares = old_shares + shares
                holdings[symbol] = {
                    "shares": new_shares,
                    "avg_price": (old_shares * old_cost + total) / new_shares,
                }
                p["cash"] = max(0.0, cash - total)
                action = "ซื้อ"
            else:
                if shares > old_shares:
                    return f"ถือ {display_stock_symbol(symbol)} เพียง {old_shares:,} หุ้น บันทึกขาย {shares:,} หุ้นไม่ได้"
                new_shares = old_shares - shares
                if new_shares:
                    holdings[symbol] = {"shares": new_shares, "avg_price": old_cost}
                else:
                    holdings.pop(symbol, None)
                p["cash"] = cash + gross - fee
                action = "ขาย"
                realized_pnl = gross - fee - shares * old_cost
            p["overnight_plan"] = None
            p.setdefault("history", []).append({
                "type": "RECORDED_BUY" if buying else "RECORDED_SELL",
                "symbol": symbol,
                "shares": shares,
                "price": price,
                "fee": fee,
                "fee_estimated": len(parts) == 4,
                **({"realized_pnl": realized_pnl} if not buying else {}),
                "recorded_at": utc_text(),
            })
            fee_note = " (ค่าใช้จ่ายประมาณ)" if len(parts) == 4 else ""
            pnl_note = f" | กำไร/ขาดทุนตามต้นทุน {realized_pnl:+,.2f} บาท" if not buying else ""
            return (
                f"บันทึก{action} {display_stock_symbol(symbol)} {shares:,} หุ้น "
                f"ราคา {price:,.2f} บาท ค่าใช้จ่าย {fee:,.2f} บาท{fee_note}\n"
                f"คงเหลือ {new_shares:,} หุ้น | เงินสดในบอต {p['cash']:,.2f} บาท{pnl_note}"
            )

        return mutate_portfolio(user_id, record_trade)

    if text.startswith("ตั้งเงินสด "):
        parts = text.split()
        if len(parts) != 2:
            return "รูปแบบ: ตั้งเงินสด 6000"
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
                f"ตั้งยอดเงินสดในบอตเป็น {amount:,.2f} บาทแล้ว "
                "(กรอกยอดคงเหลือจริงทั้งหมด ไม่ใช่คำสั่งฝากเงิน) "
                "พิมพ์ แนะนำหุ้นสะสม เพื่อวิเคราะห์ใหม่ได้ทันที"
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
        worker_scheduler.add_job(
            send_closed_day_reviews,
            "cron",
            hour=10,
            minute="5,45",
            id="closed-day-long-term-review",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        worker_scheduler.add_job(
            send_weekly_long_term_recommendations,
            "cron",
            day_of_week="mon-fri",
            hour=19,
            minute="20,50",
            id="weekly-long-term-recommendations",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        # Render Free can sleep through a cron time. A later wake-up on a
        # closed day should still attempt the idempotent weekly review.
        current = now_bkk()
        if current.hour >= 10 and not is_set_session(current.date()):
            worker_scheduler.add_job(
                send_closed_day_reviews,
                "date",
                run_date=datetime.now(timezone.utc) + timedelta(seconds=5),
                id="closed-day-long-term-review-catchup",
                replace_existing=True,
                max_instances=1,
            )
        if current.hour >= 19 and is_set_session(current.date()):
            worker_scheduler.add_job(
                send_weekly_long_term_recommendations,
                "date",
                run_date=datetime.now(timezone.utc) + timedelta(seconds=5),
                id="weekly-long-term-recommendations-catchup",
                replace_existing=True,
                max_instances=1,
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

