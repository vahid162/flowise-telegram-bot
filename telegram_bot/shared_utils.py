import os
import logging
import time
import re
import json
import asyncio
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from typing import Optional, List, Set, Tuple
from functools import wraps
from telegram import Update
from telegram import ReplyKeyboardRemove
from flowise_client import call_flowise as _flowise_call
from inspect import iscoroutinefunction
from telegram.error import BadRequest

from contextlib import contextmanager
from psycopg2.pool import ThreadedConnectionPool


# --- Observability: Prometheus metrics (Phase 2) --------------------------------
# اگر METRICS_ENABLED خاموش باشد، از Noop استفاده می‌کنیم تا رگرسیونی پیش نیاید.
try:
    _METRICS_ENABLED = str(os.getenv("METRICS_ENABLED", "0")).strip().lower() in ("1", "true", "on", "yes")
    if _METRICS_ENABLED:
        from prometheus_client import Counter, Histogram, Gauge

        # Latency تماس Flowise (ثانیه) - با لیبل کم‌تنوع
        MET_FLOWISE_LATENCY = Histogram(
            "flowise_request_seconds",
            "Latency of Flowise chatflow calls in seconds",
            ["dst"],  # dst ∈ {private, group, unknown}
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
        )

        # تعداد پاسخ‌های بات (به ازای مقصد)
        MET_BOT_REPLIES = Counter(
            "bot_replies_total",
            "Number of bot replies sent",
            ["dst"],
        )

        # تعداد سؤال‌های نامعلوم (بدون لیبل تا کاردینالیتی زیاد نشه)
        MET_UNKNOWN_QUESTIONS = Counter(
            "unknown_questions_total",
            "Number of unknown/empty-source answers",
        )

        # سلامت warmup Flowise (۱=Up, ۰=Down)
        MET_FLOWISE_UP = Gauge(
            "flowise_up",
            "Flowise warmup heartbeat (1=up, 0=down)"
        )

        # --- AdsGuard actions: warn/delete/none
        MET_ADS_ACTION = Counter(
            "ads_action_total",
            "Number of AdsGuard decisions by action",
            ["action"],   # action ∈ {warn, delete, none}
        )

        # --- Bot errors (handler exceptions)
        MET_BOT_ERRORS = Counter(
            "bot_errors_total",
            "Number of errors in bot handlers"
        )
    else:
        class _Noop:
            def labels(self, *a, **k): return self
            def inc(self, *a, **k): return None
            def observe(self, *a, **k): return None
            def set(self, *a, **k): return None
        MET_FLOWISE_LATENCY = MET_BOT_REPLIES = MET_UNKNOWN_QUESTIONS = MET_FLOWISE_UP = MET_ADS_ACTION = MET_BOT_ERRORS = _Noop()

except Exception:
    import logging as _lg
    _lg.getLogger(__name__).exception("Metrics init failed")
    class _Noop:
        def labels(self, *a, **k): return self
        def inc(self, *a, **k): return None
        def observe(self, *a, **k): return None
        def set(self, *a, **k): return None
    MET_FLOWISE_LATENCY = MET_BOT_REPLIES = MET_UNKNOWN_QUESTIONS = MET_FLOWISE_UP = MET_ADS_ACTION = MET_BOT_ERRORS = _Noop()
# ------------------------------------------------------------------------------



def _t_runtime(key: str, chat_id=None, **vars):
    """
    Lazy import از messages_service.t برای شکستن چرخهٔ ایمپورت.
    اگر به هر دلیل messages_service هنوز آماده نبود، یک فالبک امن برمی‌گردانیم.
    """
    try:
        from messages_service import t as _t
        return _t(key, chat_id=chat_id, **vars)
    except Exception:
        # فالبک حداقلی: خودِ کلید یا یک متن کوتاه
        return key if isinstance(key, str) else "⏳ لطفاً کمی بعد دوباره تلاش کنید."


# شناسه ادمین ناشناس در گروه‌ها (نسخه‌های مختلف PTB)
try:
    from telegram.constants import ANONYMOUS_ADMIN as TG_ANON  # PTB v20+
except Exception:
    try:
        from telegram.constants import ANONYMOUS_ADMIN_ID as TG_ANON  # PTB v13
    except Exception:
        TG_ANON = 1087968824  # fallback
        
        
# --- Word count helper (Unicode-aware) ---
def count_words(s: str) -> int:
    """
    تعداد «کلمه» را در رشته می‌شمارد (فارسی/لاتین).
    - HTML/تگ‌ها حذف می‌شود.
    - دنباله‌های حرف/رقم/خط‌زیر (\\w) کلمه محسوب می‌شود.
    - ایموجی و علائم نقطه‌گذاری «کلمه» حساب نمی‌شوند.
    """
    if not s:
        return 0
    # حذف تگ‌های احتمالی
    s = re.sub(r"<[^>]+>", " ", s)
    # \w در پایتون یونیکد-آگاه است (فارسی/عربی/لاتین را می‌گیرد)
    words = re.findall(r"\w+", s, flags=re.UNICODE)
    # حذف مواردی که فقط خط‌زیر باشند (نادر ولی امن)
    return sum(1 for w in words if w.strip("_"))



# --- ADD: exception logging decorator ---
def log_exceptions(fn):
    """Log exceptions with stacktrace + enriched Update context, then re-raise."""
    from logging_setup import update_log_context
    lg = logging.getLogger(getattr(fn, "__module__", "__name__"))

    if iscoroutinefunction(fn):
        @wraps(fn)
        async def _aw(*args, **kwargs):
            upd = next((a for a in args if isinstance(a, Update)), None)
            if upd:
                update_log_context(upd, op=fn.__name__)
            try:
                return await fn(*args, **kwargs)
            except Exception:
                lg.exception("Unhandled exception in %s", fn.__name__)
                raise
        return _aw
    else:
        @wraps(fn)
        def _w(*args, **kwargs):
            upd = next((a for a in args if isinstance(a, Update)), None)
            if upd:
                update_log_context(upd, op=fn.__name__)
            try:
                return fn(*args, **kwargs)
            except Exception:
                lg.exception("Unhandled exception in %s", fn.__name__)
                raise
        return _w
# --- END ADD ---


# --- Security helpers & admin throttle (Hardening Phase F) ---

def is_forwarded_message(msg) -> bool:
    """
    تشخیص «فوروارد» یا «اتوفوروارد» برای سازگاری با نسخه‌های جدید/قدیم Bot API/PTB.
    - جدید: msg.forward_origin (PTB>=20.8) یا msg.is_automatic_forward
    - قدیم: forward_date / forward_from / forward_from_chat
    """
    try:
        if getattr(msg, "is_automatic_forward", False):
            return True
        if getattr(msg, "forward_origin", None) is not None:
            return True
        # backward-compat fields
        if getattr(msg, "forward_date", None) is not None:
            return True
        if getattr(msg, "forward_from", None) is not None:
            return True
        if getattr(msg, "forward_from_chat", None) is not None:
            return True
    except Exception:
        pass
    return False


# درجا و سبک: محدود کردن فراخوانی دستورات مدیریتی توسط یک کاربر در بازهٔ کوتاه
_ADMIN_LAST_CALL: dict[tuple[int, str], float] = {}

def admin_throttle(window_sec: int = 2):
    """
    دکوراتور Throttle برای اوامر مدیریتی (سوپرادمین/ادمین).
    کلید: (user_id, function_name) → جلوگیری از spam/flood.
    پیام خطا: از i18n کلید 'errors.rate_limited' استفاده می‌کند.
    """
    from functools import wraps
    from inspect import iscoroutinefunction
    import time as _time

    def _decorator(func):
        if iscoroutinefunction(func):
            @wraps(func)
            async def _wrapped(update, context, *args, **kwargs):
                u = getattr(update, "effective_user", None)
                uid = int(getattr(u, "id", 0) or 0)
                key = (uid, func.__name__)
                now = _time.monotonic()
                last = _ADMIN_LAST_CALL.get(key, 0.0)
                if now - last < window_sec:
                    # پیام مودبانه و کوتاه (i18n)
                    try:
                        await safe_reply_text(
                            update,
                            _t_runtime("errors.rate_limited",
                                       chat_id=update.effective_chat.id if update.effective_chat else None)
                        )

                    except Exception:
                        pass
                    return
                _ADMIN_LAST_CALL[key] = now
                return await func(update, context, *args, **kwargs)
            return _wrapped
        else:
            @wraps(func)
            def _wrapped(update, context, *args, **kwargs):
                u = getattr(update, "effective_user", None)
                uid = int(getattr(u, "id", 0) or 0)
                key = (uid, func.__name__)
                now = _time.monotonic()
                last = _ADMIN_LAST_CALL.get(key, 0.0)
                if now - last < window_sec:
                    # توابع sync هم در این پروژه کم‌اند، ولی برای کامل‌بودن نگه داشتیم
                    return None
                _ADMIN_LAST_CALL[key] = now
                return func(update, context, *args, **kwargs)
            return _wrapped
    return _decorator

# --- END Security helpers & admin throttle ---



# تنظیمات و متغیرهای محیطی
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _parse_ids(env_value: Optional[str]) -> Set[int]:
    if not env_value:
        return set()
    if env_value.strip().lower() == "all":
        # اگر مقدار "all" باشد یعنی همه مجازند
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

# --- AFTER (shared_utils.py) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

FLOWISE_BASE_URL = os.getenv("FLOWISE_BASE_URL", "").rstrip("/")
FLOWISE_API_KEY = os.getenv("FLOWISE_API_KEY")

# ⚠️ CHATFLOW_ID دیگر الزامی نیست؛ DB-first:
# در runtime از bot_config.chat_ai_default_chatflow_id یا per-chat override استفاده می‌کنیم.
CHATFLOW_ID = os.getenv("CHATFLOW_ID")  # اختیاری؛ فقط به عنوان fallback نهایی
PV_CHATFLOW_ID = os.getenv("PV_CHATFLOW_ID")  # چت‌فلو اختصاصی پی‌وی (fallback اگر DB تنظیم نباشد)

if not FLOWISE_BASE_URL:
    raise RuntimeError("FLOWISE_BASE_URL must be set")


SESSION_TIMEOUT = _int_env("SESSION_TIMEOUT", 1800)
FLOWISE_TIMEOUT = _int_env("FLOWISE_TIMEOUT", 60)
FLOWISE_RETRIES = _int_env("FLOWISE_RETRIES", 3)
FLOWISE_BACKOFF_BASE_MS = _int_env("FLOWISE_BACKOFF_BASE_MS", 400)

# === Shared defaults: single source of truth ===
CHAT_AI_DEFAULT_ENABLED = os.getenv("CHAT_AI_DEFAULT_ENABLED", "off").strip().lower()
CHAT_AI_DEFAULT_MODE = os.getenv("CHAT_AI_DEFAULT_MODE", "mention").strip().lower()
CHAT_AI_DEFAULT_MIN_GAP_SEC = os.getenv("CHAT_AI_DEFAULT_MIN_GAP_SEC", "2").strip()
# پاک‌سازی خودکار پیام‌های راهنمای Chat AI (0 = off)
CHAT_AI_DEFAULT_AUTOCLEAN_SEC = os.getenv("CHAT_AI_AUTOCLEAN_SEC", "0").strip()


# Ads defaults (used by UI + runtime)
ADS_DEFAULT_FEATURE = os.getenv("ADS_FEATURE", "off").strip().lower()
ADS_DEFAULT_ACTION = os.getenv("ADS_ACTION", "none").strip().lower()  # none|warn|delete
ADS_DEFAULT_THRESHOLD = os.getenv("ADS_THRESHOLD", "0.78").strip()
ADS_DEFAULT_MAX_FEWSHOTS = os.getenv("ADS_MAX_FEWSHOTS", "10").strip()
ADS_DEFAULT_MIN_GAP_SEC = os.getenv("ADS_MIN_GAP_SEC", "2").strip()
ADS_DEFAULT_AUTOCLEAN_SEC = os.getenv("ADS_AUTOCLEAN_SEC", "0").strip()  # 0=off

def chat_ai_is_enabled(chat_id: int) -> bool:
    """Resolve group enable flag; per-chat override first, then global default from DB, finally ENV."""
    v = chat_cfg_get(chat_id, "chat_ai_enabled")
    if v is None:
        # DB-first: read global default persisted by seeding; fallback to ENV constant
        v = get_config("chat_ai_default_enabled") or CHAT_AI_DEFAULT_ENABLED
    return str(v).strip().lower() in ("on", "1", "true", "yes")


def chat_ai_autoclean_sec(chat_id: int) -> int:
    """
    مقدار تاخیر حذف خودکار پیام‌های راهنمای Chat AI برای این گروه.
    اولویت: chat_config['chat_ai_autoclean_sec'] → bot_config.chat_ai_default_autoclean_sec → ENV CHAT_AI_AUTOCLEAN_SEC → 0 (خاموش)
    """
    v = chat_cfg_get(chat_id, "chat_ai_autoclean_sec")
    if v is None:
        v = get_config("chat_ai_default_autoclean_sec") or CHAT_AI_DEFAULT_AUTOCLEAN_SEC
    try:
        return int(v)
    except Exception:
        return int(CHAT_AI_DEFAULT_AUTOCLEAN_SEC)


# --- replace whole function: call_flowise(...) in shared_utils.py ---
def call_flowise(question: str, session_id: str, chat_id: Optional[int] = None) -> tuple[str, int | None]:
    """
    اگر chat_id داده شود:
      - PV: از bot_config.pv_chatflow_id → ENV PV_CHATFLOW_ID
      - Group: از chat_config.chat_ai_chatflow_id → chat_config.chatflow_id (قدیمی)
    در نهایت فالبک: bot_config.chat_ai_default_chatflow_id → ENV CHATFLOW_ID
    + بهبود: در Group نام‌فضای RAG را به فرم grp:<chat_id> پاس می‌دهیم.
    """
    def _chat_feature_on() -> bool:
        try:
            v = get_config("chat_feature")
        except Exception:
            v = None
        return str(v or "on").strip().lower() in ("on", "1", "true", "yes")

    if not _chat_feature_on():
        def _t(key: str) -> str:
            try:
                from messages_service import t as _tx
                return _tx(key, chat_id=chat_id)
            except Exception:
                return "🔕 این قسمت فعلاً خاموشه. ادمین می‌تونه با دستور /chat on روشنش کنه."
        return (_t("chat.off.notice_admin_hint"), None)

    cfid = None
    try:
        if chat_id:
            if chat_id > 0:
                # PV (چت خصوصی): DB-first → ENV
                cfid = (get_config("pv_chatflow_id") or PV_CHATFLOW_ID)
            else:
                # Group/Supergroup: per-chat override
                cfid = (
                    chat_cfg_get(chat_id, "chat_ai_chatflow_id")
                    or chat_cfg_get(chat_id, "chatflow_id")
                )
    except Exception:
        cfid = None

    # Fallback نهایی
    if not cfid:
        cfid = (get_config("chat_ai_default_chatflow_id") or CHATFLOW_ID)

    # تعیین namespace فقط برای Group
    ns = f"grp:{chat_id}" if (chat_id is not None and chat_id < 0) else None

    # فراخوانی Flowise با namespace اختیاری
    return _flowise_call(
        question=question,
        session_id=session_id,
        chatflow_id=cfid,
        namespace=ns,           # NEW: فقط در Group پر می‌شود
        timeout_sec=FLOWISE_TIMEOUT,
        retries=FLOWISE_RETRIES,
        backoff_base_ms=FLOWISE_BACKOFF_BASE_MS,
    )



# تنظیمات پایگاه‌داده PostgreSQL
DB_HOST = os.getenv("POSTGRES_BOT_HOST", "bot_db")
DB_PORT = _int_env("POSTGRES_BOT_PORT", 5432)
DB_NAME = os.getenv("POSTGRES_BOT_DB", "bot_db")
DB_USER = os.getenv("POSTGRES_BOT_USER", "bot_user")
DB_PASS = os.getenv("POSTGRES_BOT_PASSWORD", "password")

# سیاست دریافت پیام خصوصی (DM Policy)
DM_POLICY = os.getenv("DM_POLICY", "db_or_env").strip().lower()  # حالت‌های ممکن: env_only | db_only | db_or_env
ALLOWED_DM_ENV: Set[int] = _parse_ids(os.getenv("ALLOWED_DM_USER_IDS", ""))
PRIVATE_DENY_MESSAGE = os.getenv(
    "PRIVATE_DENY_MESSAGE",
    "سلام! 👋\nبرای اینکه پاسخ‌ها مرتب و قابل جست‌وجو بمونه، من فقط داخل گروه‌ها پاسخ می‌دم.\nاگر سوالی داری، لطفاً اون رو در یکی از گروه‌هایی که عضو هستم بپرس. 🙏\nیا حتی اگه گروهی داری، من رو اونجا ادد کن و طبق تخصص گروهت من رو آموزش بده که فقط توی همون حوزه ی فعالیت و توی همون گروه پاسخ بدم😍"
)
# لیست ادمین‌های بات (از طریق ENV)
ADMIN_USER_IDS: Set[int] = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))

# ثابت‌ها
TG_MAX_MESSAGE = 4096
# نسخه‌ی UI (در صورت تغییر کیبوردهای ربات، این را افزایش دهید)
UI_SCHEMA_VERSION = 2
# عبارات قابل تشخیص برای پاسخ‌های نامعلوم (Fallback)
FALLBACK_HINTS = (
    "این پرسش در حوزه این ربات نیست",
    "این ربات در حال آموزش است",
    "پاسخی پیدا نشد",
    "متوجه منظور نشدم",
)

# تابع تشخیص پاسخ نامعلوم (بر اساس الگوهای ثابت)
def is_unknown_reply(txt: str) -> bool:
    if not txt or not str(txt).strip():
        # پاسخ خالی یا None به منزله پاسخ نامعلوم است
        return True
    t = str(txt).replace("\u200c", "")  # حذف نیم‌فاصله (ZWNJ)
    t = re.sub(r"\s+", " ", t).strip("«»\"'").strip()
    # اگر پاسخ با هر یک از عبارات پیش‌فرض fallback شروع شود
    if any(t.startswith(h) for h in FALLBACK_HINTS):
        return True
    # اگر پاسخ خیلی کوتاه باشد و یکی از عبارات را شامل شود (احتمالاً fallback)
    if len(t) <= 80 and any(h in t for h in FALLBACK_HINTS):
        return True
    return False

# شیء logger اصلی (مشترک بین ماژول‌ها)
log = logging.getLogger(__name__)

# تنظیمات پایگاه‌داده: اتصال و آماده‌سازی
# --- Connection Pool (Threaded) ---

_DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASS}"
_POOL_MIN, _POOL_MAX = 1, 15  # متناسب با لود بات تنظیم کن
_pg_pool = ThreadedConnectionPool(_POOL_MIN, _POOL_MAX, dsn=_DSN)

@contextmanager
def db_conn():
    """
    کانکشن را از استخر بگیر، در پایان به استخر برگردان.
    اگر جایی commit نکرده باشیم، برای اطمینان rollback می‌کنیم.
    """
    conn = _pg_pool.getconn()
    try:
        yield conn
    finally:
        try:
            if not conn.closed:
                conn.rollback()
        except Exception:
            pass
        _pg_pool.putconn(conn)


async def wait_for_db_ready(max_wait_sec: int = 60):
    deadline = time.time() + max_wait_sec
    attempt = 0
    while time.time() < deadline:
        try:
            # ✅ استفادهٔ درست از کانتکست‌منیجر اتصال
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
            log.info("Database is reachable.")
            return
        except Exception as e:
            attempt += 1
            sleep_sec = min(5, 0.5 * (2 ** attempt))
            log.warning(f"Waiting for DB... attempt={attempt}, err='{e}', sleep={sleep_sec}s")
            await asyncio.sleep(sleep_sec)
    log.error("Database not reachable within timeout.")
    raise Exception("Database connection failed")

def ensure_tables():
    """ایجاد جداول مورد نیاز در پایگاه‌داده (در صورت عدم وجود)"""
    with db_conn() as conn, conn.cursor() as cur:
        # جداول مربوط به جلسه، تاریخچه و بازخورد
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
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_feedback_chat_msg
          ON message_feedback (chat_id, bot_message_id);
        """)
        cur.execute("""
        ALTER TABLE chat_sessions
        ADD COLUMN IF NOT EXISTS ui_ver INT NOT NULL DEFAULT 0;
        """)
        # جدول کاربران
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
        # جدول allowlist پیام خصوصی
        cur.execute("""
        CREATE TABLE IF NOT EXISTS allowed_dm (
            user_id BIGINT PRIMARY KEY,
            added_by BIGINT,
            added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        # جدول تنظیمات بات (مثلاً برای روشن/خاموش کردن DM global بدون ریست)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        
        # --- NEW: Admin audit trail (security logging of privileged changes) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            by_user BIGINT,
            chat_id BIGINT,
            command TEXT NOT NULL,
            args JSONB,
            prev_value TEXT,
            new_value TEXT,
            ok BOOLEAN NOT NULL,
            reason TEXT,
            message_id BIGINT
        );
        """)
        # ایندکس‌های پایه برای گزارش‌گیری سریع
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit (ts DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_cmd ON admin_audit (command);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_byuser ON admin_audit (by_user);")

        # تنظیمات per-group (کلید-مقدار)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_config (
            chat_id BIGINT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (chat_id, key)
        );
        """)

        # نگاشت ادمین⇄گروه (برای مدیریت در پی‌وی)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_group_bind (
            user_id BIGINT NOT NULL,
            chat_id BIGINT NOT NULL,
            bound_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, chat_id)
        );
        """)

        # «گروه فعال» هر ادمین در پی‌وی
        cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_active_context (
            user_id BIGINT PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        # جدول ثبت سؤالات بی‌پاسخ (برای آموزش ربات)
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
    """ثبت یا به‌روزرسانی اطلاعات کاربر در جدول users با هر تعاملی."""
    try:
        u = update.effective_user
        if not u:
            return
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, is_bot, is_admin, last_seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    is_bot = EXCLUDED.is_bot,
                    is_admin = users.is_admin OR EXCLUDED.is_admin,
                    last_seen_at = NOW(),
                    updated_at = NOW()
            """, (
                u.id, u.username, u.first_name, u.last_name, u.is_bot,
                True if (ADMIN_USER_IDS and u.id in ADMIN_USER_IDS) else False
            ))
            conn.commit()
    except Exception as e:
        log.warning(f"upsert_user_from_update failed: {e}")

# توابع تنظیمات بات در DB
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


# --- NEW: Admin audit helper ---
def audit_admin_action(update: Update,
                       command: str,
                       args=None,
                       ok: bool = False,
                       prev_value: str | None = None,
                       new_value: str | None = None,
                       reason: str | None = None) -> None:
    """
    ثبت یک رخداد ممیزی برای دستورات سطح بالا (سوپر ادمین/تنظیمات سراسری).
    - update: برای گرفتن chat_id/user_id/message_id
    - command: نام دستور، مثل 'loglevel' یا 'chat on'
    - args: هر ساختاری (dict/list/str) → JSONB ذخیره می‌شود
    - ok: نتیجهٔ عمل
    - prev_value/new_value: مقدار قبلی/جدید (در صورت کاربرد)
    - reason: دلیل شکست/توضیح اضافی
    """
    try:
        u = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        msg = getattr(update, "effective_message", None)
        by_user = int(u.id) if u else None
        chat_id = int(chat.id) if chat else None
        message_id = int(msg.message_id) if msg else None

        from psycopg2.extras import Json as PGJson
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_audit (by_user, chat_id, command, args, prev_value, new_value, ok, reason, message_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                by_user, chat_id, str(command),
                PGJson(args) if args is not None else None,
                None if prev_value is None else str(prev_value),
                None if new_value is None else str(new_value),
                bool(ok),
                None if not reason else str(reason),
                message_id
            ))
            conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"audit_admin_action failed: {e}")


# --- DB-first config helpers (bot_config → ENV) ---
def cfg_get_str(key: str, env_key: Optional[str] = None, default: Optional[str] = None) -> Optional[str]:
    """Return a string config; prefer bot_config[key], fallback to os.getenv(env_key, default)."""
    v = get_config(key)
    if v is not None:
        return v
    if env_key is not None:
        return os.getenv(env_key, default)
    return default

def cfg_get_bool(key: str, env_key: Optional[str] = None, default: bool = False) -> bool:
    v = cfg_get_str(key, env_key, str(default))
    return str(v).strip().lower() in ("on","1","true","yes")

def cfg_get_int(key: str, env_key: Optional[str] = None, default: int = 0) -> int:
    v = cfg_get_str(key, env_key, str(default))
    try:
        return int(v)
    except Exception:
        return default

def cfg_get_float(key: str, env_key: Optional[str] = None, default: float = 0.0) -> float:
    v = cfg_get_str(key, env_key, str(default))
    try:
        return float(v)
    except Exception:
        return default


def chat_ai_mode(chat_id: int) -> str:
    """
    برمی‌گرداند یکی از: 'mention' یا 'all'
    هر مقدار نامعتبر یا قدیمی (reply/command) → 'mention'
    اولویت: chat_config['chat_ai_mode'] → bot_config.chat_ai_default_mode → ENV → 'mention'
    """
    v = chat_cfg_get(chat_id, "chat_ai_mode")
    if v is None:
        v = get_config("chat_ai_default_mode") or CHAT_AI_DEFAULT_MODE
    v = (str(v) or "mention").strip().lower()
    return v if v in ("mention", "all") else "mention"


def chat_ai_min_gap_sec(chat_id: int) -> int:
    """
    حداقل فاصله‌ی زمانی بین دو پاسخ Chat-AI در این گروه (ثانیه).
    اولویت: chat_config['chat_ai_min_gap_sec'] → bot_config.chat_ai_default_min_gap_sec → ENV → 2
    """
    v = chat_cfg_get(chat_id, "chat_ai_min_gap_sec")
    if v is None:
        v = get_config("chat_ai_default_min_gap_sec") or CHAT_AI_DEFAULT_MIN_GAP_SEC
    try:
        return max(0, int(v))
    except Exception:
        return 2



# ---- Ads defaults (DB-first) ----
def ads_is_enabled(chat_id: int) -> bool:
    v = chat_cfg_get(chat_id, "ads_feature")
    if v is None:
        v = get_config("ads_feature") or ADS_DEFAULT_FEATURE
    return str(v).strip().lower() in ("on","1","true","yes")

def ads_action(chat_id: int) -> str:
    v = chat_cfg_get(chat_id, "ads_action")
    if v is None:
        v = get_config("ads_action") or ADS_DEFAULT_ACTION
    v = (v or "none").strip().lower()
    return v if v in ("none","warn","delete") else "none"

def ads_threshold(chat_id: int) -> float:
    v = chat_cfg_get(chat_id, "ads_threshold")
    if v is None:
        v = get_config("ads_threshold") or ADS_DEFAULT_THRESHOLD
    try:
        return float(v)
    except Exception:
        return 0.78

def ads_max_fewshots(chat_id: int) -> int:
    v = chat_cfg_get(chat_id, "ads_max_fewshots")
    if v is None:
        v = get_config("ads_max_fewshots") or ADS_DEFAULT_MAX_FEWSHOTS
    try:
        return max(0, int(v))
    except Exception:
        return 10

def ads_min_gap_sec(chat_id: int) -> int:
    v = chat_cfg_get(chat_id, "ads_min_gap_sec")
    if v is None:
        v = get_config("ads_min_gap_sec") or ADS_DEFAULT_MIN_GAP_SEC
    try:
        return max(0, int(v))
    except Exception:
        return 2

def ads_autoclean_sec(chat_id: int) -> int:
    v = chat_cfg_get(chat_id, "ads_autoclean_sec")
    if v is None:
        v = get_config("ads_autoclean_sec") or ADS_DEFAULT_AUTOCLEAN_SEC
    try:
        return max(0, int(v))
    except Exception:
        return 0




def chat_cfg_get(chat_id: int, key: str) -> Optional[str]:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM chat_config WHERE chat_id=%s AND key=%s", (chat_id, key))
            r = cur.fetchone()
            return r[0] if r else None
    except Exception as e:
        log.warning(f"chat_cfg_get failed: {e}")
        return None

def chat_cfg_set(chat_id: int, key: str, value: str):
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO chat_config (chat_id, key, value)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id, key)
                DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """, (chat_id, key, value))
            conn.commit()
    except Exception as e:
        log.warning(f"chat_cfg_set failed: {e}")

def ensure_chat_defaults(chat_id: int) -> None:
    """
    ایجاد/تکمیل رکورد تنظیمات این گروه در chat_config به‌صورت DB-first.
    فقط کلیدهایی که در chat_config موجود نیستند را با مقادیر پیش‌فرض (از bot_config یا در نهایت ENV) ثبت می‌کند.
    این کار تغییرات قبلی ادمین‌ها را overwrite نمی‌کند (idempotent).
    """
    try:
        # کاندیدهای اصلی که دوست داریم در هر گروه «ثابت و قابل ردیابی» باشند
        candidates = {
            # زبان پیش‌فرض گروه
            "lang": (get_config("default_lang") or "fa"),
            # ChatAI
            "chat_ai_enabled": (get_config("chat_ai_default_enabled") or CHAT_AI_DEFAULT_ENABLED),
            "chat_ai_mode": (get_config("chat_ai_default_mode") or CHAT_AI_DEFAULT_MODE),
            "chat_ai_min_gap_sec": (get_config("chat_ai_default_min_gap_sec") or CHAT_AI_DEFAULT_MIN_GAP_SEC),
            "chat_ai_autoclean_sec": (get_config("chat_ai_default_autoclean_sec") or CHAT_AI_DEFAULT_AUTOCLEAN_SEC),
            # AdsGuard
            "ads_feature": (get_config("ads_feature") or ADS_DEFAULT_FEATURE),
            "ads_action": (get_config("ads_action") or ADS_DEFAULT_ACTION),
            "ads_threshold": (get_config("ads_threshold") or ADS_DEFAULT_THRESHOLD),
            "ads_max_fewshots": (get_config("ads_max_fewshots") or ADS_DEFAULT_MAX_FEWSHOTS),
            "ads_min_gap_sec": (get_config("ads_min_gap_sec") or ADS_DEFAULT_MIN_GAP_SEC),
            "ads_autoclean_sec": (get_config("ads_autoclean_sec") or ADS_DEFAULT_AUTOCLEAN_SEC),
            # ⭐️ حداقل طول کپشن (بر حسب «کلمه») برای گروه‌های جدید: DB → درغیراینصورت مقدار ثابت 5
            "ads_caption_min_len": (get_config("ads_caption_min_len") or "5"),
        }
        # فقط کلیدهای غایب را درج کن (UPSERT روی (chat_id,key))
        for k, v in candidates.items():
            if chat_cfg_get(chat_id, k) is None and v is not None:
                chat_cfg_set(chat_id, k, str(v))
    except Exception as e:
        # نباید منطق اصلی را مختل کند
        log.warning(f"ensure_chat_defaults failed for chat {chat_id}: {e}")


def pv_group_list_limit() -> int:
    """حداکثر تعداد گروه‌هایی که در پی‌وی نشان داده می‌شود (PV group picker)."""
    return cfg_get_int("pv_group_list_limit", "PV_GROUP_LIST_LIMIT", 12)

def pv_invite_links() -> str:
    """لیست لینک‌های دعوتی که در پی‌وی به کاربر نمایش/استفاده می‌شود (با فرمت فعلی پروژه‌ات)."""
    return (cfg_get_str("pv_invite_links", "PV_INVITE_LINKS", "") or "").strip()

def pv_invite_expire_hours() -> int:
    """مدت اعتبار لینک‌های دعوت به ساعت (۰ = بدون محدودیت)."""
    return cfg_get_int("pv_invite_expire_hours", "PV_INVITE_EXPIRE_HOURS", 12)

def pv_invite_member_limit() -> int:
    """حداکثر اعضای مجاز برای لینک دعوت (۰ = بدون محدودیت)."""
    return cfg_get_int("pv_invite_member_limit", "PV_INVITE_MEMBER_LIMIT", 0)



# --------- Admin ⇄ Group binding (for PV management) ---------
def bind_admin_to_group(user_id: int, chat_id: int):
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_group_bind (user_id, chat_id)
                VALUES (%s, %s)
                ON CONFLICT (user_id, chat_id) DO NOTHING
            """, (user_id, chat_id))
            conn.commit()
    except Exception as e:
        log.warning(f"bind_admin_to_group failed: {e}")

def list_admin_groups(user_id: int) -> list[int]:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT chat_id FROM admin_group_bind WHERE user_id=%s ORDER BY bound_at DESC", (user_id,))
            rows = cur.fetchall() or []
            return [int(r[0]) for r in rows]
    except Exception as e:
        log.warning(f"list_admin_groups failed: {e}")
        return []

def set_active_admin_group(user_id: int, chat_id: int):
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO admin_active_context (user_id, chat_id)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET chat_id=EXCLUDED.chat_id, updated_at=NOW()
            """, (user_id, chat_id))
            conn.commit()
    except Exception as e:
        log.warning(f"set_active_admin_group failed: {e}")

def get_active_admin_group(user_id: int) -> Optional[int]:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT chat_id FROM admin_active_context WHERE user_id=%s", (user_id,))
            r = cur.fetchone()
            return int(r[0]) if r else None
    except Exception as e:
        log.warning(f"get_active_admin_group failed: {e}")
        return None

# اعتبارسنجی ادمین بودن در یک گروه (هر بار)
async def is_user_admin_of_group(bot, user_id: int, chat_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        st = str(getattr(m, "status", "")).lower()
        return st in ("administrator", "creator")
    except Exception:
        return False
        
        
# چک کامل: هم حضور بات در گروه، هم ادمین‌بودن کاربر
# خروجی: (ok: bool, code: str, title: Optional[str])
# code ∈ {"OK", "BOT_NOT_IN_GROUP", "NOT_ADMIN", "CHECK_FAILED"}
async def check_admin_status(bot, user_id: int, chat_id: int):
    """
    خروجی: (ok, code, title)
    code یکی از این‌هاست:
      - "OK"                   ← کاربر ادمین/مالک است
      - "BOT_NOT_IN_GROUP"     ← خود ربات عضو گروه نیست
      - "BOT_NOT_ADMIN"        ← ربات عضو است ولی ادمین نیست؛ تأیید ادمین‌بودنِ کاربر «تضمین‌شده» نیست
      - "NOT_ADMIN"            ← ربات ادمین است و کاربر ادمین/مالک نیست
      - "CHECK_FAILED"         ← خطای دیگر
    """
    title = None
    # 1) اول چک کن اصلاً ربات به گروه دسترسی دارد یا نه (عضو بودن)
    try:
        chat = await bot.get_chat(chat_id)
        title = getattr(chat, "title", None) or (f"@{chat.username}" if getattr(chat, "username", None) else None)
    except Exception:
        return False, "BOT_NOT_IN_GROUP", None

    # 2) وضعیت خود ربات در گروه را بگیر (برای تشخیص محدودیت Bot API)
    try:
        me = await bot.get_me()
        bm = await bot.get_chat_member(chat_id, me.id)
        bot_status = str(getattr(bm, "status", "")).lower()
        bot_is_admin = bot_status in ("administrator", "creator")
    except Exception:
        # اگر این هم خطا داد، یعنی ربات عملاً دسترسی ندارد
        return False, "BOT_NOT_IN_GROUP", title

    # 3) حالا وضعیت کاربر را بگیر
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        st = str(getattr(m, "status", "")).lower()
        if st in ("administrator", "creator"):
            return True, "OK", title
        # نکتهٔ مهم: اگر خود ربات ادمین نیست، طبق Bot API نتیجهٔ این متد تضمین‌شده نیست
        if not bot_is_admin:
            return False, "BOT_NOT_ADMIN", title
        return False, "NOT_ADMIN", title
    except Exception:
        if not bot_is_admin:
            return False, "BOT_NOT_ADMIN", title
        return False, "CHECK_FAILED", title




# در گروه: خودِ update.effective_chat.id هدف است
# در پی‌وی: «گروه فعال» ادمین را از DB می‌خواند
async def resolve_target_chat_id(update, context) -> Optional[int]:
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        return chat.id
    u = update.effective_user
    if not u: 
        return None
    from telegram.constants import ChatType
    if chat and chat.type == "private":
        gid = get_active_admin_group(u.id)
        return gid
    return None



# توابع مربوط به سیاست DM و ادمین‌ها
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


# ---------- Super Admin (DB-first with ENV seeding) ----------
def _parse_super_ids(raw: str) -> set[int]:
    """پارس CSV یا JSON-List از آی‌دی‌ها به مجموعهٔ اعداد مثبت."""
    import json
    raw = (raw or "").strip()
    if not raw:
        return set()
    # تلاش برای JSON
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {int(x) for x in data if str(x).isdigit() and int(x) > 0}
    except Exception:
        pass
    # fallback: CSV
    parts = [p.strip() for p in raw.split(",")]
    return {int(p) for p in parts if p.isdigit() and int(p) > 0}

def get_super_admin_ids() -> set[int]:
    """
    لیست سوپرادمین‌ها را از DB می‌خواند؛ اگر نبود، از ENV (برای seed اولیه).
    منبع حقیقت = DB. مقدار می‌تواند CSV یا JSON-List باشد.
    """
    try:
        v = get_config("super_admin_ids")
        if v is None or str(v).strip() == "":
            # fallback به ENV فقط وقتی DB خالی است
            v = os.getenv("SUPER_ADMIN_IDS", "")
        return _parse_super_ids(str(v or ""))
    except Exception:
        # روی هر خطا، فقط یک مجموعهٔ خالی برگردان
        return set()

def is_super_admin(user_id: int) -> bool:
    """عضویت کاربر در مجموعهٔ سوپرادمین‌ها."""
    try:
        return int(user_id) in get_super_admin_ids()
    except Exception:
        return False

# --- New explicit helpers (readable naming) ---
def is_superadmin(user_id: int) -> bool:
    """
    Alias خوانا برای سوپرادمین.
    دلیل: کدهای مصرف‌کننده به جای is_admin از نام واضح استفاده کنند.
    """
    return is_super_admin(user_id)

async def is_group_admin(bot, user_id: int, chat_id: int) -> bool:
    """
    ادمین‌بودن «کاربر» در همان «گروه» را با Bot API بررسی می‌کند.
    این یک لفاف خوانا روی is_user_admin_of_group است.
    """
    return await is_user_admin_of_group(bot, user_id, chat_id)


def is_user_in_db_allowlist(user_id: int) -> bool:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM allowed_dm WHERE user_id=%s", (user_id,))
            return cur.fetchone() is not None
    except Exception:
        return False

def is_dm_globally_on() -> bool:
    v = get_config("dm_global")  # مقدار 'on' یا 'off' در تنظیمات (یا None اگر تنظیم نشده)
    if v is None:
        # اگر تنظیمی در DB نبود، پیش‌فرض: بسته به ALLOWED_DM_ENV
        # اگر 'all' در ENV تعریف شده باشد → معادل on
        return (-1 in ALLOWED_DM_ENV)
    return v.lower() == "on"

def is_dm_allowed(user_id: int) -> bool:
    # اگر حالت global روشن باشد → همه مجازند
    if is_dm_globally_on():
        return True
    # اگر حالت global خاموش باشد → فقط کسانی که در allowlist هستند
    env_allows = (-1 in ALLOWED_DM_ENV) or (user_id in ALLOWED_DM_ENV)
    db_allows = is_user_in_db_allowlist(user_id)
    if DM_POLICY == "env_only":
        return env_allows
    elif DM_POLICY == "db_only":
        return db_allows
    else:  # حالت پیش‌فرض: db_or_env
        return env_allows or db_allows

# === NEW: پیام راهنمای PV با فهرست گروه‌ها (لینک‌دار) ===
from typing import List

def _list_recent_group_ids(limit: int = 10) -> List[int]:
    """آخرین گروه‌هایی که ربات در آن‌ها فعالیت داشته (chat_id منفی) را از DB می‌خواند."""
    ids: List[int] = []
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT chat_id
                FROM chat_sessions
                WHERE chat_id < 0
                ORDER BY last_activity DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall() or []
            ids = [int(r[0]) for r in rows]
    except Exception as e:
        log.warning(f"_list_recent_group_ids failed: {e}")
    return ids

async def build_pv_deny_text_links(bot, limit: int | None = None) -> str:
    """
    متن «PV خاموش است» را با فهرست گروه‌ها می‌سازد.
    • اگر گروه public باشد → t.me/<username> به‌صورت لینک
    • اگر private باشد و اجازه ساخت لینک داشته باشیم → createChatInviteLink (لینک موقت/محدود)
    • اگر نتوانستیم لینک بسازیم → فقط نام گروه را نشان می‌دهیم
    """
    base = (PRIVATE_DENY_MESSAGE or "").strip()
    # تنظیمات از ENV (اختیاری)
    from html import escape as _esc
    PV_LIST_LIMIT = pv_group_list_limit()
    if limit is None:
        limit = PV_LIST_LIMIT
    INVITE_ON = (pv_invite_links().strip().lower() == "on")
    INVITE_HOURS = pv_invite_expire_hours()       # اعتبار لینک موقت
    MEMBER_LIMIT = pv_invite_member_limit()       # چند عضو بتوانند با این لینک وارد شوند


    gids = _list_recent_group_ids(limit=limit)
    if not gids:
        return base

    lines = []
    now = int(time.time())
    expire_ts = now + max(0, INVITE_HOURS) * 3600 if INVITE_ON and INVITE_HOURS > 0 else None

    for gid in gids:
        title = str(gid)
        url = None
        try:
            chat = await bot.get_chat(gid)  # عنوان/یوزرنیم گروه
            title = getattr(chat, "title", None) or (f"@{chat.username}" if getattr(chat, "username", None) else title)

            # اگر public باشد (username دارد) → t.me/<username>
            uname = getattr(chat, "username", None)
            if uname:
                url = f"https://t.me/{uname}"
            # اگر private است و ساخت لینک فعال باشد، تلاش کن لینک دعوت موقت بسازی
            elif INVITE_ON:
                kwargs = {}
                if expire_ts:
                    kwargs["expire_date"] = expire_ts
                if MEMBER_LIMIT and MEMBER_LIMIT > 0:
                    kwargs["member_limit"] = MEMBER_LIMIT
                # creates_join_request=False یعنی بدون تایید دستی
                link_obj = await bot.create_chat_invite_link(gid, creates_join_request=False, **kwargs)
                url = getattr(link_obj, "invite_link", None)
        except Exception as e:
            log.debug(f"build link for {gid} failed: {e}")

        if url:
            lines.append(f'• <a href="{_esc(url)}">{_esc(title)}</a>')
        else:
            # fallback: فقط اسم گروه (ممکن است private باشد یا ربات ادمین نباشد)
            lines.append(f"• {_esc(title)}")

    extra = "\n\n<b>گروه‌هایی که من آن‌جا پاسخ‌گو هستم:</b>\n" + "\n".join(lines) + \
            "\n\nاگر عضو هیچ‌کدام نیستی، از ادمین لینک دعوت بگیر یا داخل همان گروه من را منشن کن."
    return base + extra



# مدیریت جلسات گفتگو
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

def get_chat_ui_ver(chat_id: int) -> int:
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT ui_ver FROM chat_sessions WHERE chat_id=%s", (chat_id,))
            row = cur.fetchone()
            return int(row[0]) if row and row[0] is not None else 0
    except Exception:
        return 0

def set_chat_ui_ver(chat_id: int, ver: int):
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE chat_sessions SET ui_ver=%s WHERE chat_id=%s", (ver, chat_id))
            conn.commit()
    except Exception:
        pass

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

# تاریخچه مکالمه و فیدبک
def get_local_history(session_id: str) -> list:
    history = []
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT history FROM chat_history_log WHERE session_id=%s", (session_id,))
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
    """بررسی اینکه آیا برای یک پیام (در چت خصوصی) قبلاً هر نوع بازخوردی ثبت شده یا نه."""
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM message_feedback WHERE chat_id=%s AND bot_message_id=%s LIMIT 1",
                (chat_id, bot_message_id)
            )
            return cur.fetchone() is not None
    except Exception:
        return False

def count_feedback(chat_id: int, bot_message_id: int) -> tuple[int, int]:
    """شمارش تعداد 👍 و 👎 ثبت‌شده برای یک پیام (در گروه‌ها)"""
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
            try:
                MET_UNKNOWN_QUESTIONS.inc()
            except Exception:
                pass
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

# به‌روزرسانی UI کاربر در صورت تغییر (حذف کیبوردهای قدیمی)
async def maybe_refresh_ui(update: Update, chat_id: int):
    try:
        current = get_chat_ui_ver(chat_id)
        if current < UI_SCHEMA_VERSION:
            await safe_reply_text(update, "رابط کاربری ربات به‌روزرسانی شد. ✅", reply_markup=ReplyKeyboardRemove())
            set_chat_ui_ver(chat_id, UI_SCHEMA_VERSION)
    except Exception:
        # در صورت رخ دادن خطا، ادامه اجرای ربات را مختل نکن
        pass

# تشخیص اینکه پیام در گروه به بات خطاب شده یا خیر (با ریپلای یا منشن)
def is_addressed_to_bot(update: Update, bot_username: str, bot_id: int) -> bool:
    msg = update.message
    if not msg:
        return False
    # 1) اگر پیام ریپلای به پیام خود بات باشد
    if msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == bot_id:
        return True
    # 2) اگر نام کاربری بات در متن پیام منشن شده باشد
    text = (msg.text or "")
    if bot_username and ("@" + bot_username.lower()) in text.lower():
        return True
    # 3) اگر entity از نوع mention یا text_mention وجود داشته باشد
    for ent in (msg.entities or []):
        if ent.type == "mention":
            ent_text = text[ent.offset: ent.offset + ent.length]
            if ent_text.lower() == ("@" + bot_username.lower()):
                return True
        elif ent.type == "text_mention" and ent.user and ent.user.id == bot_id:
            return True
    return False


def build_sender_html_from_msg(msg) -> Tuple[str, str]:
    """
    خروجی: (mention_html, id_html)
    - اگر پیام توسط کاربر باشد: منشن HTML با لینک tg://user?id
    - اگر پیام از طرف chat/channel باشد: عنوان چت به‌صورت Bold
    """
    from html import escape as _esc  # import محلی برای خوانایی
    u = getattr(msg, "from_user", None)
    mention = "کاربر"
    id_html = "—"
    if u:
        disp = f"{(u.first_name or '')} {(u.last_name or '')}".strip() \
               or (f"@{u.username}" if getattr(u, "username", None) else "کاربر")
        mention = f'<a href="tg://user?id={u.id}">{_esc(disp)}</a>'
        id_html = f"<code>{u.id}</code>"
    elif getattr(msg, "sender_chat", None):
        title = getattr(msg.sender_chat, "title", None) or "کانال/گروه"
        mention = f"<b>{_esc(title)}</b>"
        id_html = f"<code>{msg.sender_chat.id}</code>"
    return mention, id_html


def build_sender_html_from_update(update: Update) -> Tuple[str, str]:
    """
    خروجی: (mention_html, id_html) بر اساس Update
    """
    from html import escape as _esc
    u = update.effective_user
    mention = "کاربر"
    id_html = "—"
    if u:
        disp = f"{(u.first_name or '')} {(u.last_name or '')}".strip() \
               or (f"@{u.username}" if getattr(u, "username", None) else "کاربر")
        mention = f'<a href="tg://user?id={u.id}">{_esc(disp)}</a>'
        id_html = f"<code>{u.id}</code>"
    return mention, id_html



# شکستن متن‌های بلند به بخش‌های کوچکتر برای ارسال امن در تلگرام
def chunk_text(text: str, limit: int = TG_MAX_MESSAGE) -> List[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        # یافتن بهترین محل شکست متن (بین \n یا جملات یا کلمات)
        break_chars = ["\n", ". ", "! ", "? ", " ", ""]
        for break_char in break_chars:
            pos = text.rfind(break_char, 0, limit)
            if pos > 0 and pos > limit * 0.6:
                parts.append(text[:pos + len(break_char)])
                text = text[pos + len(break_char):].lstrip()
                break
        else:
            # اگر کاراکتر مناسبی برای شکست پیدا نشد
            parts.append(text[:limit])
            text = text[limit:]
    return parts

# ارسال امن متن (تکه‌تکه و با مدیریت خطاها)
# --- REPLACE safe_reply_text WITH THIS VERSION ---
async def safe_reply_text(update: Update, text: str, **kwargs):
    """
    هر متنی را امن (با محدودیت 4096 کاراکتر) ارسال می‌کند.
    اگر متن طولانی باشد، به چند پیام تقسیم می‌شود.
    در نهایت 'آخرین پیام ارسالی ربات' را برمی‌گرداند تا بتوانیم آن را پاک کنیم.
    + Metrics: بعد از ارسال، شمارندهٔ پاسخ‌های بات را با لیبل کم‌تنوع افزایش می‌دهیم.
    """
    chunks = chunk_text(text or "", TG_MAX_MESSAGE)
    message = update.effective_message or update.message
    if not message:
        return None

    last_msg = None
    for i, ch in enumerate(chunks):
        send_kwargs = kwargs
        if i < len(chunks) - 1 and "reply_markup" in send_kwargs:
            send_kwargs = dict(send_kwargs)
            send_kwargs.pop("reply_markup", None)
        try:
            last_msg = await message.reply_text(ch, **send_kwargs)
        except BadRequest as e:
            # نمونهٔ رایج: "Message to be replied not found" وقتی پیام حذف شده یا دیگر قابل ریپلای نیست
            # fallback: به‌صورت مستقل پیام را بفرست
            if "message to be replied not found" in str(e).lower():
                send_kwargs.pop("reply_to_message_id", None)
                last_msg = await message.chat.send_message(ch, **send_kwargs)
            else:
                raise

    # --- Metrics: شمارش پاسخ‌های بات (dst ∈ {private, group, unknown})
    try:
        chat = getattr(update, "effective_chat", None)
        ctype = getattr(chat, "type", None) if chat else None
        dst = "private" if ctype == "private" else ("group" if ctype in ("group", "supergroup") else "unknown")
        MET_BOT_REPLIES.labels(dst=dst).inc()
    except Exception:
        pass

    return last_msg


# --- REPLACE safe_message_reply_text WITH THIS VERSION ---
async def safe_message_reply_text(message, text: str, **kwargs):
    """
    مشابه safe_reply_text اما وقتی فقط خود message را داریم (مثلاً در CallbackQuery).
    'آخرین پیام ارسالی ربات' را برمی‌گرداند.
    """
    chunks = chunk_text(text or "", TG_MAX_MESSAGE)

    last_msg = None
    for i, ch in enumerate(chunks):
        send_kwargs = kwargs
        if i < len(chunks) - 1 and "reply_markup" in send_kwargs:
            send_kwargs = dict(send_kwargs)
            send_kwargs.pop("reply_markup", None)
        try:
            last_msg = await message.reply_text(ch, **send_kwargs)
        except BadRequest as e:
            # نمونهٔ رایج: "Message to be replied not found" وقتی پیام حذف شده یا دیگر قابل ریپلای نیست
            # fallback: به‌صورت مستقل پیام را بفرست
            if "message to be replied not found" in str(e).lower():
                send_kwargs.pop("reply_to_message_id", None)
                last_msg = await message.chat.send_message(ch, **send_kwargs)
            else:
                raise

    return last_msg

async def delete_after(bot, chat_id: int, message_id: int, delay: int):
    """
    حذف امن پیام بعد از X ثانیه.
    بات باید مجوز حذف پیام داشته باشد؛ اگر خطایی بود (مجوز/زمان/قدمت پیام)، نادیده می‌گیریم.
    """
    import asyncio
    if not delay or delay <= 0:
        return
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass
    
    