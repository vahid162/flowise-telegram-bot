

import os, json
import psycopg2.extras
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from logging_setup import update_log_context
from telegram import BotCommandScopeChat, BotCommandScopeChatAdministrators
from shared_utils import safe_reply_text, get_config, set_config, db_conn
from typing import Optional
from datetime import timezone
# Anonymous admin id (PTB v22+ / v13 fallback)
from shared_utils import TG_ANON
from typing import List
from functools import wraps

from shared_utils import safe_reply_text, upsert_user_from_update, is_admin, is_dm_globally_on, set_config, db_conn, DM_POLICY, ALLOWED_DM_ENV
from shared_utils import is_superadmin
from shared_utils import get_config  # برای خواندن وضعیت فعلی chat_feature از bot_config
from shared_utils import audit_admin_action
from messages_service import t
from shared_utils import admin_throttle, is_forwarded_message
from logging_setup import apply_level
import logging
from shared_utils import set_config
from shared_utils import resolve_target_chat_id, chat_cfg_set, chat_ai_autoclean_sec
from shared_utils import log_exceptions


# دستورات ادمین: /dm, /allow, /block, /users, /unknowns
def _is_anonymous_group_admin(update: Update) -> bool:
    """تشخیص ادمین ناشناس یا پیام «از طرف خود گروه» (بدون API call)."""
    chat = update.effective_chat
    msg = update.effective_message
    u = update.effective_user
    try:
        return (
            chat
            and chat.type in ['group', 'supergroup']
            and msg
            and (
                (getattr(msg, "sender_chat", None) is not None and msg.sender_chat.id == chat.id)
                or (u and int(u.id) == int(TG_ANON))
            )
        )
    except Exception:
        return False

async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    سیاست جدید و شفاف:
      - در گروه: یا ادمین ناشناس/از طرف گروه، یا (چک قطعی) ادمین همان گروه با Bot API
      - در پی‌وی: فقط سوپرادمین
    """
    u = update.effective_user
    chat = update.effective_chat

    if not u:
        return False

    # گروه: anonymous admin کافی است؛ وگرنه چک API
    if chat and chat.type in ['group', 'supergroup']:
        if _is_anonymous_group_admin(update):
            return True
        try:
            from shared_utils import check_admin_status
            ok, _, _ = await check_admin_status(context.bot, u.id, chat.id)
            return ok
        except Exception:
            return False

    # PV: فقط سوپرادمین
    return is_superadmin(u.id)

def _require_super_admin(update: Update, pv_only: bool = True) -> bool:
    """
    گارد سوپرادمین (نسخهٔ بولی، بدون پیام/ممیزی):
      - PV-only (اختیاری)
      - رد sender_chat (ادمین ناشناس/کانالی)
      - رد پیام‌های فوروارد/اتوفوروارد
      - رد پیام‌های ارسالی توسط botها
      - سپس بررسی عضویت در فهرست سوپرادمین‌ها
    """
    u = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message

    # PV-only
    if pv_only and (not chat or str(chat.type).lower() != "private"):
        return False

    # رد درخواست‌هایی که از طرف sender_chat/کانال/ادمین ناشناس می‌آید
    try:
        if msg and getattr(msg, "sender_chat", None) is not None:
            return False
    except Exception:
        pass

    # رد فوروارد/اتوفوروارد
    try:
        if msg and is_forwarded_message(msg):
            return False
    except Exception:
        pass

    # رد درخواست‌هایی که از طرف botها می‌آید
    if u and getattr(u, "is_bot", False):
        return False

    # در نهایت: فقط سوپرادمین
    return bool(u and is_superadmin(int(u.id)))

@admin_throttle(window_sec=3)
async def dm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not await _require_admin(update, context):
        return await safe_reply_text(update, t("errors.only_admin", chat_id=update.effective_chat.id if update.effective_chat else None))
    args = context.args or []
    if not args:
        return await safe_reply_text(update, "استفاده: /dm on | off | status")
    sub = args[0].lower()
    # فقط سوپر ادمین حق تغییر سراسری دارد
    if sub in ("on", "off"):
        if not _require_super_admin(update, pv_only=True):
            audit_admin_action(update, "dm", {"sub": sub}, ok=False, reason="not_super_admin_or_not_pv")
            return await safe_reply_text(update, t("errors.only_super_admin_pv_only",
                chat_id=update.effective_chat.id if update.effective_chat else None))

    if sub == "status":
        await safe_reply_text(update,
            f"DM Global: {'ON' if is_dm_globally_on() else 'OFF'}\n"
            f"Policy: {DM_POLICY}\n"
            f"ENV Allow: {'all' if (-1 in ALLOWED_DM_ENV) else (','.join(map(str, ALLOWED_DM_ENV)) or 'None')}"
        )
        return
    if sub in ("on", "off"):
        prev = (get_config("dm_global") or "")
        set_config("dm_global", sub)
        audit_admin_action(update, "dm", {"sub": sub}, ok=True, prev_value=prev, new_value=sub)
        await safe_reply_text(update, f"DM Global تنظیم شد: {sub.upper()}")
        return

        
@admin_throttle(window_sec=3)
async def chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    مدیریت روشن/خاموش‌بودن «چت هوش‌مصنوعی» در کل ربات.
    فقط ادمین اجازه دارد.
    حالت‌ها:
      /chat on       → فعال
      /chat off      → غیرفعال
      /chat status   → نمایش وضعیت فعلی
    پیاده‌سازی با کلید bot_config: chat_feature
    """
    # ثبت/به‌روزرسانی اطلاعات کاربر در DB
    upsert_user_from_update(update)

    # فقط ادمین (با پشتیبانی از ادمین ناشناس در گروه)
    if not await _require_admin(update, context):
        return await safe_reply_text(update, t("errors.only_admin", chat_id=update.effective_chat.id if update.effective_chat else None))

    args = context.args or []
    sub = (args[0].lower() if args else "status").strip()
    # شاخهٔ on/off فقط برای سوپر ادمین (PV-only)
    if sub in ("on", "off"):
        if not _require_super_admin(update, pv_only=True):
            audit_admin_action(update, "chat", {"sub": sub}, ok=False, reason="not_super_admin_or_not_pv")
            return await safe_reply_text(update, t("errors.only_super_admin_pv_only",
                chat_id=update.effective_chat.id if update.effective_chat else None))

    # --- NEW: /chat autoclean <sec|Xm|off> (per-group) ---
    if sub in ("autoclean", "autodel", "autodelete"):
        tgt = await resolve_target_chat_id(update, context)
        if not tgt:
            return await safe_reply_text(
                update,
                "برای تنظیم «autoclean» داخل گروه این دستور رو بزن، یا در پی‌وی اول یک گروه رو با /manage وصل کن."
            )

        # مقدار ورودی
        val = (args[1].strip().lower() if len(args) >= 2 else "")

        # اگر آرگومان نداد → فقط نمایش مقدار فعلی
        if not val:
            sec = chat_ai_autoclean_sec(tgt)
            return await safe_reply_text(update, f"⏱ autoclean این گروه: {sec} ثانیه (خاموش=off)")

        # خاموش‌کردن
        if val in ("off", "disable", "0"):
            chat_cfg_set(tgt, "chat_ai_autoclean_sec", "0")
            return await safe_reply_text(update, "🧹 پاک‌سازی خودکار: خاموش شد.")

        # پارس مقدار ثانیه/دقیقه
        try:
            if val.endswith("m"):
                seconds = int(float(val[:-1])) * 60
            elif val.endswith("s"):
                seconds = int(float(val[:-1]))
            else:
                seconds = int(float(val))
            if seconds < 0:
                raise ValueError()
        except Exception:
            return await safe_reply_text(
                update,
                "فرمت درست: /chat autoclean <ثانیه|Xm|off>\n"
                "مثال‌ها: /chat autoclean 120  یا  /chat autoclean 2m  یا  /chat autoclean off"
            )

        chat_cfg_set(tgt, "chat_ai_autoclean_sec", str(seconds))
        return await safe_reply_text(update, f"⏱ autoclean این گروه روی {seconds} ثانیه تنظیم شد.")


    # تابع کمکی: خواندن وضعیت جاری از bot_config (پیش‌فرض: ON)
    def _chat_feature_on_now() -> bool:
        v = get_config("chat_feature")  # ممکن است None باشد
        return str(v or "on").strip().lower() in ("on", "1", "true", "yes")

    # نمایش وضعیت
    if sub in ("status", "info", "?"):
        status = "ON" if _chat_feature_on_now() else "OFF"
        return await safe_reply_text(update, f"Chat Feature: {status}")

    # روشن/خاموش
    if sub in ("on", "off"):
        prev = (get_config("chat_feature") or "")
        set_config("chat_feature", sub)
        audit_admin_action(update, "chat", {"sub": sub}, ok=True, prev_value=prev, new_value=sub)
        return await safe_reply_text(update, f"Chat Feature تنظیم شد: {sub.upper()}")


    # راهنما
    return await safe_reply_text(update, "استفاده: /chat on | off | status | autoclean <sec|Xm|off>")

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not await _require_admin(update, context):
        return await safe_reply_text(update, t("errors.only_admin", chat_id=update.effective_chat.id if update.effective_chat else None))

    target_id = None
    args = context.args or []
    if args:
        try:
            target_id = int(args[0])
        except Exception:
            pass
    if not target_id and update.message and update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    if not target_id:
        return await safe_reply_text(update, t("admin.allow.usage", chat_id=update.effective_chat.id if update.effective_chat else None))

    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO allowed_dm (user_id, added_by)
                VALUES (%s, %s)
                ON CONFLICT (user_id) DO NOTHING
            """, (target_id, update.effective_user.id))
            conn.commit()
        await safe_reply_text(update, t("admin.allow.ok", chat_id=update.effective_chat.id if update.effective_chat else None, target_id=target_id))
    except Exception as e:
        await safe_reply_text(update, t("errors.action.with_reason", chat_id=update.effective_chat.id if update.effective_chat else None, action="allow", reason=f"{e}"))

async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not await _require_admin(update, context):
        return await safe_reply_text(update, t("errors.only_admin", chat_id=update.effective_chat.id if update.effective_chat else None))
    target_id = None
    args = context.args or []
    if args:
        try:
            target_id = int(args[0])
        except Exception:
            pass
    if not target_id and update.message and update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    if not target_id:
        return await safe_reply_text(update, t("admin.block.usage", chat_id=update.effective_chat.id if update.effective_chat else None))
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM allowed_dm WHERE user_id=%s", (target_id,))
            conn.commit()
        await safe_reply_text(update, t("admin.block.ok", chat_id=update.effective_chat.id if update.effective_chat else None, target_id=target_id))
    except Exception as e:
        await safe_reply_text(update, t("errors.action.with_reason", chat_id=update.effective_chat.id if update.effective_chat else None, action="block", reason=f"{e}"))

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not await _require_admin(update, context):
        return await safe_reply_text(update, t("errors.only_admin", chat_id=update.effective_chat.id if update.effective_chat else None))
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
            return await safe_reply_text(update, t("admin.users.empty", chat_id=update.effective_chat.id if update.effective_chat else None))
        lines = [t("admin.users.title", chat_id=update.effective_chat.id if update.effective_chat else None)]
        for r in rows:
            uid = r["user_id"]
            dm_icon = "✅" if r["dm_ok"] == 1 else "❌"
            uline = t(
                "admin.users.row",
                chat_id=update.effective_chat.id if update.effective_chat else None,
                uid=uid,
                username=(r["username"] or "-"),
                first_name=(r["first_name"] or ""),
                last_name=(r["last_name"] or ""),
                dm_icon=dm_icon,
                last_seen=(r["last_seen_at"] or "-")
            )

            lines.append(uline)
        await safe_reply_text(update, "\n".join(lines))
    except Exception as e:
        await safe_reply_text(update, t("errors.action.with_reason", chat_id=update.effective_chat.id if update.effective_chat else None, action="users", reason=f"{e}"))

async def unknowns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not await _require_admin(update, context):
        return await safe_reply_text(update, t("errors.only_admin", chat_id=update.effective_chat.id if update.effective_chat else None))
    try:
        with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT id, chat_id, user_id, session_id, left(question, 180) AS q, reported, created_at::text AS ts
                FROM unknown_questions
                ORDER BY id DESC
                LIMIT 20
            """)
            rows = cur.fetchall() or []
        if not rows:
            return await safe_reply_text(update, t("admin.unknowns.empty", chat_id=update.effective_chat.id if update.effective_chat else None))
        lines = [t("admin.unknowns.title", chat_id=update.effective_chat.id if update.effective_chat else None)]
        for r in rows:
            tag = "📨" if not r["reported"] else "✅"
            lines.append(f"{tag} #{r['id']:>4} | {r['ts']} | chat:{r['chat_id']} | user:{r['user_id']} | {r['q']}")
        await safe_reply_text(update, "\n".join(lines))
    except Exception as e:
        await safe_reply_text(update, t("errors.action.with_reason", chat_id=update.effective_chat.id if update.effective_chat else None, action="unknowns", reason=f"{e}"))
        
        
async def fixcommands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    if not await _require_admin(update, context):
        return await safe_reply_text(update, t("errors.only_admin",
                                               chat_id=update.effective_chat.id if update.effective_chat else None))
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return await safe_reply_text(update, t("errors.only_in_group",
                                               chat_id=update.effective_chat.id if update.effective_chat else None))
    # سکوپ‌های محلی همین گروه را پاک کن تا منوی سراسری دیده شود
    try:
        await context.bot.delete_my_commands(scope=BotCommandScopeChat(chat.id))
    except Exception:
        pass
    try:
        await context.bot.delete_my_commands(scope=BotCommandScopeChatAdministrators(chat.id))
    except Exception:
        pass
    return await safe_reply_text(update, "✅ منوی دستورات محلی گروه پاک شد.")

    
# ----------------------------- Language Switcher -----------------------------
LANG_CHOICES = [
    ("fa", "🇮🇷 فارسی"),
    ("en", "🇬🇧 English"),
    ("ar", "🇸🇦 العربية"),
    ("tr", "🇹🇷 Türkçe"),
    ("ru", "🇷🇺 Русский"),
]

async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش انتخاب‌گر زبان در خودِ گروه (فقط ادمین)."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return await safe_reply_text(update, t("errors.only_in_group", chat_id=update.effective_chat.id if update.effective_chat else None))
    # ادمین‌بودن الزامی
    if not await _require_admin(update, context):
        return await safe_reply_text(update, t("errors.only_admin", chat_id=chat.id))

    rows = []
    row = []
    for i, (code, title) in enumerate(LANG_CHOICES, start=1):
        row.append(InlineKeyboardButton(title, callback_data=f"lang:set:{code}"))
        if i % 3 == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    kb = InlineKeyboardMarkup(rows)
    await safe_reply_text(update, t("lang.picker.title", chat_id=chat.id), reply_markup=kb)

async def on_lang_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ثبت زبان انتخاب‌شده برای همین گروه در DB و اعلام نتیجه."""
    q = update.callback_query
    data = (q.data or "")
    parts = data.split(":")
    code = parts[-1] if len(parts) >= 3 else None
    if code not in dict(LANG_CHOICES):
        return await q.answer("Invalid.")
    chat = update.effective_chat
    # ادمین‌بودن الزامی
    if not await _require_admin(update, context):
        return await q.answer(t("errors.only_admin_short", chat_id=chat.id))
    # ذخیره در DB (DB-first)
    chat_cfg_set(chat.id, "lang", code)
    # اعلام نتیجه و بستن نوتیف
    await q.answer(t("lang.changed.ok", chat_id=chat.id, lang=dict(LANG_CHOICES)[code]))


@log_exceptions
@admin_throttle(window_sec=2)
async def loglevel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # فقط سوپر ادمین و فقط در PV
    if not _require_super_admin(update, pv_only=True):
        audit_admin_action(update, "loglevel", {"args": context.args}, ok=False, reason="not_super_admin_or_not_pv")
        return await safe_reply_text(
            update,
            t("errors.only_super_admin_pv_only", chat_id=update.effective_chat.id if update.effective_chat else None)
        )

    args = (context.args or [])
    if not args:
        lvl = logging.getLevelName(logging.getLogger().getEffectiveLevel())
        return await safe_reply_text(update, f"🔎 سطح فعلی لاگ: {lvl}")

    want = (args[0] or "").upper()
    prev = logging.getLevelName(logging.getLogger().getEffectiveLevel())

    new = apply_level(want)  # اعمال بلافاصله
    if new == want:
        set_config("log_level", new)  # پایداری در DB
        audit_admin_action(update, "loglevel", {"want": want}, ok=True, prev_value=prev, new_value=new)
        return await safe_reply_text(update, f"✅ سطح لاگ شد: {new}")

    audit_admin_action(update, "loglevel", {"want": want}, ok=False, reason="invalid_level",
                       prev_value=prev, new_value=prev)
    return await safe_reply_text(update, "⚠️ مقدار نامعتبر. سطوح مجاز: DEBUG/INFO/WARNING/ERROR/CRITICAL")

@log_exceptions
@admin_throttle(window_sec=2)
async def lognoise_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """تنظیم سطح لاگِ کتابخانه‌های پرحرف (telegram/httpx/httpcore/urllib3/apscheduler)."""
    if not _require_super_admin(update, pv_only=True):
        audit_admin_action(update, "lognoise", {"args": context.args}, ok=False, reason="not_super_admin_or_not_pv")
        return await safe_reply_text(update, t("errors.only_super_admin_pv_only",
                                              chat_id=update.effective_chat.id if update.effective_chat else None))

    args = (context.args or [])
    if not args:
        lvl = (get_config("log_noisy_level") or "WARNING").upper()
        return await safe_reply_text(update, f"🔎 سطح لاگ کتابخانه‌ها: {lvl}")

    want = (args[0] or "").upper()
    prev = (get_config("log_noisy_level") or "WARNING").upper()
    if want not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        audit_admin_action(update, "lognoise", {"want": want}, ok=False, reason="invalid_level",
                           prev_value=prev, new_value=prev)
        return await safe_reply_text(update, "سطح مجاز: DEBUG/INFO/WARNING/ERROR/CRITICAL")

    # اعمال فوری
    try:
        from logging_setup import apply_libs_level
        apply_libs_level(want)
    except Exception:
        pass

    # پایداری در DB
    set_config("log_noisy_level", want)
    audit_admin_action(update, "lognoise", {"want": want}, ok=True, prev_value=prev, new_value=want)
    return await safe_reply_text(update, f"✅ سطح لاگ کتابخانه‌ها شد: {want}")
    
    
    
# --- NEW: /audit (PV-only, Super Admin) --------------------------------------
@log_exceptions
@admin_throttle(window_sec=3)
async def audit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    """
    نمایش لاگ ممیزی تغییرات سطح‌بالا (loglevel, chat on/off, dm on/off, ...).
    سیاست امنیتی:
      - فقط در PV و فقط برای سوپرادمین.
    ورودی‌ها:
      /audit                 → ۲۰ رکورد آخر
      /audit 50              → ۵۰ رکورد آخر (حداکثر ۱۰۰)
      /audit cmd=loglevel    → فیلتر بر اساس نام دستور
      /audit user=5620665435 → فیلتر بر اساس آیدی کاربر
      /audit chat=-100123... → فیلتر بر اساس آیدی چت
      ترکیب هم مجاز است: /audit cmd=chat user=5620 50
    """
    chat = update.effective_chat
    if not _require_super_admin(update, pv_only=True):
        return await safe_reply_text(update, t("errors.only_super_admin_pv_only",
            chat_id=chat.id if chat else None))

    args = context.args or []

    # پیش‌فرض‌ها
    limit = 20
    limit_max = 100
    cmd_like: Optional[str] = None
    user_id: Optional[int] = None
    chat_id: Optional[int] = None

    # پارس آرگومان‌ها
    for a in args:
        a = str(a).strip()
        if not a:
            continue
        if a.isdigit():
            # تعداد رکورد
            limit = max(1, min(limit_max, int(a)))
            continue
        if a.lower().startswith("cmd="):
            cmd_like = a.split("=", 1)[1].strip()
            continue
        if a.lower().startswith("user="):
            try:
                user_id = int(a.split("=", 1)[1].strip())
            except Exception:
                pass
            continue
        if a.lower().startswith("chat="):
            try:
                chat_id = int(a.split("=", 1)[1].strip())
            except Exception:
                pass
            continue

    # ساخت کوئری ایمن با فیلترهای اختیاری
    sql = """
        SELECT ts, by_user, chat_id, command, args, prev_value, new_value, ok, reason
        FROM admin_audit
        WHERE 1=1
    """
    params = []
    if cmd_like:
        sql += " AND command ILIKE %s"
        params.append(f"%{cmd_like}%")
    if user_id is not None:
        sql += " AND by_user = %s"
        params.append(user_id)
    if chat_id is not None:
        sql += " AND chat_id = %s"
        params.append(chat_id)

    sql += " ORDER BY ts DESC LIMIT %s"
    params.append(limit)

    # اجرا
    rows = []
    try:
        with db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except Exception as e:
        # پیام خطا برای سوپرادمین (شفاف)
        return await safe_reply_text(update, f"❌ audit failed: {e}")

    if not rows:
        return await safe_reply_text(update, t("admin.audit.empty",
            chat_id=chat.id if chat else None) or "رکوردی پیدا نشد.")

    # خروجی متنی (ایجاز + رعایت محدودیت 4096 کاراکتر)
    lines = []
    for r in rows:
        ok_emoji = "✅" if r["ok"] else "❌"
        # TS به UTC برای یکدستی
        ts = r["ts"]
        try:
            ts_str = ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts_str = str(ts)
        # خلاصه args (در صورت وجود)
        args_short = ""
        try:
            if r["args"] is not None:
                if isinstance(r["args"], (dict, list)):
                    args_short = json.dumps(r["args"], ensure_ascii=False)
                else:
                    args_short = str(r["args"])
                if len(args_short) > 120:
                    args_short = args_short[:117] + "..."
        except Exception:
            pass

        line = (f"{ok_emoji} {ts_str} — {r['command']}"
                f" | by {r['by_user']}"
                f"{(' | chat ' + str(r['chat_id'])) if r['chat_id'] else ''}"
                f"{(' | args ' + args_short) if args_short else ''}"
                f"{(' | prev=' + str(r['prev_value'])) if r['prev_value'] else ''}"
                f"{(' → new=' + str(r['new_value'])) if r['new_value'] else ''}"
                f"{(' | ⚠ ' + str(r['reason'])) if r['reason'] else ''}")
        lines.append(line)

    head = t("admin.audit.title",
             chat_id=chat.id if chat else None,
             n=len(rows)) or f"گزارش ممیزی (آخرین {len(rows)} مورد):"
    text = head + "\n" + "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    return await safe_reply_text(update, text)
# --- END /audit ----------------------------------------------------------------


def register_superadmin_tools(app):
    from telegram.ext import CommandHandler, filters as tg_filters
    app.add_handler(CommandHandler("loglevel", loglevel_cmd, filters=tg_filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("lognoise", lognoise_cmd, filters=tg_filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("audit",    audit_cmd,    filters=tg_filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("sa",       sa_cmd,       filters=tg_filters.ChatType.PRIVATE))

# === /sa (Super Admin management) =============================================
def _sa_load_ids(get_config_fn) -> List[int]:
    raw = (get_config_fn("super_admin_ids") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)  # پشتیبانی از JSON: ["5620665435","123"]
        return [int(x) for x in data]
    except Exception:
        # پشتیبانی از CSV: "5620665435,123"
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        out = []
        for p in parts:
            try:
                out.append(int(p))
            except Exception:
                continue
        return out

def _sa_save_ids(set_config_fn, ids: List[int]) -> None:
    # همیشه به صورت JSON مرتب ذخیره کن (DB-first منبع حقیقت)
    uniq_sorted = sorted(set(int(x) for x in ids))
    set_config_fn("super_admin_ids", json.dumps(uniq_sorted, ensure_ascii=False))


# --- shim decorator: require_super_admin ---------------------------------------
# دکوراتور سبک که از گیت موجود _require_super_admin استفاده می‌کند.
# نکتهٔ مهم: عمداً هیچ تایپ‌هینتی نزده‌ایم تا نیازی به ایمپورت Update/ContextTypes نباشد.

def require_super_admin(pv_only=True):
    """
    گِیت سوپرادمین با ممیزی روی ردها:
      - PV-only (اختیاری)
      - رد پیام‌های فوروارد/اتوفوروارد
      - رد پیام‌های ارسالی توسط botها
      - رد پیام‌های sender_chat (ادمین ناشناس/کانالی)
      - ثبت reason در جدول ممیزی برای traceability
    """
    def _decorator(func):
        @wraps(func)
        async def _wrapped(update, context, *args, **kwargs):
            u = update.effective_user
            chat = update.effective_chat
            msg = update.effective_message

            # تشخیص نام دستور برای ممیزی تمیزتر
            cmd_name = None
            try:
                txt = (msg.text or msg.caption or "").strip()
                cmd_name = txt.split()[0] if txt.startswith("/") else func.__name__
            except Exception:
                cmd_name = func.__name__

            # قوانین رد + reason
            reason = None
            if pv_only and (not chat or str(chat.type).lower() != "private"):
                reason = "not_private"
            elif msg and getattr(msg, "sender_chat", None) is not None:
                reason = "sender_chat"
            elif msg and is_forwarded_message(msg):
                reason = "forwarded"
            elif u and getattr(u, "is_bot", False):
                reason = "bot_sender"
            elif not (u and is_superadmin(int(u.id))):
                reason = "not_super_admin"

            if reason:
                # ممیزی رد
                audit_admin_action(update, cmd_name, {"pv_only": pv_only}, ok=False, reason=reason)
                # پیام خطای i18n یکدست (مختصر و امن)
                await safe_reply_text(update, t("errors.only_super_admin_pv_only",
                    chat_id=chat.id if chat else None))
                return

            return await func(update, context, *args, **kwargs)
        return _wrapped
    return _decorator

# -------------------------------------------------------------------------------



@require_super_admin(pv_only=True)
@admin_throttle(window_sec=3)
async def sa_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /sa list
    /sa add <user_id>
    /sa remove <user_id>
    """
    from shared_utils import get_config, set_config  # استفاده از utilهای مشترک (DB-first)
    args = context.args or []

    if not args:
        await update.effective_message.reply_text(
            "استفاده: /sa list | /sa add <user_id> | /sa remove <user_id>"
        )
        return

    sub = args[0].lower().strip()
    ids = _sa_load_ids(get_config)

    if sub == "list":
        if not ids:
            await update.effective_message.reply_text("هیچ سوپر ادمینی ثبت نشده است.")
            return
        await update.effective_message.reply_text(
            "فهرست سوپر ادمین‌ها:\n" + "\n".join(f"• {x}" for x in ids)
        )
        return

    if sub == "add":
        if len(args) < 2:
            await update.effective_message.reply_text("شناسهٔ کاربر را بدهید: /sa add 5620665435")
            return
        try:
            uid = int(args[1])
        except Exception:
            await update.effective_message.reply_text("شناسهٔ معتبر نیست.")
            return
        if uid in ids:
            await update.effective_message.reply_text("این شناسه قبلاً سوپر ادمین بوده.")
            return
        prev = list(ids)
        ids.append(uid)
        _sa_save_ids(set_config, ids)
        # توجه: این تابع sync است؛ نباید await شود
        audit_admin_action(update, "sa.add", {"uid": uid}, ok=True, prev_value=prev, new_value=ids)
        await update.effective_message.reply_text(f"✅ شناسه {uid} به فهرست سوپر ادمین‌ها اضافه شد.")
        return


    if sub == "remove":
        if len(args) < 2:
            await update.effective_message.reply_text("شناسهٔ کاربر را بدهید: /sa remove 5620665435")
            return
        try:
            uid = int(args[1])
        except Exception:
            await update.effective_message.reply_text("شناسهٔ معتبر نیست.")
            return
        if uid not in ids:
            await update.effective_message.reply_text("این شناسه در فهرست سوپر ادمین‌ها نیست.")
            return
        prev = list(ids)
        ids = [x for x in ids if x != uid]
        _sa_save_ids(set_config, ids)
        # توجه: این تابع sync است؛ نباید await شود
        audit_admin_action(update, "sa.remove", {"uid": uid}, ok=True, prev_value=prev, new_value=ids)
        await update.effective_message.reply_text(f"🗑️ شناسه {uid} از فهرست سوپر ادمین‌ها حذف شد.")
        return

    await update.effective_message.reply_text(
        "زیر‌فرمان ناشناخته.\nاستفاده: /sa list | /sa add <user_id> | /sa remove <user_id>"
    )