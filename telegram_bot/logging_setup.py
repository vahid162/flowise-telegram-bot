# logging_setup.py
# -----------------------------------------------------------------------------
# پیکربندی متمرکز لاگ با dictConfig (DB-first) + قالب JSON روی stdout
# - فرمت‌ها: console | json (پیش‌فرض: json در کانتینر)
# - تزریق کانتکست تلگرام با contextvars (chat_id, user_id, update_id, message_id, op, session_id)
# - سطح‌ها از DB (bot_config) و درصورت نبود، از ENV (LOG_LEVEL/LOG_FORMAT)
# - قابلیت تغییر سطح در زمان اجرا: apply_level()
# -----------------------------------------------------------------------------
from __future__ import annotations
import os
import logging
from logging.config import dictConfig
from logging import Filter, LogRecord
from typing import Optional, Dict, Any
import contextvars
import re


def _cfg(key: str, env: Optional[str], default: Optional[str]) -> Optional[str]:
    # DB-first: اگر در bot_config باشد همان را استفاده می‌کنیم، وگرنه ENV و در نهایت default
    try:
        from shared_utils import get_config
        v = get_config(key)
    except Exception:
        v = None
    if v is not None and str(v).strip() != "":
        return str(v)
    if env:
        return os.getenv(env, default)
    return default

# --------- contextvars برای تزریق کانتکست تلگرام به رکوردهای لاگ ----------
_ctx_chat_id   = contextvars.ContextVar("chat_id",   default=None)
_ctx_user_id   = contextvars.ContextVar("user_id",   default=None)
_ctx_update_id = contextvars.ContextVar("update_id", default=None)
_ctx_message_id= contextvars.ContextVar("message_id",default=None)
_ctx_op        = contextvars.ContextVar("op",        default=None)
_ctx_session   = contextvars.ContextVar("session_id",default=None)

def update_log_context(update=None, **kw):
    try:
        if update is not None:
            chat = getattr(update, "effective_chat", None)
            user = getattr(update, "effective_user", None)
            msg  = getattr(update, "effective_message", None)
            _ctx_chat_id.set(getattr(chat, "id", None))
            _ctx_user_id.set(getattr(user, "id", None))
            _ctx_update_id.set(getattr(update, "update_id", None))
            _ctx_message_id.set(getattr(msg, "message_id", None))
        for k, v in kw.items():
            if k == "op": _ctx_op.set(v)
            if k == "session_id": _ctx_session.set(v)
    except Exception:
        pass

def clear_log_context():
    for var in (_ctx_chat_id, _ctx_user_id, _ctx_update_id, _ctx_message_id, _ctx_op, _ctx_session):
        try:
            var.set(None)
        except Exception:
            pass

class ContextFilter(Filter):
    def filter(self, record: LogRecord) -> bool:
        record.chat_id    = _ctx_chat_id.get()
        record.user_id    = _ctx_user_id.get()
        record.update_id  = _ctx_update_id.get()
        record.message_id = _ctx_message_id.get()
        record.op         = _ctx_op.get()
        record.session_id = _ctx_session.get()
        return True

class RedactFilter(Filter):
    _bot_token = re.compile(r"/bot(\d+):[A-Za-z0-9_-]+")
    _bearer    = re.compile(r"(Authorization:\s*Bearer\s+)([A-Za-z0-9._-]+)", re.IGNORECASE)

    def filter(self, record: LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = self._bot_token.sub(r"/bot\\1:***", msg)
        red = self._bearer.sub(r"\\1***", red)
        if red != msg:
            record.msg = red
            record.args = ()
        return True

def setup_logging() -> None:
    update_log_context(op="startup")
    # سطح و فرمت از DB-first → ENV → پیش‌فرض
    level_str = (_cfg("log_level", "LOG_LEVEL", "INFO") or "INFO").upper()
    fmt_str   = (_cfg("log_format", "LOG_FORMAT", "json") or "json").lower()
    enable_file = (_cfg("enable_file_logs", "ENABLE_FILE_LOGS", "0") or "0").lower() in ("1","true","on","yes")
    level = getattr(logging, level_str, logging.INFO)

    json_fmt = {
        "format": "%(asctime)s %(levelname)s %(name)s %(message)s %(chat_id)s %(user_id)s %(update_id)s %(message_id)s %(op)s %(session_id)s",
        "datefmt": "%Y-%m-%dT%H:%M:%S%z",
    }
    console_fmt = {
        "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s [chat=%(chat_id)s user=%(user_id)s msg=%(message_id)s op=%(op)s]",
        "datefmt": "%H:%M:%S",
    }
    formatter_name = "console" if fmt_str == "console" else "json"

    handlers: Dict[str, Dict[str, Any]] = {
        "stdout": {
            "class": "logging.StreamHandler",
            "level": level_str,
            "stream": "ext://sys.stdout",
            "formatter": formatter_name,
            "filters": ["ctx", "redact"],
        }
    }
    if enable_file:
        os.makedirs("/app/logs", exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "level": level_str,
            "filename": "/app/logs/bot.log",
            "when": "midnight",
            "backupCount": 7,
            "encoding": "utf-8",
            "formatter": formatter_name,
            "filters": ["ctx", "redact"],
        }

    noisy_level = _cfg("log_noisy_level", "LOG_NOISY_LEVEL", "WARNING") or "WARNING"

    cfg = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "ctx": {"()": ContextFilter},
            "redact": {"()": RedactFilter}
        },
        "formatters": {
            "json": { "()": "pythonjsonlogger.jsonlogger.JsonFormatter", **json_fmt },
            "console": console_fmt,
        },
        "handlers": handlers,
        "root": { "level": level_str, "handlers": list(handlers.keys()) },
        "loggers": {
            "httpx":      {"level": noisy_level, "handlers": ["stdout"], "propagate": False},
            "httpcore":   {"level": noisy_level, "handlers": ["stdout"], "propagate": False},
            "urllib3":    {"level": noisy_level, "handlers": ["stdout"], "propagate": False},
            "apscheduler":{"level": noisy_level, "handlers": ["stdout"], "propagate": False},
            "telegram":   {"level": noisy_level, "handlers": ["stdout"], "propagate": False},
        }
    }
    if enable_file:
        for name in list(cfg["loggers"].keys()) + ["root"]:
            cfg["loggers"].setdefault(name, {})
            hlist = cfg["loggers"][name].get("handlers")
            if hlist is None:
                cfg["loggers"][name]["handlers"] = list(handlers.keys())
            else:
                if "file" not in hlist:
                    cfg["loggers"][name]["handlers"] = list(set(hlist + ["file"]))
    dictConfig(cfg)

def apply_level(new_level: str) -> str:
    """
    تغییر «سطح لاگ» به‌صورت runtime:
      - هم سطح Loggerها را عوض می‌کند
      - هم سطح Handlerهای متصل (stdout/file) را بالا/پایین می‌برد
    چرا لازم است؟ چون در setup_logging، سطحِ هندلرها هنگام بوت ثابت‌گذاری می‌شود
    و اگر فقط Logger را DEBUG کنیم ولی Handler روی INFO بماند، لاگ‌های DEBUG فیلتر می‌شوند.
    """
    new_level = (new_level or "").upper()
    if new_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        # اگر مقدار نامعتبر بود، همان سطحِ فعلی را برگردان
        return logging.getLevelName(logging.getLogger().getEffectiveLevel())

    # 1) روت‌لاگر
    root = logging.getLogger()
    root.setLevel(new_level)

    # 2) همهٔ هندلرهای متصل به روت (stdout / file) نیز باید به همین سطح بروند
    for h in list(getattr(root, "handlers", [])):
        try:
            h.setLevel(new_level)
        except Exception:
            pass  # اگر هندلری سطح‌پذیر نبود، نادیده بگیر

    # 3) سطح لاگرهای ماژول‌های اصلی پروژه (برای اطمینان)
    for name in ("__main__", "bot", "admin_commands", "user_commands", "ads_guard"):
        logging.getLogger(name).setLevel(new_level)

    # توجه: لاگرهای noisy مثل httpx/telegram سطح مستقل دارند
    # و با دستور /lognoise تنظیم می‌شوند، نه این تابع.
    return new_level


def apply_libs_level(new_level: str) -> str:
    """تنظیم سطح برای کتابخانه‌های پرحرف؛ برای دیباگ مقطعی می‌توانید DEBUG کنید."""
    new_level = (new_level or "").upper()
    if new_level not in ("DEBUG","INFO","WARNING","ERROR","CRITICAL"):
        return logging.getLevelName(logging.getLogger().getEffectiveLevel())
    for name in ("telegram", "httpx", "httpcore", "urllib3", "apscheduler"):
        logging.getLogger(name).setLevel(new_level)
    return new_level
