from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import logging
import os
import re
from pathlib import Path
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
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import numpy as np
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb
import requests

from fundamental_analysis import analyze_fundamentals
from position_risk import estimate_position
from set100_constituents import SET100_SYMBOLS


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
_portfolio_lock = threading.RLock()
_portfolio_database_lock = threading.Lock()
_portfolio_database_ready = False

INDEX_SYMBOL = "SET.BK"
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
def get_stock_history(symbol, period="3mo"):
    try:
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol}?range={period}&interval=1d"
        )
        headers = {"User-Agent": "Mozilla/5.0"}
        response = None
        for attempt in range(3):
            response = requests.get(
                url, headers=headers, timeout=10
            )
            if response.status_code == 200:
                break

            retryable = (
                response.status_code == 429
                or response.status_code >= 500
            )
            if retryable and attempt < 2:
                try:
                    delay = float(
                        response.headers.get(
                            "Retry-After", ""
                        )
                    )
                except (TypeError, ValueError):
                    delay = 1.5 * (attempt + 1)
                time.sleep(min(max(delay, 1), 5))
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
        logger.exception("ดึงหรือแปลงข้อมูลราคาหุ้นหนึ่งรายการไม่สำเร็จ")
        return None


def get_market_frames(symbols):
    unique_symbols = sorted(set(symbols))
    if not unique_symbols:
        return {}

    def fetch_one(sym):
        return sym, get_stock_history(sym, period="6mo")

    frames = {}
    # ลดคำขอพร้อมกันเพื่อลดโอกาสถูก Yahoo Finance จำกัด
    worker_count = min(4, len(unique_symbols))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for sym, df in executor.map(fetch_one, unique_symbols):
            if df is not None and len(df) >= 20:
                frames[sym] = df
    return frames


def normalize_stock_symbol(value):
    if not isinstance(value, str):
        return None
    base = value.strip().upper().removesuffix(".BK")
    if not re.fullmatch(r"[A-Z0-9]{1,10}", base):
        return None
    return base + ".BK"


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
    idx_df = frames.get(INDEX_SYMBOL)
    if idx_df is None or len(idx_df) < 20:
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

    as_of = idx_df.index[-1].strftime("%Y-%m-%d")
    valid = {}
    for sym in UNIVERSE:
        values = _close_and_sma20(frames.get(sym), as_of)
        if values is not None:
            valid[sym] = values

    coverage = (
        len(valid) / len(UNIVERSE) if UNIVERSE else 0
    )
    if coverage < MIN_UNIVERSE_COVERAGE:
        return {
            "sufficient": False,
            "date": as_of,
            "breadth": 0,
            "coverage": coverage,
            "pass": False,
            "valid_count": len(valid),
            "total_count": len(UNIVERSE),
            "missing_symbols": [
                sym for sym in UNIVERSE if sym not in valid
            ],
        }

    above_count = sum(
        close > sma20 for close, sma20 in valid.values()
    )
    breadth = above_count / len(valid) * 100
    return {
        "sufficient": True,
        "date": as_of,
        "breadth": breadth,
        "coverage": coverage,
        "pass": breadth >= 50.0,
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
                + [INDEX_SYMBOL]
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
                f"และเสี่ยงตาม Stop ไม่เกิน {RISK_PER_TRADE:.0%} "
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
                    f"{index}. 📈 {sym}\n"
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
                            equity, cash, close, stop, len(selected),
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
                "ต้องคำนวณใหม่ที่ราคาเปิดจริง"
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
                "และบอตยังไม่บันทึกวันซื้อขายจริง"
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
def read_overnight_plan(user_id):
    portfolio = get_portfolio(user_id)
    plan = portfolio.get("overnight_plan")

    if not isinstance(plan, dict):
        # ไม่ดึงหุ้นทั้งตลาดระหว่างตอบ LINE
        logger.warning(
            "ไม่พบแผนล่วงหน้า: "
            "ข้ามการคำนวณในคำขอ LINE"
        )
        return (
            "⚠️ ยังไม่มีแผนที่เตรียมไว้สำหรับวันนี้\n"
            "ระบบไม่ได้คำนวณหุ้นในระหว่างตอบ LINE "
            "เพื่อป้องกันคำขอหมดเวลา "
            "และจะไม่แนะนำซื้อโดยไม่มีข้อมูล"
            "ที่ตรวจสอบได้\n"
            "โปรดตรวจ Logs ของบริการว่ารอบเตรียมแผน"
            "หลังตลาดปิดทำงานสำเร็จหรือไม่"
        )

    if plan.get("status") == "NO_DATA":
        diagnostic = plan.get("diagnostic", {})
        coverage = float(
            diagnostic.get("coverage", 0) or 0
        )
        actual_date = (
            diagnostic.get("actual_date")
            or "ไม่พบข้อมูลดัชนี"
        )
        missing_sample = diagnostic.get(
            "missing_sample", []
        )
        response = (
            "⚠️ รอบล่าสุดยังสร้างแผนไม่ได้\n"
            f"วันที่ราคาที่พบ: {actual_date}\n"
            f"ข้อมูลหุ้นครบ {coverage:.0%} "
            f"({diagnostic.get('valid_count', 0)}/"
            f"{diagnostic.get('total_count', len(UNIVERSE))} ตัว)\n"
            f"สาเหตุ: {diagnostic.get('reason', 'ข้อมูลไม่ครบ')}"
        )
        if missing_sample:
            response += (
                "\nตัวอย่างหุ้นที่ข้อมูลขาด: "
                + ", ".join(missing_sample[:5])
            )
        return (
            response
            + "\nระบบจะลองใหม่ในรอบเตรียมแผนถัดไป"
        )

    today = now_bkk().date().isoformat()
    delivery_day = plan.get("delivery_day")
    status = plan.get("status")

    if delivery_day != today:
        if (
            status == "READY"
            and isinstance(delivery_day, str)
            and delivery_day > today
        ):
            return (
                "เตรียมแผนจากราคาปิด "
                f"{plan.get('data_date', 'ไม่ทราบ')} แล้ว\n"
                "รอส่งเช้าวันซื้อขาย "
                f"{delivery_day}"
            )
        return (
            "ยังไม่มีแผนที่ตรวจสอบได้สำหรับวันนี้ "
            "จึงไม่ใช้แผนเก่ามาแนะนำซื้อ"
        )

    if status == "READY":
        return (
            "แผนสำหรับวันนี้เตรียมแล้ว "
            "แต่ยังไม่ถึงรอบส่งอัตโนมัติ"
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


def prepare_overnight_plans():
    current_day = now_bkk().date()
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
        users = registered_users()
        logger.info(
            "รอบเตรียมแผน: registered=%d",
            len(users),
        )

        pending = {}
        read_failed = 0
        for user_id in users:
            try:
                portfolio = get_portfolio(user_id)
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

        symbols = set(UNIVERSE + [INDEX_SYMBOL])
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
        ctx = market_context(frames)

        if (
            not ctx["sufficient"]
            or ctx["date"]
            != current_day.isoformat()
        ):
            missing = ctx.get("missing_symbols", [])
            reason = (
                "ไม่มีข้อมูลดัชนี SET"
                if not ctx["date"]
                else (
                    "ข้อมูลหุ้นหลักไม่ครบ"
                    if not ctx["sufficient"]
                    else "วันที่ราคายังไม่ตรงกับวันที่ต้องใช้"
                )
            )
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
                    mutate_portfolio(
                        user_id,
                        lambda portfolio: portfolio.update(
                            {"overnight_plan": failure_plan}
                        ),
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
                    p["overnight_plan"] = new_plan
                    return "เตรียมแผนแล้ว"

                mutate_portfolio(
                    user_id, save_plan
                )
                prepared += 1
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
    skipped = 0

    for user_id in users:
        try:
            portfolio = get_portfolio(user_id)
            plan = portfolio.get(
                "overnight_plan"
            )
            if not isinstance(plan, dict):
                skipped += 1
                continue
            if (
                plan.get("delivery_day")
                != today.isoformat()
                or plan.get("status")
                != "READY"
            ):
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
        "failed=%d skipped=%d "
        "registered=%d",
        sent,
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


def help_text():
    return (
        "📊 PORTFOLIO BOT\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📈 แผนหุ้น\n"
        "แผนวันนี้ — ดูแผนหุ้นสำหรับวันซื้อขายนี้\n"
        "สถานะแผน — ดูวันที่ข้อมูลและสถานะเตรียมแผน\n"
        "หรือพิมพ์: แผนเทรดวันนี้ / "
        "ควรซื้อวันนี้ / ซื้อวันนี้\n\n"
        "💼 พอร์ตและรายการติดตาม\n"
        "ดูพอร์ต — ดูเงินสดและหุ้นที่บอตบันทึก\n"
        "รายการติดตาม — ดูหุ้นที่ติดตาม\n"
        "วิเคราะห์ COM7 — ดูแนวโน้มราคา"
        "และข้อมูลงบรายตัว\n"
        "ติดตาม AOT — เพิ่มหุ้นในรายการ"
        f"และใช้พิจารณาแผน (สูงสุด {MAX_WATCHLIST} หุ้น)\n"
        "เลิกติดตาม AOT — นำหุ้นออกจากรายการ\n\n"
        "❔ เมนูคำสั่ง\n"
        "พิมพ์: เมนู หรือ ช่วยเหลือ\n\n"
        "หมายเหตุ: บอตให้ข้อมูลประกอบ"
        "การตัดสินใจ ไม่ส่งคำสั่งซื้อขายจริง"
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
            f"📊 วิเคราะห์ {symbol}",
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

    if text in {
        "แผนเทรดวันนี้",
        "แผนวันนี้",
        "ควรซื้อวันนี้",
        "ซื้อวันนี้",
    }:
        return read_overnight_plan(
            user_id
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
                    f"• {sym}  |  "
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
                f"• {symbol}"
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
                return (
                    f"เพิ่ม {sym} "
                    "ลงในรายการติดตามแล้ว"
                )
            return (
                f"{sym} อยู่ในรายการ"
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
                return (
                    f"นำ {sym} ออกจาก"
                    "รายการติดตามแล้ว"
                )
            return (
                f"ไม่พบ {sym} "
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
scheduler = BackgroundScheduler(
    timezone="Asia/Bangkok"
)
scheduler.add_job(
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
scheduler.add_job(
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
scheduler.add_job(
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
scheduler.start()
logger.info(
    "เริ่ม scheduler สำหรับรอบเตรียม"
    "และส่งแผนแล้ว"
)

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
    )

