# bot.py (نسخه کامل + افزوده‌ها: DM Policy با /dm on|off|status، allow/block، ثبت کاربران،
# بدون حذف هیچ قابلیت قبلی: لانگ‌پولینگ، وبهوک-کلیر، رتری Flowise/DB،
# چانک پیام‌های طولانی، تایپینگ واقعی، تاریخچه JSONB، فیدبک 👍/👎)

import os
import logging
import time
import json
import io
import asyncio
from datetime import datetime, timezone
from typing import Optional, List, Set

import psycopg2
import psycopg2.extras
import requests

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
)

from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)


# ----------------------------
# Logging
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("root")

logging.getLogger("httpx").setLevel(os.getenv("LOG_LEVEL_HTTPX", "WARNING").upper())
logging.getLogger("telegram").setLevel(os.getenv("LOG_LEVEL_TELEGRAM", "INFO").upper())
logging.getLogger("telegram.ext").setLevel(os.getenv("LOG_LEVEL_TELEGRAM_EXT", "INFO").upper())

# ----------------------------
# ENV
# ----------------------------
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _parse_ids(env_value: Optional[str]) -> Set[int]:
    if not env_value:
        return set()
    if env_value.strip().lower() == "all":
        # علامت ویژه «همه مجازند»
        return {-1}
    out = set()
    for part in env_value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            pass
    return out

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

FLOWISE_BASE_URL = os.getenv("FLOWISE_BASE_URL", "").rstrip("/")
CHATFLOW_ID = os.getenv("CHATFLOW_ID")
FLOWISE_API_KEY = os.getenv("FLOWISE_API_KEY")
if not (FLOWISE_BASE_URL and CHATFLOW_ID):
    raise RuntimeError("FLOWISE_BASE_URL and CHATFLOW_ID must be set")

SESSION_TIMEOUT = _int_env("SESSION_TIMEOUT", 1800)
FLOWISE_TIMEOUT = _int_env("FLOWISE_TIMEOUT", 60)
FLOWISE_RETRIES = _int_env("FLOWISE_RETRIES", 3)
FLOWISE_BACKOFF_BASE_MS = _int_env("FLOWISE_BACKOFF_BASE_MS", 400)

DB_HOST = os.getenv("POSTGRES_BOT_HOST", "bot_db")
DB_PORT = _int_env("POSTGRES_BOT_PORT", 5432)
DB_NAME = os.getenv("POSTGRES_BOT_DB", "bot_db")
DB_USER = os.getenv("POSTGRES_BOT_USER", "bot_user")
DB_PASS = os.getenv("POSTGRES_BOT_PASSWORD", "password")

# --- DM Policy (جدید) ---
DM_POLICY = os.getenv("DM_POLICY", "db_or_env").strip().lower()  # env_only | db_only | db_or_env
ALLOWED_DM_ENV: Set[int] = _parse_ids(os.getenv("ALLOWED_DM_USER_IDS", ""))
PRIVATE_DENY_MESSAGE = os.getenv(
    "PRIVATE_DENY_MESSAGE",
    "❗️ من فقط در گروه تلگرامی «لیزرکاران پانته» پاسخ می‌دهم. لطفاً سوال‌تان را در گروه بپرسید."
)

# ادمین‌ها (برای /dm و /allow /block /users)
ADMIN_USER_IDS: Set[int] = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))

# ----------------------------
# Constants / Keyboards
# ----------------------------
CLEAR_BUTTON = "🧹 پاک کردن تاریخچه"
EXPORT_BUTTON = "📥 خروجی تاریخچه"
TG_MAX_MESSAGE = 4096

# ---- Unknown / Out-of-scope UX helpers ----
FALLBACK_HINTS = (
    "این پرسش در حوزه این ربات نیست",
    "این ربات در حال آموزش است",
    "پاسخی پیدا نشد",  
    "متوجه منظور نشدم"
)



def is_unknown_reply(txt: str) -> bool:
    if not txt:
        return False
    t = txt.strip()
    return any(h in t for h in FALLBACK_HINTS)


def unknown_keyboard(uq_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 ارسال برای آموزش ربات", callback_data=f"kb:report:{uq_id}")]
    ])


async def send_unknown_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, sid: str, uq_id: int):
    msg = (
    "❓ <b>پاسخ دقیقی پیدا نکردم</b>\n"
    "یا اینکه این سؤال خارج از حوزهٔ پاسخ‌گویی من است.\n"
    "لطفاً سؤال را کمی دقیق‌تر بنویس یا از دکمهٔ زیر برای ارسال جهت آموزش استفاده کن."
    )

    await update.message.reply_text(
        msg,
        reply_markup=unknown_keyboard(uq_id),
        parse_mode=ParseMode.HTML
    )


def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(CLEAR_BUTTON), KeyboardButton(EXPORT_BUTTON)]],
        resize_keyboard=True
    )

def feedback_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("👍", callback_data=f"fb:like:{session_id}"),
            InlineKeyboardButton("👎", callback_data=f"fb:dislike:{session_id}")
        ]]
    )

# ----------------------------
# DB Helpers
# ----------------------------
def _conn_once():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )

def db_conn():
    return _conn_once()

async def wait_for_db_ready(max_wait_sec: int = 60):
    deadline = time.time() + max_wait_sec
    attempt = 0
    while True:
        try:
            with _conn_once() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                log.info("Database is reachable.")
                return
        except Exception as e:
            attempt += 1
            if time.time() > deadline:
                log.exception("Database not reachable within timeout.")
                raise
            sleep_ms = min(2000, 200 + attempt * 150)
            log.warning(f"Waiting for DB... attempt={attempt}, err={e}, sleep={sleep_ms}ms")
            await asyncio.sleep(sleep_ms / 1000.0)

def ensure_tables():
    """جداول قبلی + جداول مدیریت کاربران/پالیسی DM (جدید)"""
    with db_conn() as conn, conn.cursor() as cur:
        # sessions/history/feedback (قدیمی)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            chat_id BIGINT PRIMARY KEY,
            current_session_id TEXT NOT NULL,
            last_activity TIMESTAMPTZ NOT NULL
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_history_log (
            session_id TEXT PRIMARY KEY,
            chat_id BIGINT,
            history JSONB NOT NULL
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS message_feedback (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            session_id TEXT NOT NULL,
            bot_message_id BIGINT NOT NULL,
            feedback TEXT NOT NULL CHECK (feedback IN ('like','dislike')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (chat_id, user_id, bot_message_id)
        );
        """)
        
        # فقط این یکی لازم است (برای شمارش/چک سریع هر پیام در یک چت)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_feedback_chat_msg
          ON message_feedback (chat_id, bot_message_id);
        """)


        # users (جدید)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            is_bot BOOLEAN,
            is_admin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ
        );
        """)

        # allowed_dm (جدید): لیست سفید DM از DB
        cur.execute("""
        CREATE TABLE IF NOT EXISTS allowed_dm (
            user_id BIGINT PRIMARY KEY,
            added_by BIGINT,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        # bot_config (جدید): برای سوییچ سریع /dm on|off بدون ری‌استارت
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        
        # unknown_questions: ثبت پرسش‌های بی‌پاسخ برای آموزش
        cur.execute("""
        CREATE TABLE IF NOT EXISTS unknown_questions (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT,
            user_id BIGINT,
            session_id TEXT,
            question TEXT,
            reported BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)

        conn.commit()
    log.info("All bot tables are ready.")

def upsert_user_from_update(update: Update):
    """ثبت/به‌روز کاربر در جدول users"""
    try:
        u = update.effective_user
        if not u:
            return
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, is_bot, is_admin, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET username=EXCLUDED.username,
                    first_name=EXCLUDED.first_name,
                    last_name=EXCLUDED.last_name,
                    is_bot=EXCLUDED.is_bot,
                    last_seen_at=NOW(),
                    updated_at=NOW();
            """, (
                u.id, u.username, u.first_name, u.last_name, u.is_bot,
                True if (ADMIN_USER_IDS and u.id in ADMIN_USER_IDS) else False
            ))
    except Exception as e:
        log.warning(f"upsert_user_from_update failed: {e}")

# --- bot_config helpers ---
def get_config(key: str) -> Optional[str]:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_config WHERE key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        log.warning(f"get_config failed: {e}")
        return None

def set_config(key: str, value: str):
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_config (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """, (key, value))
            conn.commit()
    except Exception as e:
        log.warning(f"set_config failed: {e}")

# --- DM Policy logic ---
def is_admin(user_id: int) -> bool:
    if user_id in ADMIN_USER_IDS:
        return True
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT is_admin FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
            return bool(row[0]) if row else False
    except Exception:
        return False

def is_user_in_db_allowlist(user_id: int) -> bool:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM allowed_dm WHERE user_id=%s", (user_id,))
            return cur.fetchone() is not None
    except Exception:
        return False

def is_dm_globally_on() -> bool:
    v = get_config("dm_global")  # 'on' | 'off' | None
    if v is None:
        # اگر کانفیگ در DB نبود، حالت پیش‌فرض: بر اساس ALLOWED_DM_USER_IDS
        # اگر 'all' در env بود → معادل on
        return (-1 in ALLOWED_DM_ENV)
    return v.lower() == "on"

def is_dm_allowed(user_id: int) -> bool:
    # اگر globally ON → همه مجازند
    if is_dm_globally_on():
        return True

    # globally OFF → فقط allowlist
    env_allows = (-1 in ALLOWED_DM_ENV) or (user_id in ALLOWED_DM_ENV)
    db_allows = is_user_in_db_allowlist(user_id)

    if DM_POLICY == "env_only":
        return env_allows
    elif DM_POLICY == "db_only":
        return db_allows
    else:  # db_or_env (پیش‌فرض)
        return env_allows or db_allows

# ----------------------------
# Session Logic (قدیمی)
# ----------------------------
def get_session(chat_id: int) -> Optional[dict]:
    with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT chat_id, current_session_id, last_activity FROM chat_sessions WHERE chat_id=%s", (chat_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def upsert_session(chat_id: int, session_id: str, last_activity: datetime):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO chat_sessions (chat_id, current_session_id, last_activity)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE
            SET current_session_id = EXCLUDED.current_session_id,
                last_activity = EXCLUDED.last_activity
        """, (chat_id, session_id, last_activity))
        conn.commit()

def update_last_activity(chat_id: int, ts: datetime):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE chat_sessions SET last_activity=%s WHERE chat_id=%s", (ts, chat_id))
        conn.commit()

def new_session_id(chat_id: int) -> str:
    return f"session_{chat_id}_{int(time.time())}"

def get_or_rotate_session(chat_id: int) -> str:
    now = datetime.now(timezone.utc)
    row = get_session(chat_id)
    if not row:
        sid = new_session_id(chat_id)
        upsert_session(chat_id, sid, now)
        log.info(f"New session created for chat {chat_id}: {sid}")
        return sid

    last_activity: datetime = row["last_activity"]
    sid: str = row["current_session_id"]
    elapsed = (now - last_activity).total_seconds()

    if elapsed > SESSION_TIMEOUT:
        sid = new_session_id(chat_id)
        upsert_session(chat_id, sid, now)
        log.info(f"Session timeout for chat {chat_id} after {elapsed:.0f}s. Rotated to {sid}")
        return sid

    update_last_activity(chat_id, now)
    return sid

def force_clear_session(chat_id: int) -> str:
    sid = new_session_id(chat_id)
    upsert_session(chat_id, sid, datetime.now(timezone.utc))
    log.info(f"History cleared for chat {chat_id}")
    return sid

# ----------------------------
# History/Feedback (قدیمی)
# ----------------------------
def get_local_history(session_id: str) -> list:
    history = []
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT history FROM chat_history_log WHERE session_id = %s", (session_id,))
            result = cur.fetchone()
            if result:
                history = result[0]
    except Exception as e:
        log.error(f"Failed to get local history for session {session_id}: {e}")
    return history

def save_local_history(session_id: str, chat_id: int, new_history_item: dict):
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_history_log (session_id, chat_id, history)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (session_id) DO UPDATE
                SET history = chat_history_log.history || %s::jsonb
            """, (
                session_id,
                chat_id,
                json.dumps([new_history_item]),
                json.dumps([new_history_item])
            ))
            conn.commit()
    except Exception as e:
        log.error(f"Failed to save local history for session {session_id}: {e}")

def save_feedback(chat_id: int, user_id: int, session_id: str, bot_message_id: int, feedback: str) -> bool:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO message_feedback (chat_id, user_id, session_id, bot_message_id, feedback)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chat_id, user_id, bot_message_id) DO NOTHING
            """, (chat_id, user_id, session_id, bot_message_id, feedback))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        log.error(f"Failed to save feedback: {e}")
        return False
def has_any_feedback_for_message(chat_id: int, bot_message_id: int) -> bool:
    """در خصوصی: بررسی اینکه برای این پیام «قبلاً هر نوع بازخوردی» ثبت شده یا نه"""
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM message_feedback WHERE chat_id=%s AND bot_message_id=%s LIMIT 1",
                (chat_id, bot_message_id),
            )
            return cur.fetchone() is not None
    except Exception:
        return False

def count_feedback(chat_id: int, bot_message_id: int) -> tuple[int, int]:
    """اختیاری: شمارش 👍 و 👎 برای نمایش روی دکمه‌ها در گروه‌ها"""
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT
                  SUM(CASE WHEN feedback='like' THEN 1 ELSE 0 END) AS likes,
                  SUM(CASE WHEN feedback='dislike' THEN 1 ELSE 0 END) AS dislikes
                FROM message_feedback
                WHERE chat_id=%s AND bot_message_id=%s
            """, (chat_id, bot_message_id))
            row = cur.fetchone() or (0, 0)
            return int(row[0] or 0), int(row[1] or 0)
    except Exception:
        return (0, 0)

def save_unknown_question(chat_id: int, user_id: int, session_id: str, question: str) -> int:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO unknown_questions (chat_id, user_id, session_id, question)
                VALUES (%s, %s, %s, %s)
                RETURNING id
            """, (chat_id, user_id, session_id, question))
            new_id = cur.fetchone()[0]
            conn.commit()
            return int(new_id)
    except Exception as e:
        log.warning(f"save_unknown_question failed: {e}")
        return 0

def mark_unknown_reported(uq_id: int) -> bool:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE unknown_questions SET reported=TRUE WHERE id=%s", (uq_id,))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        log.warning(f"mark_unknown_reported failed: {e}")
        return False

# ----------------------------
# Utils
# ----------------------------

def is_addressed_to_bot(update: Update, bot_username: str, bot_id: int) -> bool:
    msg = update.message
    if not msg:
        return False

    # 1) اگر ریپلای به پیام خود بات است
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == bot_id:
        return True

    # 2) اگر در متن @username آمده باشد (mention کلاسیک)
    text = (msg.text or "")
    if bot_username and ("@" + bot_username.lower()) in text.lower():
        return True

    # 3) اگر entity از نوع mention یا text_mention وجود داشته باشد
    for ent in (msg.entities or []):
        if ent.type == "mention":
            # متن entity را استخراج کنیم و با @botname بسنجیم
            ent_text = text[ent.offset: ent.offset + ent.length]
            if ent_text.lower() == ("@" + bot_username.lower()):
                return True
        elif ent.type == "text_mention" and ent.user and ent.user.id == bot_id:
            return True

    return False


def chunk_text(text: str, limit: int = TG_MAX_MESSAGE) -> List[str]:
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut == -1 or cut < int(limit * 0.6):
            cut = remaining.rfind(" ", 0, limit)
            if cut == -1 or cut < int(limit * 0.6):
                cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n ")
    return parts

# ----------------------------
# Flowise Client (blocking with retry)
# ----------------------------
def call_flowise(question: str, session_id: str) -> str:
    url = f"{FLOWISE_BASE_URL}/api/v1/prediction/{CHATFLOW_ID}"
    headers = {"Authorization": f"Bearer {FLOWISE_API_KEY}", "Content-Type": "application/json"}
    payload = {"question": question, "overrideConfig": {"sessionId": session_id}}
    for attempt in range(1, FLOWISE_RETRIES + 1):
        try:
            r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=FLOWISE_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                if data.get("text"):
                    return data["text"]
                res = data.get("result")
                if isinstance(res, dict) and res.get("text"):
                    return res["text"]
                if isinstance(res, list) and res and isinstance(res[0], dict) and res[0].get("text"):
                    return res[0]["text"]
            return "پاسخ نامعتبر از سرور دریافت شد."
        except Exception as e:
            if attempt < FLOWISE_RETRIES:
                backoff_ms = FLOWISE_BACKOFF_BASE_MS * (2 ** (attempt - 1))
                log.warning(f"Flowise request failed (attempt {attempt}/{FLOWISE_RETRIES}): {e}. retry in {backoff_ms}ms")
                time.sleep(backoff_ms / 1000.0)
            else:
                log.exception("Flowise request failed (no more retries)")
                break
    return "خطا در ارتباط با سرور هوش مصنوعی."

# ----------------------------
# Typing helpers
# ----------------------------
async def _typing_loop(bot, chat_id: int, action: ChatAction, stop_event: asyncio.Event, interval: float = 4.0):
    try:
        while not stop_event.is_set():
            await bot.send_chat_action(chat_id=chat_id, action=action)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
    except Exception as e:
        log.debug(f"Typing loop ended: {e}")

# ----------------------------
# Handlers
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    chat_id = update.effective_chat.id
    log.info(f"Start command in chat: {chat_id}")
    force_clear_session(chat_id)
    await update.message.reply_text(
        "سلام! من آماده‌ام 😊\nاز دکمه‌های زیر استفاده کن.",
        reply_markup=main_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    await update.message.reply_text(
        "دستورات:\n"
        "/start آغاز و ریست جلسه\n"
        "/clear پاک‌کردن تاریخچه (در گروه فقط ادمین)\n"
        "/export خروجی تاریخچه جلسه\n"
        "/help راهنما\n"
        "/whoami نمایش شناسه شما\n"
        "/ask <سؤال>            پرسیدن سؤال با دستور (در گروه یا خصوصی)\n"
        "\n— مدیران:\n"
        "/dm on | off | status  مدیریت پاسخ‌گویی در چت خصوصی\n"
        "/allow <user_id>       اضافه‌کردن کاربر به اجازه‌ی پیام خصوصی\n"
        "/block <user_id>       حذف کاربر از اجازه‌ی پیام خصوصی\n"
        "/users                 نمایش ۵۰ کاربر اخیر + وضعیت اجازه",
        reply_markup=main_keyboard()
    )

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user_from_update(update)
    await update.message.reply_text(
        f"User ID: {u.id}\nUsername: @{u.username if u.username else '-'}\nAdmin: {'✅' if is_admin(u.id) else '❌'}\nDM allowed now: {'✅' if is_dm_allowed(u.id) else '❌'}"
    )

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    chat = update.effective_chat
    user = update.effective_user
    if chat.type in ['group', 'supergroup']:
        try:
            admins = await context.bot.get_chat_administrators(chat.id)
            admin_ids = [admin.user.id for admin in admins]
            if user.id not in admin_ids:
                await update.message.reply_text("❌ فقط ادمین‌های گروه می‌توانند تاریخچه را پاک کنند.")
                return
        except Exception:
            await update.message.reply_text("❌ امکان بررسی ادمین‌ها وجود ندارد.")
            return
    force_clear_session(chat.id)
    await update.message.reply_text("تاریخچه پاک شد ✅", reply_markup=main_keyboard())

async def export_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    chat_id = update.effective_chat.id
    log.info(f"Export requested by chat: {chat_id}")

    stop_event = asyncio.Event()
    typing_task = context.application.create_task(
        _typing_loop(context.bot, chat_id, ChatAction.UPLOAD_DOCUMENT, stop_event)
    )
    try:
        session_row = get_session(chat_id)
        if not session_row:
            await update.message.reply_text("هنوز مکالمه‌ای برای خروجی گرفتن وجود ندارد.")
            return
        session_id = session_row["current_session_id"]
        history = get_local_history(session_id)
        if not history:
            await update.message.reply_text("تاریخچه این جلسه خالی است.")
            return
        formatted_text = f"تاریخچه مکالمه برای چت: {chat_id}\nSession ID: {session_id}\n"
        formatted_text += "="*40 + "\n\n"
        for item in history:
            speaker = "کاربر" if item.get("type") == "human" else "ربات"
            message = item.get("message", "")
            formatted_text += f"[{speaker}]:\n{message}\n\n"
        me = context.application.bot_data.get("me") or await context.bot.get_me()
        bot_name = me.full_name
        bot_username = me.username
        signature = "\n" + "="*40 + f"\nخروجی گرفته شده توسط ربات:\nنام: {bot_name}\nآیدی: @{bot_username}\n"
        formatted_text += signature
        file_in_memory = io.BytesIO(formatted_text.encode('utf-8'))
        safe_session = session_id.replace(':', '_')
        file_name = f"chat_history_{chat_id}_{safe_session}.txt"
        await context.bot.send_document(
            chat_id=chat_id,
            document=file_in_memory,
            filename=file_name,
            caption="این هم خروجی تاریخچه جلسه فعلی شما."
        )
    finally:
        stop_event.set()
        try:
            await typing_task
        except Exception:
            pass

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # مثل on_message، اول کاربر را ثبت/آپدیت می‌کنیم
    upsert_user_from_update(update)

    chat = update.effective_chat
    u = update.effective_user

    # متن سوال پس از /ask
    text = " ".join(context.args or []).strip()

    # اگر بعد از /ask چیزی نیامده بود، از پیامِ ریپلای (text/caption) بخوان
    if not text and update.message and update.message.reply_to_message:
        src = update.message.reply_to_message
        text = (src.text or src.caption or "").strip()


    # چک سیاست DM در چت خصوصی را «زودتر» انجام بده تا ForceReply در خصوصیِ غیرمجاز داده نشود
    if chat.type == 'private' and not is_dm_allowed(u.id):
        return await update.message.reply_text(PRIVATE_DENY_MESSAGE)

    # اگر هنوز متنی نیست → به‌جای «فرمت استفاده»، ForceReply بده تا کاربر همان‌جا سوالش را بنویسد
    if not text:
        placeholder = "سوالت رو همینجا بنویس 👇"
        return await update.message.reply_text(
            placeholder,
            reply_markup=ForceReply(
                input_field_placeholder="مثال: قیمت برش لیزر پلکسی ۳ میل؟",
                selective=True  # فقط همان کسی که دستور را زد، Prompt را می‌بیند
            )
        )

    # در گروه‌ها، /ask همیشه مجاز است (نیاز به منشن/ریپلای نیست چون دستور است)
    sid = get_or_rotate_session(chat.id)

    # تایپینگ
    stop_event = asyncio.Event()
    typing_task = context.application.create_task(
        _typing_loop(context.bot, chat.id, ChatAction.TYPING, stop_event)
    )
    try:
        reply_text = await asyncio.to_thread(call_flowise, text, sid)
        # اگر پاسخ، fallback بود → تجربهٔ بهتر + لاگ آموزشی
        if is_unknown_reply(reply_text):
            uq_id = save_unknown_question(chat.id, u.id, sid, text)
            await send_unknown_reply(update, context, sid, uq_id)
            return
    finally:
        stop_event.set()
        try:
            await typing_task
        except Exception:
            pass


    # ثبت تاریخچه مثل on_message
    save_local_history(sid, chat.id, {"type": "human", "message": text})
    save_local_history(sid, chat.id, {"type": "ai", "message": reply_text})

    # ارسال پاسخ با چانک و دکمهٔ فیدبک
    chunks = chunk_text(reply_text, TG_MAX_MESSAGE)
    if not chunks:
        chunks = ["(پاسخی دریافت نشد)"]
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            await update.message.reply_text(chunk, reply_markup=feedback_keyboard(sid))
        else:
            await update.message.reply_text(chunk)
            
    # --- Clean up the ForceReply prompt (if the user answered to it) ---
    try:
        me = context.application.bot_data.get("me") or await context.bot.get_me()
        fr = update.message.reply_to_message
        # اگر این پیام، ریپلای به پیام خودِ ربات بود، و متنش همان پرامپت ForceReply ماست، پرامپت را پاک کن
        if fr and fr.from_user and fr.from_user.id == me.id:
            prompt_text = (fr.text or "")
            if prompt_text.startswith("سوالت رو همینجا بنویس"):
                await context.bot.delete_message(chat_id=chat.id, message_id=fr.message_id)
    except Exception:
        # ممکن است ربات دسترسی حذف نداشته باشد؛ مشکلی نیست.
        pass



async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    chat = update.effective_chat
    u = update.effective_user
    text = (update.message.text or "").strip()
    if not text:
        return

    # شرتکات‌های کیبورد Reply
    if text == CLEAR_BUTTON:
        return await clear_history(update, context)
    if text == EXPORT_BUTTON:
        return await export_history(update, context)

    # در گروه‌ها: فقط در صورت رپلای به ربات یا منشن
    # --- داخل on_message ، بخش گروه‌ها ---
    is_group = chat.type in ['group', 'supergroup']
    if is_group:
        me = context.application.bot_data.get("me")
        if not me:
            me = await context.bot.get_me()
            context.application.bot_data["me"] = me
        bot_user = me

        if not is_addressed_to_bot(update, bot_user.username or "", bot_user.id):
            return

        # اگر در متن @bot آمده، حذفش کن تا سوال تمیز به Flowise برسد
        if bot_user.username:
            text = text.replace(f"@{bot_user.username}", "").strip()
    else:
        # چت خصوصی: اعمال سیاست DM
        if not is_dm_allowed(u.id):
            await update.message.reply_text(PRIVATE_DENY_MESSAGE)
            return

    log.info(f"Received message: '{text}' from chat: {chat.id}")
    sid = get_or_rotate_session(chat.id)

    stop_event = asyncio.Event()
    typing_task = context.application.create_task(
        _typing_loop(context.bot, chat.id, ChatAction.TYPING, stop_event)
    )
    try:
        reply_text = await asyncio.to_thread(call_flowise, text, sid)
        if is_unknown_reply(reply_text):
            uq_id = save_unknown_question(chat.id, u.id, sid, text)
            await send_unknown_reply(update, context, sid, uq_id)
            return
    finally:
        stop_event.set()
        try:
            await typing_task
        except Exception:
            pass

    save_local_history(sid, chat.id, {"type": "human", "message": text})
    save_local_history(sid, chat.id, {"type": "ai", "message": reply_text})

    chunks = chunk_text(reply_text, TG_MAX_MESSAGE)
    if not chunks:
        chunks = ["(پاسخی دریافت نشد)"]
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            await update.message.reply_text(chunk, reply_markup=feedback_keyboard(sid))
        else:
            await update.message.reply_text(chunk)
            
    # --- Clean up the ForceReply prompt (if the user answered to it) ---
    try:
        me = context.application.bot_data.get("me") or await context.bot.get_me()
        fr = update.message.reply_to_message
        # اگر این پیام، ریپلای به پیام خودِ ربات بود، و متنش همان پرومپت ForceReply ماست، پرومپت را پاک کن
        if fr and fr.from_user and fr.from_user.id == me.id:
            prompt_text = (fr.text or "")
            if prompt_text.startswith("سوالت رو همینجا بنویس"):
                await context.bot.delete_message(chat_id=chat.id, message_id=fr.message_id)
    except Exception:
        # حذف پیام ممکن است در گروه‌هایی که ربات دسترسی حذف ندارد شکست بخورد؛ اشکالی ندارد.
        pass

async def on_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if not cq or not cq.data:
        return
    try:
        action, feedback, session_id = cq.data.split(":", 3)
    except ValueError:
        return await cq.answer("داده نامعتبر.", show_alert=False)
    if action != "fb" or feedback not in ("like", "dislike"):
        return await cq.answer("نامعتبر.", show_alert=False)

    chat = cq.message.chat
    chat_id = chat.id
    user_id = cq.from_user.id
    bot_message_id = cq.message.message_id

    # حالت «خصوصی»: هر پیام فقط یک بازخورد کلی داشته باشد (از هر کس)
    if chat.type == 'private':
        # اگر قبلاً هر بازخوردی برای این پیام ثبت شده، اجازه نده
        if has_any_feedback_for_message(chat_id, bot_message_id):
            return await cq.answer("برای این پاسخ قبلاً یک بازخورد ثبت شده است.", show_alert=False)

        created = save_feedback(chat_id, user_id, session_id, bot_message_id, feedback)
        if created:
            # در خصوصی، چون فقط یک نفر هست، دکمه‌ها را حذف کن
            try:
                await cq.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return await cq.answer("بازخوردت ثبت شد. ممنون 🙏", show_alert=False)
        else:
            return await cq.answer("برای این پاسخ قبلاً بازخورد ثبت شده است.", show_alert=False)

    # حالت «گروه/سوپرگروه»: هر کاربر فقط یک‌بار برای هر پیام؛ افراد متعدد آزادند
    created = save_feedback(chat_id, user_id, session_id, bot_message_id, feedback)
    if created:
        # (اختیاری) شمارش را روی دکمه‌ها نشان بده
        try:
            likes, dislikes = count_feedback(chat_id, bot_message_id)
            await cq.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"👍 {likes}", callback_data=f"fb:like:{session_id}"),
                    InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"fb:dislike:{session_id}")
                ]])
            )
        except Exception:
            # اگر امکان ویرایش نبود (مثلاً پیام خیلی قدیمی)، بی‌خیال
            pass
        return await cq.answer("ثبت شد ✅", show_alert=False)
    else:
        return await cq.answer("تو قبلاً برای این پیام رأی داده‌ای.", show_alert=False)

async def on_unknown_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if not cq or not cq.data:
        return

    data = cq.data

    # فقط گزارش برای آموزش
    if data.startswith("kb:report:"):
        try:
            uq_id = int(data.split(":", 2)[2])
        except Exception:
            return await cq.answer("داده نامعتبر.", show_alert=False)

        ok = mark_unknown_reported(uq_id)
        log.info(f"Unknown question reported: id={uq_id}, ok={ok}")
        if ok:
            await cq.answer("ثبت شد. ممنون! ✅", show_alert=False)
            try:
                await cq.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        else:
            await cq.answer("خطا در ثبت.", show_alert=False)
    else:
        # از نظر تئوری نباید به اینجا برسیم چون هندلر با pattern فقط report را می‌گیرد.
        await cq.answer("دکمه معتبر نیست.", show_alert=False)


    # گزارش برای آموزش
    if data.startswith("kb:report:"):
        try:
            uq_id = int(data.split(":", 2)[2])
        except Exception:
            return await cq.answer("داده نامعتبر.", show_alert=False)
        ok = mark_unknown_reported(uq_id)
        log.info(f"Unknown question reported: id={uq_id}, ok={ok}")
        if ok:
            await cq.answer("ثبت شد. ممنون! ✅", show_alert=False)
            try:
                await cq.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        else:
            await cq.answer("خطا در ثبت.", show_alert=False)

# ----------------------------
# Admin commands (جدید)
# ----------------------------
def _require_admin(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    if is_admin(u.id):
        return True
    # اگر در گروه، اجازه می‌دهیم ادمین‌های گروه هم دستورات را بزنند
    chat = update.effective_chat
    if chat and chat.type in ['group', 'supergroup'] and update.message:
        # این تابع async است؛ برای سادگی، در اینجا فقط is_admin پایگاه داده را چک کردیم.
        # دستورات مدیریتی بهتر است در خصوصی اجرا شوند.
        return False
    return False

async def dm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not _require_admin(update):
        return await update.message.reply_text("❌ فقط ادمین مجاز است.")
    args = context.args or []
    if not args:
        return await update.message.reply_text("استفاده: /dm on | off | status")
    sub = args[0].lower()
    if sub == "status":
        await update.message.reply_text(f"DM Global: {'ON' if is_dm_globally_on() else 'OFF'}\nPolicy: {DM_POLICY}\nENV Allow: {'all' if (-1 in ALLOWED_DM_ENV) else (','.join(map(str, ALLOWED_DM_ENV)) or 'None')}")
        return
    if sub in ("on", "off"):
        set_config("dm_global", sub)
        await update.message.reply_text(f"DM Global تنظیم شد: {sub.upper()}")
    else:
        await update.message.reply_text("استفاده: /dm on | off | status")

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not _require_admin(update):
        return await update.message.reply_text("❌ فقط ادمین مجاز است.")
    target_id: Optional[int] = None
    args = context.args or []
    if args:
        try:
            target_id = int(args[0])
        except Exception:
            pass
    if not target_id and update.message and update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    if not target_id:
        return await update.message.reply_text("استفاده: /allow <user_id> (یا روی پیام کاربر ریپلای کنید)")
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO allowed_dm (user_id, added_by)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO NOTHING
            """, (target_id, update.effective_user.id))
            conn.commit()
        await update.message.reply_text(f"✅ کاربر {target_id} به لیست مجاز DM اضافه شد.")
    except Exception as e:
        await update.message.reply_text(f"خطا در allow: {e}")

async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not _require_admin(update):
        return await update.message.reply_text("❌ فقط ادمین مجاز است.")
    target_id: Optional[int] = None
    args = context.args or []
    if args:
        try:
            target_id = int(args[0])
        except Exception:
            pass
    if not target_id and update.message and update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    if not target_id:
        return await update.message.reply_text("استفاده: /block <user_id> (یا روی پیام کاربر ریپلای کنید)")
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM allowed_dm WHERE user_id=%s", (target_id,))
            conn.commit()
        await update.message.reply_text(f"🚫 کاربر {target_id} از لیست مجاز DM حذف شد.")
    except Exception as e:
        await update.message.reply_text(f"خطا در block: {e}")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not _require_admin(update):
        return await update.message.reply_text("❌ فقط ادمین مجاز است.")
    try:
        with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT u.user_id, u.username, u.first_name, u.last_name,
                       COALESCE((SELECT 1 FROM allowed_dm a WHERE a.user_id=u.user_id), 0) AS dm_ok,
                       u.last_seen_at
                FROM users u
                ORDER BY u.last_seen_at DESC NULLS LAST
                LIMIT 50
            """)
            rows = cur.fetchall()
        if not rows:
            return await update.message.reply_text("هیچ کاربری ثبت نشده است.")
        lines = ["لیست ۵۰ کاربر اخیر:"]
        for r in rows:
            uid = r["user_id"]
            uline = f"- {uid} | @{r['username'] or '-'} | {r['first_name'] or ''} {r['last_name'] or ''} | DM:{'✅' if r['dm_ok'] == 1 else '❌'} | seen:{r['last_seen_at'] or '-'}"
            lines.append(uline)
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"خطا در users: {e}")

# ----------------------------
# Error Handler
# ----------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception(f"Unhandled error: {context.error}")

# ----------------------------
# Startup / Run
# ----------------------------
async def _on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (if existed).")
    except Exception as e:
        log.warning(f"delete_webhook failed (ignored): {e}")

    await wait_for_db_ready(max_wait_sec=90)
    ensure_tables()

    # --- Cache bot info once (saves an API call per message) ---
    me = await app.bot.get_me()
    app.bot_data["me"] = me
    log.info(f"Cached bot info: @{me.username} (id={me.id})")


def run():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("export", export_history))
    app.add_handler(CommandHandler("ask", ask_cmd))
    # Admin
    app.add_handler(CommandHandler("dm", dm_cmd))
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    # Feedback buttons
    app.add_handler(CallbackQueryHandler(on_feedback, pattern=r"^fb:"))
    app.add_handler(CallbackQueryHandler(on_unknown_buttons, pattern=r"^kb:report:\d+$"))
    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    # Errors
    app.add_error_handler(on_error)
    # Startup
    app.post_init = _on_startup
    log.info("Bot is starting to poll...")
    app.run_polling(
        poll_interval=0,
        timeout=50,
        drop_pending_updates=True,
        allowed_updates=None
    )

if __name__ == "__main__":
    run()
