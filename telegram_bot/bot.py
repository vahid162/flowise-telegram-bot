# bot.py
import os
import logging
import time
import json
from datetime import datetime, timezone, timedelta
import psycopg2
import psycopg2.extras
import requests

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackContext, filters

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("root")

# ----------------------------
# ENV
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

FLOWISE_BASE_URL = os.getenv("FLOWISE_BASE_URL", "").rstrip("/")
CHATFLOW_ID = os.getenv("CHATFLOW_ID")
FLOWISE_API_KEY = os.getenv("FLOWISE_API_KEY")

if not (FLOWISE_BASE_URL and CHATFLOW_ID and FLOWISE_API_KEY):
    raise RuntimeError("FLOWISE_BASE_URL / CHATFLOW_ID / FLOWISE_API_KEY must be set")

# timeout in seconds
try:
    SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", "1800"))
except Exception:
    SESSION_TIMEOUT = 1800

# --- Postgres (DB for bot) ---
DB_HOST = os.getenv("POSTGRES_BOT_HOST", "telegram_bot_db")  # name of bot db service in docker-compose
DB_PORT = int(os.getenv("POSTGRES_BOT_PORT", "5432"))
DB_NAME = os.getenv("POSTGRES_BOT_DB", "bot_db")
DB_USER = os.getenv("POSTGRES_BOT_USER", "bot_user")
DB_PASS = os.getenv("POSTGRES_BOT_PASSWORD", "password")

# ----------------------------
# DB Helpers
# ----------------------------
def db_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT,
        dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )

def ensure_tables():
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_sessions (
            user_id BIGINT PRIMARY KEY,
            current_session_id TEXT NOT NULL,
            last_activity TIMESTAMPTZ NOT NULL
        );
        """)
        conn.commit()
    log.info("Bot database tables are ready.")

def get_session(user_id: int) -> dict | None:
    with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT user_id, current_session_id, last_activity FROM user_sessions WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

def upsert_session(user_id: int, session_id: str, last_activity: datetime):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO user_sessions (user_id, current_session_id, last_activity)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE
            SET current_session_id = EXCLUDED.current_session_id,
                last_activity = EXCLUDED.last_activity
        """, (user_id, session_id, last_activity))
        conn.commit()

def update_last_activity(user_id: int, ts: datetime):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE user_sessions SET last_activity=%s WHERE user_id=%s", (ts, user_id))
        conn.commit()

# ----------------------------
# Session Logic
# ----------------------------
def new_session_id(user_id: int) -> str:
    # unique and short; changing this resets memory
    return f"{user_id}:{int(time.time())}"

def get_or_rotate_session(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    row = get_session(user_id)
    if not row:
        sid = new_session_id(user_id)
        upsert_session(user_id, sid, now)
        log.info(f"New session created for user {user_id}: {sid}")
        return sid

    last_activity: datetime = row["last_activity"]
    sid: str = row["current_session_id"]

    elapsed = (now - last_activity).total_seconds()
    if elapsed > SESSION_TIMEOUT:
        # rotate
        sid = new_session_id(user_id)
        upsert_session(user_id, sid, now)
        log.info(f"Session timeout for user {user_id} after {elapsed:.0f}s. Rotated to {sid}")
        return sid

    # still active
    update_last_activity(user_id, now)
    return sid

def force_clear_session(user_id: int) -> str:
    sid = new_session_id(user_id)
    upsert_session(user_id, sid, datetime.now(timezone.utc))
    log.info(f"History cleared for user {user_id}")
    return sid

# ----------------------------
# Flowise Client
# ----------------------------
def call_flowise(question: str, session_id: str) -> str:
    url = f"{FLOWISE_BASE_URL}/api/v1/prediction/{CHATFLOW_ID}"
    headers = {
        "Authorization": f"Bearer {FLOWISE_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "question": question,
        "overrideConfig": {
            # IMPORTANT: Buffer Memory will isolate per sessionId
            "sessionId": session_id
        }
    }
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=60)
        r.raise_for_status()
        data = r.json()

        # Robust extraction (Flowise variants)
        text = (
            data.get("text")
            or (data.get("result", {}) if isinstance(data.get("result"), dict) else {}).get("text")
            or (data["result"][0]["text"] if isinstance(data.get("result"), list) and data["result"] else None)
        )
        if not text:
            text = "پاسخ نامعتبر از سرور دریافت شد."
        return text
    except Exception as e:
        log.exception("Flowise request failed")
        return f"خطا در ارتباط با سرور: {e}"

# ----------------------------
# Telegram Handlers
# ----------------------------
CLEAR_BUTTON = "🧹 پاک کردن تاریخچه"

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton(CLEAR_BUTTON)]],
        resize_keyboard=True
    )

async def start(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    log.info(f"Start command by user: {user_id}")
    force_clear_session(user_id)
    await update.message.reply_text(
        "سلام! من آماده‌ام 😊\nهر زمان خواستی با دکمهٔ زیر تاریخچه رو پاک کن.",
        reply_markup=main_keyboard()
    )

async def clear_history(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    force_clear_session(user_id)
    await update.message.reply_text("تاریخچه پاک شد ✅", reply_markup=main_keyboard())

async def on_message(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()

    # manual clear via button
    if text == CLEAR_BUTTON:
        return await clear_history(update, context)

    log.info(f"Received message: '{text}' from user: {user_id}")

    # get/rotate session based on timeout
    sid = get_or_rotate_session(user_id)

    # call Flowise with sessionId
    reply = call_flowise(text, sid)

    # update last activity (successful or not)
    update_last_activity(user_id, datetime.now(timezone.utc))

    # log + send
    log.info(f"Successfully logged conversation for user {user_id}")
    await update.message.reply_text(reply, reply_markup=main_keyboard())

def run():
    ensure_tables()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Bot is starting to poll...")
    app.run_polling()

if __name__ == "__main__":
    run()
