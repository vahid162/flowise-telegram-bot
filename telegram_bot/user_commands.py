import io
import asyncio
import json
import time  # برای محدود کردن تعداد پیام‌های «خاموش است»
import os

from shared_utils import db_conn, log_exceptions, log
from flowise_client import ping_flowise


from time import perf_counter
from shared_utils import chat_cfg_get, chat_cfg_set  # برای خواندن/ثبت تنظیمات زبان (DB-first)
from telegram import constants as C
from os import getenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply, ReplyKeyboardRemove

# برای پنل مدیریتی
from panel_ui import render_home, render_module_panel, parse_callback, render_group_picker_text_kb

# پیام «لطفاً صبر کنید» برای محدودسازی سرعت پاسخ‌دهی (throttle)
THROTTLE_MSG = "⏳ لطفاً کمی صبر کنید و دوباره امتحان کنید."

from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes, ApplicationHandlerStop
from shared_utils import (
    safe_reply_text, upsert_user_from_update, maybe_refresh_ui, force_clear_session,
    is_superadmin, is_dm_allowed, call_flowise, is_unknown_reply, save_unknown_question,
    save_local_history, get_session, get_local_history, get_or_rotate_session,
    set_chat_ui_ver, UI_SCHEMA_VERSION, has_any_feedback_for_message, save_feedback,
    count_feedback, mark_unknown_reported, log, PRIVATE_DENY_MESSAGE, is_addressed_to_bot, get_config, cfg_get_str,
    build_pv_deny_text_links, build_sender_html_from_update,
    CHAT_AI_DEFAULT_ENABLED, CHAT_AI_DEFAULT_MODE, CHAT_AI_DEFAULT_MIN_GAP_SEC, chat_ai_is_enabled, chat_cfg_get,
    chat_ai_autoclean_sec, delete_after, ensure_chat_defaults,
    TG_ANON
)
from shared_utils import bind_admin_to_group, set_active_admin_group, resolve_target_chat_id, check_admin_status, list_admin_groups
from messages_service import t

# --- کانتکست پنل برای هر کاربر در PV ---
# در user_data نگه می‌داریم تا بتوانیم همان پیام پنل را با editMessageText آپدیت کنیم.
PANEL_CTX_KEY = "panel_ctx"   # dict: { "panel_msg_chat_id": int, "panel_msg_id": int, "active_tab": "home|ads|chat" }
PANEL_AWAIT_KEY = "panel_await"  # dict: {"module":"ads|chat", "field":"...", "title":"..."}

# پیام ForceReply پنل را هم نگه می‌داریم تا بتوانیم لغوش کنیم
PANEL_AWAIT_MSG_KEY = "panel_await_msg_id"   # message_id پیام ForceReply
PANEL_AWAIT_CHAT_KEY = "panel_await_chat_id" # chat_id پیام ForceReply (PV)


# --- سوییچ سراسری چت هوش‌مصنوعی ---
def _chat_feature_on() -> bool:
    """
    اگر bot_config: chat_feature روی 'off' باشد → False
    در غیر این صورت (یا اگر تنظیمی ثبت نشده) → True
    """
    v = get_config("chat_feature")
    return str(v or "on").strip().lower() in ("on", "1", "true", "yes")

# پیام یکسان برای UX بهتر وقتی چت خاموش است
CHAT_OFF_MSG = "🔕 این قسمت فعلاً خاموشه. "
# هر چت حداکثر هر X ثانیه یک‌بار «خاموشه» ببیند
CHAT_OFF_NOTIFY_GAP = 30  # ثانیه
_last_chat_off_ts = {}    # chat_id -> unix time

# پیام‌های راهنمای مود چت (برای وقتی که باید روش پرسیدن را به کاربر بگوییم)
MODE_HINT_NOTIFY_GAP = 30  # ثانیه
_last_mode_hint_ts = {}    # chat_id -> unix time

def _should_notify_mode_hint(chat_id: int) -> bool:
    now = time.time()
    last = _last_mode_hint_ts.get(chat_id, 0)
    if now - last >= MODE_HINT_NOTIFY_GAP:
        _last_mode_hint_ts[chat_id] = now
        return True
    return False


# --- کنترل پایهٔ Chat AI per-group (نسخهٔ ساده‌شده: فقط دو مود mention|all) ---
_last_chat_ai_ts = {}  # (chat_id, thread_id) -> unix time

async def _chat_ai_should_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_username: str, bot_id: int) -> bool:
    """
    سیاست نهایی پاسخ‌گویی Chat-AI با دو مود:
      - mention: فقط وقتی خطاب صریح باشد (منشن @Bot یا ریپلای به پیام خودِ بات).
      - all: به همهٔ پیام‌ها پاسخ بده *جز* وقتی کاربر به پیام «کاربر دیگری» ریپلای کرده؛
             مگر این که همان پیام خطاب صریح به بات باشد.

    قواعد عمومی:
      - در PV همیشه پاسخ بده.
      - اگر پیام «اتوفوروارد کانال به گروهِ discussion» است → پاسخ نده.
      - اگر admins-only=on باشد: فقط ادمین همان گروه مجاز است «و» باید خطاب صریح باشد.
      - محدودیت فاصلهٔ زمانی per-thread رعایت می‌شود.
    """
    msg = update.effective_message
    chat = update.effective_chat

    # PV همیشه پاسخ می‌دهیم
    if not chat or chat.type == "private":
        return True

    # فقط گروه/سوپرگروه
    if chat.type not in ("group", "supergroup"):
        return False

    # خاموش بودن Chat-AI
    en = (chat_cfg_get(chat.id, "chat_ai_enabled") or CHAT_AI_DEFAULT_ENABLED).strip().lower()
    if en not in ("on", "1", "true", "yes"):
        return False

    # نرمال‌سازی مود به {mention|all}
    mode = (chat_cfg_get(chat.id, "chat_ai_mode") or CHAT_AI_DEFAULT_MODE).strip().lower()
    if mode not in ("mention", "all"):
        # نگاشت مودهای قدیمی (reply/command) به mention
        mode = "mention"

    # پیام‌های اتوفوروارد کانال به گروه گفتگو را پاسخ نده (Bot API/PTB)
    if getattr(msg, "is_automatic_forward", False):
        return False  # True if channel post auto-forwarded to discussion. :contentReference[oaicite:2]{index=2}

    # محدودکنندهٔ فاصلهٔ زمانی (بر اساس thread)
    try:
        gap = int(chat_cfg_get(chat.id, "chat_ai_min_gap_sec") or CHAT_AI_DEFAULT_MIN_GAP_SEC)
    except Exception:
        gap = int(CHAT_AI_DEFAULT_MIN_GAP_SEC)
    now = time.time()
    thread_id = getattr(msg, "message_thread_id", None) or 0
    key = (chat.id, thread_id)
    last = _last_chat_ai_ts.get(key, 0)
    if gap > 0 and (now - last) < gap:
        return False

    # آیا خطاب صریح به بات است؟ (mention/@ یا ریپلای به خودِ بات)
    addressed = is_addressed_to_bot(update, bot_username, bot_id)  # از shared_utils

    # admins-only: فقط ادمینِ همین گروه «و» الزاماً خطاب صریح
    admins_only = (chat_cfg_get(chat.id, "chat_ai_admins_only") or "off").strip().lower() in ("on", "1", "true", "yes")
    if admins_only:
        u = update.effective_user
        # Anonymous Admin = sender_chat خود گروه یا @GroupAnonymousBot
        is_anon_admin = (u and int(getattr(u, "id", 0)) == int(TG_ANON)) \
            or (getattr(msg, "sender_chat", None) is not None and msg.sender_chat.id == chat.id)  # PTB: sender_chat for anonymous admins. :contentReference[oaicite:3]{index=3}
        is_grp_admin = False
        if not is_anon_admin and u:
            try:
                from shared_utils import is_user_admin_of_group
                is_grp_admin = await is_user_admin_of_group(context.bot, u.id, chat.id)
            except Exception:
                is_grp_admin = False
        if not (is_grp_admin or is_anon_admin):
            return False
        if not addressed:
            return False

    # منطق دو مود
    if mode == "mention":
        if not addressed:
            return False
    else:  # mode == "all"
        if msg.reply_to_message:  # Bot API: reply_to_message field. :contentReference[oaicite:4]{index=4}
            from_bot = bool(getattr(msg.reply_to_message, "from_user", None) and msg.reply_to_message.from_user.id == bot_id)
            # اگر ریپلای به کاربر دیگر باشد و خطاب صریح هم نباشد → پاسخ نده
            if (not from_bot) and (not addressed):
                return False

    # در این نقطه پاسخ می‌دهیم: مُهر زمان را ثبت کن
    _last_chat_ai_ts[key] = now
    return True



# ساخت کیبورد شناسایی سؤال نامعلوم
def unknown_keyboard(uq_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 ارسال برای آموزش ربات", callback_data=f"kb:report:{uq_id}")]
    ])

# پاسخ به سؤال نامعلوم با پیام راهنما و کیبورد گزارش
async def send_unknown_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, sid: str, uq_id: int):
    msg = (
        "❓ <b>پاسخ دقیقی پیدا نکردم</b>\n"
        "یا اینکه این سؤال خارج از حوزهٔ پاسخ‌گویی من است.\n"
        "لطفاً سؤال را کمی دقیق‌تر بنویس یا از دکمهٔ زیر برای ارسال جهت آموزش استفاده کن."
    )
    await safe_reply_text(
        update,
        msg,
        reply_markup=unknown_keyboard(uq_id),
        parse_mode=ParseMode.HTML
    )



# کیبورد بازخورد (👍👎) برای پیام‌های پاسخ
def feedback_keyboard(session_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍", callback_data=f"fb:like:{session_id}"),
        InlineKeyboardButton("👎", callback_data=f"fb:dislike:{session_id}")
    ]])

# حلقه کمکی برای نمایش وضعیت «در حال تایپ...»
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

# هندلر دستور /start (ریست جلسه)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    chat_id = update.effective_chat.id
    log.info(f"Start command in chat: {chat_id}")
    
    # NEW: اگر /start در گروه/سوپرگروه زده شد → پیام آماده‌بودن بده و تمام
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        await safe_reply_text(update, t("group.ready_min", chat_id=chat_id))
        return
    
    # --- Deep Link: /start grp_<chat_id> (ورود از دکمهٔ «مدیریت در پی‌وی») ---
    try:
        arg = (context.args[0] if context.args else "").strip()
    except Exception:
        arg = ""
    if arg.startswith("grp_"):
        try:
            target_chat_id = int(arg.split("grp_", 1)[1])
        except Exception:
            target_chat_id = None

        if target_chat_id:
            ok, code, gtitle = await check_admin_status(context.bot, update.effective_user.id, target_chat_id)
            nice = gtitle or str(target_chat_id)
            if not ok:
                if code == "BOT_NOT_IN_GROUP":
                    return await safe_reply_text(update,
                        f"❌ من هنوز عضو این گروه نیستم.\n"
                        f"گروه: {nice}\nID: {target_chat_id}\n\n"
                        "لطفاً اول من را به گروه اضافه کن، بعد دوباره روی دکمهٔ مدیریت کلیک کن."
                    )
                if code == "BOT_NOT_ADMIN":
                    return await safe_reply_text(update,
                        f"⚠️ من در این گروه «ادمین» نیستم، بنابراین به دلیل محدودیت Bot API نمی‌توانم ادمین‌بودن شما را با قطعیت تأیید کنم.\n"
                        f"گروه: {nice}\nID: {target_chat_id}\n\n"
                        "دو راه دارید:\n"
                        "1) موقتاً من را ادمین کن (می‌توانی همهٔ‌ دسترسی‌ها را خاموش بگذاری) و دوباره روی دکمهٔ مدیریت کلیک کن.\n"
                        "2) یا بعداً تست کن.\n"
                        "نکته: فقط برای «تأیید»، ادمین‌بودن لازم است."
                    )
                if code == "NOT_ADMIN":
                    return await safe_reply_text(update,
                        f"❌ شما ادمین این گروه نیستید.\n"
                        f"گروه: {nice}\nID: {target_chat_id}"
                    )
                return await safe_reply_text(update,
                    f"❌ بررسی ناموفق بود.\n"
                    f"گروه: {nice}\nID: {target_chat_id}"
                )

            # بایند + گروه فعال
            bind_admin_to_group(update.effective_user.id, target_chat_id)
            set_active_admin_group(update.effective_user.id, target_chat_id)

            # تلاش برای بازکردن پنل (PV)
            try:
                await _panel_cancel_forcereply(update, context)
                await panel_open(update, context)
                return
            except Exception:
                pass
            return await safe_reply_text(update, f"✅ اتصال به «{nice}» انجام شد (ID: {target_chat_id}).")

    # --- خانهٔ آغازین با ۴ دکمه (InlineKeyboard) ---
    force_clear_session(chat_id)
    await maybe_refresh_ui(update, chat_id)

    # به‌دست آوردن یوزرنیم بات برای لینک startgroup
    me = context.application.bot_data.get("me") or await context.bot.get_me()
    bot_username = (me.username or "").strip()

    # آیا کاربر قبلاً گروهی را برای مدیریت متصل کرده؟
    try:
        from shared_utils import list_admin_groups
        has_groups = bool(list_admin_groups(update.effective_user.id))
    except Exception:
        has_groups = False

    # ردیف‌ها: اگر گروه دارد → «مدیریت گروه‌ها»، اگر ندارد → «افزودن به گروه»
    if has_groups:
        row1_left = InlineKeyboardButton(t("home.btn.manage_groups", chat_id=chat_id), callback_data="h|panel")
    else:
        add_url = f"https://t.me/{bot_username}?startgroup=start" if bot_username else "https://t.me"
        row1_left = InlineKeyboardButton(t("home.btn.add_to_group", chat_id=chat_id), url=add_url)

    kb = InlineKeyboardMarkup([
        [row1_left, InlineKeyboardButton(t("home.btn.ask",  chat_id=chat_id), callback_data="h|ask")],
        [InlineKeyboardButton(t("home.btn.help", chat_id=chat_id), callback_data="h|help"),
         InlineKeyboardButton(t("home.btn.lang", chat_id=chat_id), callback_data="h|lang")],
    ])

    # متن خوش‌آمد به‌صورت i18n
    await safe_reply_text(
        update,
        t("home.welcome", chat_id=chat_id),
        reply_markup=kb
    )

# هندلر دستور /help (نمایش راهنما)
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /help — راهنمای کامل ربات با بخش‌بندی (i18n)
    متن‌ها از fa.json خوانده می‌شوند.
    """
    upsert_user_from_update(update)
    chat = update.effective_chat
    section = (context.args[0].lower() if context.args else "").strip()

    # متون از i18n
    overview   = t("help.overview",   chat_id=chat.id if chat else None)
    chat_help  = t("help.chat",       chat_id=chat.id if chat else None)
    ads_help   = t("help.ads",        chat_id=chat.id if chat else None)
    admin_help = t("help.admin",      chat_id=chat.id if chat else None)
    shortcuts  = t("help.shortcuts",  chat_id=chat.id if chat else None)

    if section in ("ads", "ad", "guard"):
        text = ads_help

    elif section in ("admin", "admins", "مدیر", "ادمین"):
        text = admin_help

    elif section in ("chat", "general", "gen"):
        text = chat_help
        # اگر فقط ادمین‌ها اجازه چت دارند، هشدار مخصوص را الحاق کن
        if chat and chat.type in ("group", "supergroup"):
            admins_only = (chat_cfg_get(chat.id, "chat_ai_admins_only") or "off").strip().lower() in ("on", "1", "true", "yes")
            if admins_only:
                text += "\n" + t("help.chat.admins_only_note", chat_id=chat.id)

    elif section in ("?", "help"):
        text = overview

    else:
        # پیش‌فرض: نمای کلی + میانبرها
        text = overview + "\n" + shortcuts

    await safe_reply_text(update, text)




async def manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """در گروه: دکمهٔ دیپ‌لینک به پی‌وی می‌دهد. در پی‌وی: اگر گروه فعال ست نیست، راهنما می‌دهد."""
    chat = update.effective_chat
    u = update.effective_user
    me = context.bot_data.get("me") or await context.bot.get_me()
    if chat and chat.type in ("group", "supergroup"):
        # deep link: /start grp_<chat_id>
        deep = f"https://t.me/{me.username}?start=grp_{chat.id}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("مدیریت در پی‌وی 🔐", url=deep)]])
        return await update.effective_message.reply_text("برای مدیریت این گروه در پی‌وی کلیک کن:", reply_markup=kb)

    # در پی‌وی: اگر گروه فعال نداریم، راهنما بده
    tgt = await resolve_target_chat_id(update, context)
    if not tgt:
        return await update.effective_message.reply_text(
            "هنوز هیچ گروهی را به پی‌وی متصل نکرده‌ای.\n"
            "در گروه موردنظر بزن: /manage و روی دکمه کلیک کن."
        )
    return await update.effective_message.reply_text("گروه فعال شما ست است. از دستورات /ads ... در همین‌جا استفاده کن؛ تغییرات روی همان گروه اعمال می‌شود.")

async def _panel_cancel_forcereply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    لغو ForceReply پنل اگر از قبل فعال است:
      - حذف پیام ForceReply (اگر قابل حذف باشد)
      - پاک کردن state انتظار
    """
    mid = context.user_data.get(PANEL_AWAIT_MSG_KEY)
    chid = context.user_data.get(PANEL_AWAIT_CHAT_KEY)
    # اگر chat_id ذخیره نشده، از چت فعلی استفاده می‌کنیم
    if not chid and update.effective_chat:
        chid = update.effective_chat.id
    if mid and chid:
        try:
            await context.bot.delete_message(chat_id=chid, message_id=mid)
        except Exception as e:
            # حذف ForceReply ممکن است به خاطر حقوق ناکافی/قدیمی بودن پیام خطا بدهد؛ برای دیباگ لاگ می‌کنیم
            log.debug("ForceReply cleanup failed for panel: chat_id=%s, message_id=%s, err=%s", chid, mid, e)

    # پاک کردن state انتظار
    context.user_data[PANEL_AWAIT_KEY] = None
    context.user_data[PANEL_AWAIT_MSG_KEY] = None
    context.user_data[PANEL_AWAIT_CHAT_KEY] = None


async def _ask_cancel_forcereply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    لغو ForceReply مربوط به /ask در همین PV:
      - حذف پیام ForceReply اگر هنوز موجود باشد
      - پاک کردن state ذخیره‌شده
    """
    mid = context.chat_data.get("await_ask_msg_id")
    chid = context.chat_data.get("await_ask_chat_id") or (update.effective_chat.id if update.effective_chat else None)
    if mid and chid:
        try:
            await context.bot.delete_message(chat_id=chid, message_id=mid)
        except Exception as e:
            # حذف ForceReply ممکن است به خاطر حقوق ناکافی/قدیمی بودن پیام خطا بدهد؛ برای دیباگ لاگ می‌کنیم
            log.debug("ForceReply cleanup failed for ask: chat_id=%s, message_id=%s, err=%s", chid, mid, e)

    context.chat_data.pop("await_ask_msg_id", None)
    context.chat_data.pop("await_ask_chat_id", None)



async def _gtitle_or_id(bot, chat_id: int) -> str:
    """
    عنوان گروه را از Bot API می‌گیریم؛ اگر در دسترس نبود، خود ID را برمی‌گردانیم.
    - Bot API: getChat → عنوان گروه را می‌دهد.
    """
    try:
        chat = await bot.get_chat(chat_id)  # Telegram Bot API: getChat
        return getattr(chat, "title", None) or str(chat_id)
    except Exception:
        return str(chat_id)


async def panel_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /panel — فقط در PV.
    رفتار: همیشه پیام «پنل جدید» بساز، فرمان کاربر (/panel) را در PV پاک کن،
    و اگر پنل قبلی وجود داشت آن را هم پاک کن تا کیبوردهای قدیمی نمانند.
    """
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return await safe_reply_text(
            update,
            t("errors.only_private_cmd", chat_id=update.effective_chat.id if update.effective_chat else None)
        )

    # 1) ForceReplyهای قدیمی را لغو کن
    await _ask_cancel_forcereply(update, context)
    await _panel_cancel_forcereply(update, context)

    # 2) «پیام دستور /panel کاربر» را در PV پاک کن (تمیزکاری UI)


    # 3) دبونس: اسپم‌های /panel زیر 0.8s را نادیده بگیر (ولی پیامشان را پاک کرده‌ایم)
    now = time.time()
    last = context.user_data.get("__last_panel_cmd_ts", 0.0)
    if (now - last) <= 0.8:
        context.user_data["__last_panel_cmd_ts"] = now
        return
    context.user_data["__last_panel_cmd_ts"] = now

    # 4) متن و کیبورد گروه‌پیکر
    text, kb = await render_group_picker_text_kb(context.bot, update.effective_user.id)

    # 5) شناسهٔ پنل قبلی (اگر هست) را بردار
    pc_prev = context.user_data.get(PANEL_CTX_KEY) or {}
    pm_chat_id_prev = pc_prev.get("panel_msg_chat_id")
    pm_msg_id_prev  = pc_prev.get("panel_msg_id")

    # 6) همیشه پیام «پنل جدید» بساز (ته چت)
    m = await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

    # 7) اگر پنل قبلی هست، پاکش کن تا دو کیبورد همزمان نماند


    # 8) کانتکست پنل را روی پیام جدید ست کن
    context.user_data[PANEL_CTX_KEY] = {
        "panel_msg_chat_id": m.chat_id,
        "panel_msg_id": m.message_id,
        "active_tab": "home"
    }
    return


async def panel_on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r"""
    همهٔ کلیک‌های پنل به اینجا می‌آیند (pattern: ^v1\|).
    1) اگر sys|pick → set_active_admin_group و رفرش Home
    2) اگر sys|home/tab → رندر مربوطه
    3) اگر ads/chat → قبل از تغییر، check_admin_status
    """
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    parts = parse_callback(data)
    module = parts.get("m","")
    action = parts.get("a","")
    val    = parts.get("val","")
    
    await _panel_cancel_forcereply(update, context)

    # کمک: پیام پنل کدام است؟
    pc = context.user_data.get(PANEL_CTX_KEY) or {}
    pm_chat_id = pc.get("panel_msg_chat_id") or q.message.chat.id
    pm_msg_id  = pc.get("panel_msg_id") or q.message.message_id

    # در PV همیشه کار می‌کنیم
    if q.message.chat.type != "private":
        await q.answer(t("errors.only_private_panel", chat_id=update.effective_chat.id if update.effective_chat else None))
        return

    # --- router: sys ---
    if module == "sys":
        if action == "help" and val == "add":
            await q.answer(
                t("panel.group_picker.add_hint", chat_id=update.effective_chat.id if update.effective_chat else None),
                show_alert=True
            )
            return

        if action == "home":
            tgt = await resolve_target_chat_id(update, context)
            if not tgt:
                text, kb = await render_group_picker_text_kb(context.bot, update.effective_user.id)
                await q.answer()
                await context.bot.edit_message_text(
                    chat_id=pm_chat_id, message_id=pm_msg_id, text=text, reply_markup=kb
                )
                return
            gtitle = await _gtitle_or_id(context.bot, tgt)
            text, kb = render_home(tgt, gtitle=gtitle)

            await _panel_cancel_forcereply(update, context)
            await q.answer()
            await context.bot.edit_message_text(
                chat_id=pm_chat_id, message_id=pm_msg_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML
            )
            # اطمینان از وجود کانتکست پنل
            ctx = context.user_data.setdefault(PANEL_CTX_KEY, {"panel_msg_chat_id": pm_chat_id, "panel_msg_id": pm_msg_id})
            ctx["active_tab"] = "home"
            return
    
        if action == "tab":   # ⟵ قبلاً startswith("tab:") بود؛ حالا از 'val' استفاده می‌کنیم
            tgt = await resolve_target_chat_id(update, context)
            if not tgt:
                return await q.answer(t("panel.group_picker.first_prompt", chat_id=update.effective_chat.id if update.effective_chat else None), show_alert=True)
            tab = val  # ⟵ مقدار بعد از ':'
            gtitle = await _gtitle_or_id(context.bot, tgt)
            text, kb = render_module_panel("ads" if tab == "ads" else "chat", tgt, gtitle=gtitle)
            await _panel_cancel_forcereply(update, context)

            await q.answer()
            await context.bot.edit_message_text(
                chat_id=pm_chat_id, message_id=pm_msg_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML
            )
            # نگه‌داشت تب فعال
            ctx = context.user_data.setdefault(PANEL_CTX_KEY, {"panel_msg_chat_id": pm_chat_id, "panel_msg_id": pm_msg_id})
            ctx["active_tab"] = "ads" if tab == "ads" else "chat"
            return
    
        if action == "pick":  # ⟵ قبلاً startswith("pick:") بود
            try:
                target_chat_id = int(val)  # ⟵ مقدار بعد از ':'
            except Exception:
                target_chat_id = None
            if not target_chat_id:
                return await q.answer(t("errors.invalid_group_id", chat_id=update.effective_chat.id if update.effective_chat else None), show_alert=True)
        
            # چک رسمی نقش‌ها با Bot API: getChatMember
            ok, code, gtitle = await check_admin_status(context.bot, update.effective_user.id, target_chat_id)
            if not ok:
                if code == "BOT_NOT_IN_GROUP":
                    return await q.answer(t("errors.bot_not_member", chat_id=update.effective_chat.id if update.effective_chat else None), show_alert=True)
                if code == "BOT_NOT_ADMIN":
                    return await q.answer(t("errors.bot_not_admin", chat_id=update.effective_chat.id if update.effective_chat else None), show_alert=True)
                # اگر کاربر ادمین نیست (شامل Anonymous Admin بدون هویت قابل تطبیق)
                return await q.answer(t("errors.user_not_admin", chat_id=update.effective_chat.id if update.effective_chat else None), show_alert=True)
        
            set_active_admin_group(update.effective_user.id, target_chat_id)
            gtitle = await _gtitle_or_id(context.bot, target_chat_id)
            text, kb = render_home(target_chat_id, gtitle=gtitle)
        
            await q.answer()
            await context.bot.edit_message_text(
                chat_id=pm_chat_id, message_id=pm_msg_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML
            )
            ctx = context.user_data.setdefault(PANEL_CTX_KEY, {"panel_msg_chat_id": pm_chat_id, "panel_msg_id": pm_msg_id})
            ctx["active_tab"] = "home"
            return

    # --- router: ads/chat ---
    tgt = await resolve_target_chat_id(update, context)
    if not tgt:
        await q.answer("ابتدا گروه را انتخاب کن.", show_alert=True)
        return

    # قبل از هر تغییری، ادمین‌بودن را برای همان گروه فعال چک کن
    ok, code, gtitle = await check_admin_status(context.bot, update.effective_user.id, tgt)
    if not ok:
        msg = "اول من را ادد/ادمین کن." if code=="BOT_NOT_IN_GROUP" else "اجازه لازم را نداری."
        await q.answer(msg, show_alert=True); return

    # اعمال تغییر و رندر مجدد تب
    updated = {}
    gtitle = await _gtitle_or_id(context.bot, tgt)  # ← نام گروه برای هدر
    if module == "ads":
        from panel_ui import handle_ads_action
        updated = handle_ads_action(tgt, action, val)
        text, kb = render_module_panel("ads", tgt, gtitle=gtitle)
    else:
        from panel_ui import handle_chat_action
        updated = handle_chat_action(tgt, action, val)
        text, kb = render_module_panel("chat", tgt, gtitle=gtitle)




    # اگر ForceReply لازم است، وضعیت انتظار را ست کن و پیام ForceReply بفرست
    aw = updated.get("__await_text__")
    if aw:
        context.user_data[PANEL_AWAIT_KEY] = aw  # {"module":"ads|chat","field":"...","title":"..."}
        # فقط برای بستن اسپینر؛ هیچ نوتیفیکیشنی نشان داده نشود
        await q.answer()
    
        # مقدار فعلی را برای نمایش در پیام و placeholder آماده می‌کنیم
        try:
            from shared_utils import chat_cfg_get
            try:
                from panel_ui import DEFAULTS as _PANEL_DEFAULTS
            except Exception:
                _PANEL_DEFAULTS = {}
            cur_val = chat_cfg_get(tgt, aw["field"])
            if cur_val in (None, ""):
                cur_val = _PANEL_DEFAULTS.get(aw["field"], "")
        except Exception:
            cur_val = None
    
        placeholder = ""
        try:
            field = str(aw.get("field", "")).strip().lower()
            if field in ("ads_chatflow_id", "chat_ai_chatflow_id", "chatflow_id", "pv_chatflow_id"):
                placeholder = "مثال: 123e4567-e89b-12d3-a456-426614174000"
            elif field in ("ads_threshold",):
                placeholder = "مثال: 0.78"
            elif field.endswith("_sec"):
                placeholder = "مثال: 120"
            elif field.endswith("_maxlen") or field.endswith("_min_len"):
                placeholder = "مثال: 160"
        except Exception:
            placeholder = ""
    
        cur_for_text = str(cur_val) if (cur_val not in (None, "")) else "—"
        from html import escape
        t_safe = escape(str(aw['title']))
        gtitle_safe = escape(str(gtitle))
        ph = (placeholder or "")[:64]
        cur_for_text_safe = escape(cur_for_text)
    
        m = await q.message.reply_text(
            f"✎ مقدار جدید {t_safe} را ارسال کن (گروه: <b>{gtitle_safe}</b>):\n"
            f"(مقدار فعلی: <code>{cur_for_text_safe}</code>)",
            reply_markup=ForceReply(selective=True, input_field_placeholder=ph),
            parse_mode=ParseMode.HTML,
        )
        context.user_data[PANEL_AWAIT_MSG_KEY] = m.message_id
        context.user_data[PANEL_AWAIT_CHAT_KEY] = m.chat_id
        return

    # در غیر اینصورت → ذخیره تمام شد؛ پیام پنل را آپدیت کن
    await q.answer(t("panel.save.ok", chat_id=update.effective_chat.id if update.effective_chat else None))
    await context.bot.edit_message_text(chat_id=pm_chat_id, message_id=pm_msg_id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def home_on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    کال‌بک کلی برای دکمه‌های صفحهٔ خانه (الگوی کوتاه: h|<action>)
    action ∈ {panel, ask, help, lang}
    """
    q = update.callback_query
    if not q or not q.data:
        return
    data = q.data or ""
    # ایمن: کوتاه، < 64 بایت (مطابق محدودیت Bot API/PTB)
    # h|ask, h|panel, h|help, h|lang
    try:
        _, action = data.split("|", 1)
    except Exception:
        await q.answer()
        return
    chat = update.effective_chat
    chat_id = chat.id if chat else None

    if action == "panel":
        # پنل فقط در PV
        if chat and chat.type != "private":
            await q.answer(t("errors.only_private_panel", chat_id=chat_id), show_alert=True)
            return
        await q.answer()  # بدون متن -> نوتیفیکیشن نشان داده نمی‌شود
        await panel_open(update, context)
        return

    if action == "ask":
        if chat and chat.type != "private":
            await q.answer(t("errors.only_private_cmd", chat_id=chat_id), show_alert=True)
            return
        # لغو ForceReplyهای قبلی و ساخت ForceReply جدید (UX ساده برای شروع سؤال)
        await _ask_cancel_forcereply(update, context)
        placeholder = t("home.ask.placeholder", chat_id=chat_id)
        m = await safe_reply_text(
            update,
            placeholder,
            reply_markup=ForceReply(
                input_field_placeholder=t("home.ask.input_hint", chat_id=chat_id),
                selective=True
            )
        )
        context.chat_data["await_ask_msg_id"] = getattr(m, "message_id", None)
        context.chat_data["await_ask_chat_id"] = chat_id
        await q.answer()
        return

    if action == "help":
        await q.answer()
        await help_cmd(update, context)
        return

    # --- زبان در PV: نمایش منو یا ثبت انتخاب ---
    if action.startswith("lang:set:"):
        # مثال: h|lang:set:fa
        parts = action.split(":", 2)
        code = parts[2] if len(parts) == 3 else None
        LANG_CHOICES = {"fa": "🇮🇷 فارسی", "en": "🇬🇧 English", "ar": "🇸🇦 العربية", "tr": "🇹🇷 Türkçe", "ru": "🇷🇺 Русский"}
        if code not in LANG_CHOICES:
            await q.answer("Invalid.", show_alert=True)
            return
        # ذخیره در DB (DB-first)
        chat = update.effective_chat
        chat_id = chat.id if chat else None
        try:
            chat_cfg_set(chat_id, "lang", code)
        except Exception:
            # اگر DB در دسترس نبود، فقط پیام کوتاه بده
            await q.answer("DB error", show_alert=True)
            return

        # اعلام موفقیت و (در صورت امکان) ادیت پیام
        await q.answer(t("lang.changed.ok", chat_id=chat_id, lang=code))
        try:
            await q.edit_message_text(text=t("home.welcome", chat_id=chat_id))
        except Exception:
            pass
        return

    if action == "lang":
        # فقط در PV زبان را نشان بده
        chat = update.effective_chat
        chat_id = chat.id if chat else None
        if chat and chat.type != "private":
            await q.answer(t("errors.only_private_cmd", chat_id=chat_id), show_alert=True)
            return

        # ساخت منوی زبان
        LANG_CHOICES = [("fa", "🇮🇷 فارسی"), ("en", "🇬🇧 English"), ("ar", "🇸🇦 العربية"), ("tr", "🇹🇷 Türkçe"), ("ru", "🇷🇺 Русский")]
        rows, row = [], []
        for i, (code, title) in enumerate(LANG_CHOICES, start=1):
            row.append(InlineKeyboardButton(title, callback_data=f"h|lang:set:{code}"))
            if i % 3 == 0:
                rows.append(row); row = []
        if row:
            rows.append(row)
        kb = InlineKeyboardMarkup(rows)

        await q.answer()
        # تیتر منو: می‌توانی از همان کلید گروهی استفاده کنی
        await q.edit_message_text(text=t("lang.picker.title", chat_id=chat_id), reply_markup=kb)
        return
    await q.answer()

async def panel_on_force_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    وقتی کاربر به پیام ForceReply (پنل) پاسخ می‌دهد:
      - مقدار جدید را در chat_config ذخیره کن
      - ForceReply را لغو/پاک کن
      - فقط یک پیام «ذخیره شد» بفرست (بدون ادیت/ارسال مجدد پنل)
    """
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return  # فقط در PV

    aw = context.user_data.get(PANEL_AWAIT_KEY)
    if not aw:
        return  # در حالت انتظار نیستیم

    tgt = await resolve_target_chat_id(update, context)
    if not tgt:
        # پیام راهنمای «ابتدا یک گروه انتخاب کن»
        return await safe_reply_text(
            update,
            t("panel.group_picker.first_prompt", chat_id=update.effective_chat.id if update.effective_chat else None)
        )

    # مقدار جدید از پیام کاربر
    new_val = (update.effective_message.text or "").strip()
    field = str(aw.get("field", "")).strip()

    # ذخیره در DB-first (بدون ادیت پنل)
    try:
        chat_cfg_set(tgt, field, new_val)
    except Exception as e:
        # ForceReply را جمع کن و خطا را اطلاع بده (i18n)
        await _panel_cancel_forcereply(update, context)
        return await safe_reply_text(
            update,
            t("errors.action.with_reason",
              chat_id=update.effective_chat.id if update.effective_chat else None,
              action="save",
              reason=str(e))
        )

    # ForceReply را پاک/لغو کن و state را خالی کن
    await _panel_cancel_forcereply(update, context)
    context.user_data[PANEL_AWAIT_KEY] = None

    # فقط پیام موفقیت (بدون ادیت/ارسال مجدد منوها)
    await safe_reply_text(
        update,
        t("panel.save.ok", chat_id=update.effective_chat.id if update.effective_chat else None)
    )
    return



# هندلر دستور /whoami (نمایش شناسه/وضعیت کاربر)
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user_from_update(update)
    # وضعیت سوپرادمین (سراسری)
    super_flag = '✅' if is_superadmin(u.id) else '❌'

    # اگر در گروه هستیم، وضعیت ادمینِ همان گروه را دقیق با Bot API بگیر
    group_flag = ''
    chat = update.effective_chat
    if chat and chat.type in ('group', 'supergroup'):
        try:
            ok, _, _ = await check_admin_status(context.bot, u.id, chat.id)
            group_flag = f"\nGroup admin here: {'✅' if ok else '❌'}"
        except Exception:
            group_flag = "\nGroup admin here: ⚠️ (check failed)"

    await safe_reply_text(update,
        f"User ID: {u.id}\n"
        f"Username: @{u.username if u.username else '-'}\n"
        f"Super admin: {super_flag}"
        f"{group_flag}\n"
        f"DM allowed now: {'✅' if is_dm_allowed(u.id) else '❌'}"
    )

# هندلر دستور /clear (پاک کردن تاریخچه جلسه جاری)
async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    chat = update.effective_chat
    user = update.effective_user
    if chat.type in ['group', 'supergroup']:
        try:
            admins = await context.bot.get_chat_administrators(chat.id)
            admin_ids = [admin.user.id for admin in admins]
            if user.id not in admin_ids:
                await safe_reply_text(update, "❌ فقط ادمین‌های گروه می‌توانند تاریخچه را پاک کنند.")
                return
        except Exception:
            await safe_reply_text(update, "❌ امکان بررسی ادمین‌ها وجود ندارد.")
            return
    force_clear_session(chat.id)
    await safe_reply_text(update, "تاریخچه پاک شد ✅")

# هندلر دستور /export (دریافت خروجی تاریخچه جلسه)
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
            await safe_reply_text(update, "هنوز مکالمه‌ای برای خروجی گرفتن وجود ندارد.")
            return
        session_id = session_row["current_session_id"]
        history = get_local_history(session_id)
        if not history:
            await safe_reply_text(update, "تاریخچه این جلسه خالی است.")
            return
        formatted_text = f"تاریخچه مکالمه برای چت: {chat_id}\nSession ID: {session_id}\n"
        formatted_text += "=" * 40 + "\n\n"
        for item in history:
            speaker = "کاربر" if item.get("type") == "human" else "ربات"
            message = item.get("message", "")
            formatted_text += f"[{speaker}]:\n{message}\n\n"
        me = context.application.bot_data.get("me") or await context.bot.get_me()
        bot_name = me.full_name
        bot_username = me.username
        signature = "\n" + "=" * 40 + f"\nخروجی گرفته شده توسط ربات:\nنام: {bot_name}\nآیدی: @{bot_username}\n"
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


# هندلر پیام‌های کاربر که در پاسخ به پیام ForceReply ربات (دستور /ask) ارسال می‌شود
async def ask_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    پاسخ به پیام کاربر وقتی روی «🤖 سؤال جدید» ForceReply داده‌ایم.
    اصلاحات:
      - فقط اگر ریپلای به «همان» پیام ForceReply اخیر باشد (chat_data.await_ask_msg_id).
      - اگر متن ارسالی «دستور» باشد (شروع با / یا entity از نوع bot_command)،
        حالت سؤال لغو و پردازش به CommandHandler واگذار می‌شود.
    """
    # باید حتماً ریپلای باشد
    if not update.message or not update.message.reply_to_message:
        return

    me = context.application.bot_data.get("me") or await context.bot.get_me()
    original_msg = update.message.reply_to_message

    # ریپلای به پیام خود ربات؟
    if not original_msg.from_user or original_msg.from_user.id != me.id:
        return

    # فقط اگر به آخرین ForceReply ما ریپلای شده باشد
    expected_mid = context.chat_data.get("await_ask_msg_id")
    if expected_mid and original_msg.message_id != expected_mid:
        return

    # متن دعوتِ ForceReply باید همان placeholder باشد
    prompt_text = (original_msg.text or "")
    if not prompt_text.startswith("سوالت رو همینجا بنویس"):
        return

    # اگر پیام فعلی «دستور» است → ForceReply را لغو کن و هیچ کاری نکن
    msg = update.message
    txt = (getattr(msg, "text", "") or "").strip()
    try:
        ents = getattr(msg, "entities", []) or []
    except Exception:
        ents = []

    is_cmd = False
    if txt.startswith("/"):
        is_cmd = True
    else:
        for e in ents:
            if getattr(e, "type", "") == "bot_command" and int(getattr(e, "offset", 0)) == 0:
                is_cmd = True
                break

    if is_cmd:
        # لغو ForceReply معلق تا UX تمیز شود
        try:
            await _ask_cancel_forcereply(update, context)
        except Exception:
            pass
        return  # اجازه بده هندلرهای دستور کار خودشان را انجام بدهند

    # --- ادامهٔ منطق فعلی (بدون تغییر رفتار) ---
    upsert_user_from_update(update)
    chat = update.effective_chat
    u = update.effective_user
    text = txt

    # (ادامهٔ کد «قبلی» همین تابع؛ از اینجا به بعد را عیناً نگه دار)
    # سیاست گفتگوی خصوصی: اگر PV مجاز نباشد، عدم دسترسی و خروج
    if chat.type == 'private' and not is_dm_allowed(u.id):
        txt = await build_pv_deny_text_links(context.bot)
        return await safe_reply_text(update, txt, parse_mode=ParseMode.HTML)

    # اگر چت سراسری خاموش باشد → پیام خاموش و خروج
    if not _chat_feature_on():
        if _should_notify_chat_off(chat.id):
            m, mid = build_sender_html_from_update(update)
            wm = await safe_reply_text(update, f"{t('chat.off.notice', chat_id=chat.id)}\nخطاب به: {m} | ID: {mid}", parse_mode=ParseMode.HTML)
            try:
                sec = chat_ai_autoclean_sec(chat.id)
                if sec and sec > 0 and wm:
                    context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                    if chat.type in ("group", "supergroup"):
                        context.application.create_task(delete_after(context.bot, chat.id, update.message.message_id, sec))
            except Exception:
                pass
        return

    # اگر چت AI این گروه خاموش باشد → پیام خاموش و خروج
    if chat.type in ("group", "supergroup"):
        if not chat_ai_is_enabled(chat.id):
            if _should_notify_chat_off(chat.id):
                m, mid = build_sender_html_from_update(update)
                wm = await safe_reply_text(update, f"{t('chat.off.notice', chat_id=chat.id)}\nخطاب به: {m} | ID: {mid}", parse_mode=ParseMode.HTML)

                try:
                    sec = chat_ai_autoclean_sec(chat.id)
                    if sec and sec > 0 and wm:
                        # حذف پیام راهنمای بات
                        context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                        # --- NEW: حذف پیام کاربر محرک ---
                        context.application.create_task(delete_after(context.bot, chat.id, update.message.message_id, sec))
                except Exception:
                    pass
            return


        
    # اگر admins_only روشن است، پاسخ به ForceReply فقط توسط ادمین‌ها مجاز است
    if chat.type in ("group", "supergroup"):
        admins_only = (chat_cfg_get(chat.id, "chat_ai_admins_only") or "off").strip().lower() in ("on", "1", "true", "yes")
        if admins_only:
            # ادمین ناشناس: یا فرستنده GroupAnonymousBot، یا پیام به‌نمایندگی از خودِ گروه (sender_chat.id == chat.id)
            is_anon_admin = (u and int(getattr(u, "id", 0)) == int(TG_ANON)) or (
                getattr(update.message, "sender_chat", None) is not None and update.message.sender_chat.id == chat.id
            )
            is_grp_admin = False
            try:
                from shared_utils import is_user_admin_of_group
                is_grp_admin = await is_user_admin_of_group(context.bot, u.id if u else 0, chat.id)
            except Exception:
                is_grp_admin = False


            if not (is_grp_admin or is_anon_admin):
                m, mid = build_sender_html_from_update(update)
                wm = await safe_reply_text(
                    update,
                    f"⛔ فقط ادمین‌های این گروه می‌توانند پاسخ بدهند.\n"
                    f"خطاب به: {m} | ID: <code>{mid}</code>",
                    parse_mode=ParseMode.HTML,
                )
                try:
                    sec = chat_ai_autoclean_sec(chat.id)
                    if sec and sec > 0 and wm:
                        # حذف پیام راهنمای بات
                        context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                        # --- NEW: حذف پیام کاربر محرک ---
                        context.application.create_task(delete_after(context.bot, chat.id, update.message.message_id, sec))
                except Exception:
                    pass
                return

        
    # (در اینجا نیازی به بررسی مجدد min_gap نیست؛ قبلاً در مرحله /ask اعمال شده است)
    sid = get_or_rotate_session(chat.id)
    stop_event = asyncio.Event()
    typing_task = context.application.create_task(
        _typing_loop(context.bot, chat.id, ChatAction.TYPING, stop_event)
    )
    try:
        flow_sid = sid if chat.type == 'private' else f"{sid}_u{u.id}"
        reply_text, src_count = await asyncio.to_thread(call_flowise, text, flow_sid, chat.id)
        if (src_count == 0) or is_unknown_reply(reply_text):
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
    await safe_reply_text(update, reply_text, reply_markup=feedback_keyboard(sid))
    
    # حذف پیام ForceReply فقط در گروه‌ها و فقط اگر autoclean>0 تنظیم شده باشد
    try:
        if chat and chat.type in ("group", "supergroup"):
            sec = chat_ai_autoclean_sec(chat.id)
            if sec and sec > 0:
                # حذف با تأخیر تنظیم‌شده برای تمیز نگه داشتن گروه
                context.application.create_task(
                    delete_after(context.bot, chat.id, original_msg.message_id, sec)
                )
            # اگر sec==0 باشد در گروه هم حذف نکن؛ در PV هم هرگز حذف نکن
    except Exception:
        pass

    # ثبت زمان آخرین پاسخ این چت/موضوع برای اعمال min_gap در آینده
    thread_id = getattr(update.message, "message_thread_id", None)
    _last_chat_ai_ts[(chat.id, thread_id or 0)] = time.time()


# هندلر دستور /ask (پرسیدن سؤال با دستور، مخصوصاً در گروه‌ها با حالت '/command')
async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    chat = update.effective_chat
    u = update.effective_user
    # متن سؤال را از آرگومان‌های دستور یا پیام ریپلای‌شده استخراج کن
    text = " ".join(context.args or []).strip()
    if not text and update.message and update.message.reply_to_message:
        src = update.message.reply_to_message
        text = (src.text or src.caption or "").strip()
    
    # --- NEW: Anti-ads pre-check for /ask args in groups ---
    if text and chat.type in ("group", "supergroup"):
        ads = context.application.bot_data.get("ads_guard")
        if ads:
            try:
                # از همان پایپ‌لاین AdsGuard استفاده می‌کنیم تا پیام اخطار/حذف و متریک‌ها یکدست بمانند
                await ads.watchdog(update, context)
            except ApplicationHandlerStop:
                # تبلیغ تشخیص داده شد؛ AdsGuard خودش اخطار/حذف را انجام داده و باید همین‌جا متوقف شویم
                return

    
    # سیاست گفتگوی خصوصی: اگر PV مجاز نباشد، پیام عدم دسترسی و خروج
    if chat.type == 'private' and not is_dm_allowed(u.id):
        return await safe_reply_text(update, PRIVATE_DENY_MESSAGE)
        
        
        
    # اگر قابلیت چت هوش‌مصنوعی سراسری خاموش باشد → پیام خاموش و خروج
    if not _chat_feature_on():
        if _should_notify_chat_off(chat.id):
            m, mid = build_sender_html_from_update(update)
            wm = await safe_reply_text(update, f"{t('chat.off.notice', chat_id=chat.id)}\nخطاب به: {m} | ID: {mid}", parse_mode=ParseMode.HTML)

            try:
                sec = chat_ai_autoclean_sec(chat.id)
                if sec and sec > 0 and wm:
                    # حذف پیام بات
                    context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                    # --- NEW: حذف پیام کاربر محرک (فقط در گروه) ---
                    if chat.type in ("group", "supergroup"):
                        context.application.create_task(delete_after(context.bot, chat.id, update.message.message_id, sec))
            except Exception:
                pass
        return

    # اگر چتِ همین گروه خاموش باشد → پیام خاموش و خروج
    if chat.type in ("group", "supergroup") and not chat_ai_is_enabled(chat.id):
        if _should_notify_chat_off(chat.id):
            m, mid = build_sender_html_from_update(update)
            wm = await safe_reply_text(update, f"{t('chat.off.notice', chat_id=chat.id)}\nخطاب به: {m} | ID: {mid}", parse_mode=ParseMode.HTML)

            try:
                sec = chat_ai_autoclean_sec(chat.id)
                if sec and sec > 0 and wm:
                    # حذف پیام بات
                    context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                    # --- NEW: حذف پیام کاربر محرک ---
                    context.application.create_task(delete_after(context.bot, chat.id, update.message.message_id, sec))
            except Exception:
                pass
        return

    
    # اگر admins-only روشن است، /ask فقط برای ادمین‌های همان گروه مجاز باشد
    if chat.type in ("group", "supergroup"):
        admins_only = (chat_cfg_get(chat.id, "chat_ai_admins_only") or "off").strip().lower() in ("on", "1", "true", "yes")
        if admins_only:
            try:
                from shared_utils import is_user_admin_of_group
                is_grp_admin = await is_user_admin_of_group(context.bot, u.id, chat.id)
            except Exception:
                is_grp_admin = False
            if not is_grp_admin:
                m, mid = build_sender_html_from_update(update)
                wm = await safe_reply_text(
                    update,
                    f"⛔ فقط ادمین‌های این گروه می‌توانند از /ask استفاده کنند.\n"
                    f"خطاب به: {m} | ID: <code>{mid}</code>",
                    parse_mode=ParseMode.HTML,
                )
                try:
                    sec = chat_ai_autoclean_sec(chat.id)
                    if sec and sec > 0 and wm:
                        # حذف پیام راهنمای بات
                        context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                        # --- NEW: حذف پیام کاربرِ محرک (فقط در گروه/سوپرگروه) ---
                        if chat.type in ("group", "supergroup"):
                            context.application.create_task(delete_after(context.bot, chat.id, update.message.message_id, sec))
                except Exception:
                    pass
                return

    # رعایت min_gap برای جلوگیری از درخواست‌های پیاپی (در سطح هر چت/موضوع)
    try:
        gap = int(chat_cfg_get(chat.id, "chat_ai_min_gap_sec") or CHAT_AI_DEFAULT_MIN_GAP_SEC)
    except Exception:
        gap = int(CHAT_AI_DEFAULT_MIN_GAP_SEC)

    if gap > 0:
        now = time.time()
        thread_id = getattr(update.message, "message_thread_id", None)
        key = (chat.id, thread_id or 0)
        last = _last_chat_ai_ts.get(key, 0)
        if (now - last) < gap:
            m, mid = build_sender_html_from_update(update)
            await safe_reply_text(update, f"{t('errors.rate_limited', chat_id=chat.id)}\nخطاب به: {m} | ID: {mid}", parse_mode=ParseMode.HTML)

            return
        _last_chat_ai_ts[key] = now
        
        
    # اگر هنوز سؤال مشخص نشده → ارسال پیام درخواست سؤال با ForceReply
    # اگر هنوز سؤال مشخص نشده
    if not text:
        # در گروه: اگر admins_only=on و کاربر ادمین نیست، اصلاً ForceReply نساز
        if chat.type in ("group", "supergroup"):
            admins_only = (chat_cfg_get(chat.id, "chat_ai_admins_only") or "off").strip().lower() in ("on", "1", "true", "yes")
            if admins_only:
                # ادمین ناشناس یا sender_chat == chat.id؟
                is_anon_admin = (u and int(getattr(u, "id", 0)) == int(TG_ANON)) or (
                    getattr(update.message, "sender_chat", None) is not None and update.message.sender_chat.id == chat.id
                )
                is_grp_admin = False
                try:
                    from shared_utils import is_user_admin_of_group
                    is_grp_admin = await is_user_admin_of_group(context.bot, u.id if u else 0, chat.id)
                except Exception:
                    is_grp_admin = False
                if not (is_grp_admin or is_anon_admin):
                    m, mid = build_sender_html_from_update(update)
                    wm = await safe_reply_text(
                        update,
                        f"⛔ فقط ادمین‌های این گروه می‌توانند از /ask استفاده کنند.\n"
                        f"خطاب به: {m} | ID: <code>{mid}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                    try:
                        sec = chat_ai_autoclean_sec(chat.id)
                        if sec and sec > 0 and wm:
                            # حذف پیام راهنمای بات
                            context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                            # حذف پیام کاربر محرک
                            context.application.create_task(delete_after(context.bot, chat.id, update.message.message_id, sec))
                    except Exception:
                        pass
                    return

        # اگر به اینجا رسیدیم، ساخت ForceReply مجاز است
        placeholder = "سوالت رو همینجا بنویس 👇"
        m = await safe_reply_text(
            update,
            placeholder,
            reply_markup=ForceReply(
                input_field_placeholder="مثال: سلام، خوبی؟؟",
                selective=True
            )
        )
        context.chat_data["await_ask_msg_id"] = getattr(m, "message_id", None)
        context.chat_data["await_ask_chat_id"] = chat.id
        return m

        
    sid = get_or_rotate_session(chat.id)
    # نشان‌دادن وضعیت «در حال تایپ...» تا زمان آماده‌شدن پاسخ
    stop_event = asyncio.Event()
    typing_task = context.application.create_task(
        _typing_loop(context.bot, chat.id, ChatAction.TYPING, stop_event)
    )
    try:
        flow_sid = sid if chat.type == 'private' else f"{sid}_u{u.id}"
        reply_text, src_count = await asyncio.to_thread(call_flowise, text, flow_sid, chat.id)
        # اگر پاسخ نامشخص بود یا منابعی پیدا نشد → ذخیره سؤال برای آموزش و ارسال پاسخ راهنما
        if (src_count == 0) or is_unknown_reply(reply_text):
            uq_id = save_unknown_question(chat.id, u.id, sid, text)
            await send_unknown_reply(update, context, sid, uq_id)
            return
    finally:
        stop_event.set()
        try:
            await typing_task
        except Exception:
            pass
        
    # ذخیره تاریخچه مکالمه (سؤال و جواب) در پایگاه داده
    save_local_history(sid, chat.id, {"type": "human", "message": text})
    save_local_history(sid, chat.id, {"type": "ai", "message": reply_text})
    # ارسال پاسخ در همان چت/موضوع به همراه دکمه‌های بازخورد
    await safe_reply_text(update, reply_text, reply_markup=feedback_keyboard(sid))
    
    # فقط در گروه‌ها پیام ForceReply را پاک کن و آن هم در صورت تنظیم autoclean>0
    try:
        if chat and chat.type in ("group", "supergroup"):
            me = context.application.bot_data.get("me") or await context.bot.get_me()
            fr = update.message.reply_to_message if update.message else None
            if fr and fr.from_user and fr.from_user.id == me.id:
                prompt_text = (fr.text or "")
                if prompt_text.startswith("سوالت رو همینجا بنویس"):
                    sec = chat_ai_autoclean_sec(chat.id)
                    if sec and sec > 0:
                        context.application.create_task(
                            delete_after(context.bot, chat.id, fr.message_id, sec)
                        )
                    # اگر sec==0 باشد در گروه هم حذف نکن؛ در PV هم هرگز حذف نکن
    except Exception:
        pass


# هندلر پیام‌های متنی معمولی (چت خصوصی یا گروه با منشن/ریپلای)
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user_from_update(update)
    chat = update.effective_chat
    
    # فاز ۲: اطمینان از اینکه رکورد تنظیمات گروه در DB ساخته شده است (فقط برای گروه‌ها)
    if chat and getattr(chat, "id", None) and chat.id < 0:
        try:
            ensure_chat_defaults(chat.id)
        except Exception:
            # در صورت اختلال موقت DB، منطق اصلی پیام قطع نشود
            pass
        
    u = update.effective_user
    msg = update.effective_message
    if not msg or msg.text is None:
        return
    text = msg.text.strip()
    if not text:
        return
    
    
    # --- محافظ پنل: اگر در حال ForceReply پنل هستیم، این پیام نباید به AI برسد ---
    if chat and chat.type == 'private':
        try:
            # اگر state انتظار پنل فعاله، همین‌جا مصرف و خارج شو
            if context.user_data.get(PANEL_AWAIT_KEY):
                return
            # یا اگر این پیام ریپلای به پرامپت ForceReply پنل است (متن پرامپت با «✎ مقدار جدید …» شروع می‌شود)
            rm = getattr(msg, "reply_to_message", None)
            if rm and (rm.text or "").startswith("✎ مقدار جدید"):
                return
        except Exception:
            pass
    
    
    # پاک‌سازی کیبوردهای قدیمی
    if text in ("🧹 پاک کردن تاریخچه", "📥 خروجی تاریخچه"):
        await safe_reply_text(update, "رابط کاربری به‌روزرسانی شد. ✅", reply_markup=ReplyKeyboardRemove())
        set_chat_ui_ver(chat.id, UI_SCHEMA_VERSION)
        return

    # --- PV route (explicit & DB-first) ---
    if chat.type == 'private':
        # اگر PV برای این کاربر مجاز نیست → پیام راهنما
        if not is_dm_allowed(u.id):
            txt = await build_pv_deny_text_links(context.bot)
            await safe_reply_text(update, txt, parse_mode=C.ParseMode.HTML)
            return

        # در PV نیازی به مودِ mention/reply/command نیست؛ متن عادی یعنی پرسش
        q = (msg.text or "").strip()
        if not q:
            return

        # Session ID پایدار برای PV (هر کاربر یک سشن)
        sid = f"pv_{u.id}"

        # حلقه‌ی نمایش «در حال تایپ...» تا آماده‌شدن پاسخ
        stop_event = asyncio.Event()
        typing_task = context.application.create_task(
            _typing_loop(context.bot, chat.id, ChatAction.TYPING, stop_event)
        )
        try:
            # پردازش LLM در ترد جدا تا event loop قفل نشود
            reply_text, _src = await asyncio.to_thread(call_flowise, q, sid, chat.id)
            if not reply_text:
                reply_text = "متوجه نشدم، یه‌بار دیگه بپرس لطفاً 🙂"
            await safe_reply_text(update, reply_text)
        except Exception as e:
            # خطای امن و مختصر (لاگ کامل در سرور ثبت می‌شود)
            await safe_reply_text(update, f"❌ خطا در پاسخ: {type(e).__name__}")
        finally:
            # توقف امن حلقه تایپینگ
            stop_event.set()
            try:
                await typing_task
            except Exception:
                pass
        return


    # از اینجا به بعد: فقط برای گروه‌ها
    is_group = chat.type in ("group", "supergroup")

    # تنظیمات چت
    feature_on = _chat_feature_on()
    mode = (chat_cfg_get(chat.id, "chat_ai_mode") or CHAT_AI_DEFAULT_MODE).strip().lower()
    is_enabled = chat_ai_is_enabled(chat.id)


    # هویت بات
    me = context.application.bot_data.get("me")
    if not me:
        me = await context.bot.get_me()
        context.application.bot_data["me"] = me
    bot_user = me

    rm = getattr(msg, "reply_to_message", None)
    # تشخیص امن: یا همان پیام ForceReply‌ای که خودمان فرستادیم، یا دست‌کم متنِ پرامپت ما
    await_id = context.chat_data.get("await_ask_msg_id")
    is_reply_to_pending_ask = bool(
        rm
        and getattr(rm, "from_user", None) and rm.from_user.id == bot_user.id
        and (
            (await_id and getattr(rm, "message_id", None) == await_id) or
            ((rm.text or "").startswith("سوالت رو همینجا بنویس"))
        )
    )


    # خطاب بودن به بات
    is_reply_to_bot = bool(rm and getattr(rm, "from_user", None) and rm.from_user.id == bot_user.id)
    addressed = is_reply_to_bot or is_addressed_to_bot(update, bot_user.username or "", bot_user.id)

    
    # OFF: فقط اگر صراحتاً خطاب شده‌ایم (mention/reply) هشدار بده؛ در غیر این صورت سکوت
    if not feature_on or not is_enabled:
        if addressed and _should_notify_chat_off(chat.id):
            m, mid = build_sender_html_from_update(update)
            wm = await safe_reply_text(update, f"{t('chat.off.notice', chat_id=chat.id)}\nخطاب به: {m} | ID: {mid}", parse_mode=ParseMode.HTML)
            try:
                sec = chat_ai_autoclean_sec(chat.id)
                if sec and sec > 0 and wm:
                    # حذف پیام راهنمای بات
                    context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                    # --- NEW: حذف پیام کاربر محرک (فقط در گروه) ---
                    if chat.type in ("group", "supergroup"):
                        context.application.create_task(delete_after(context.bot, chat.id, update.effective_message.message_id, sec))
            except Exception:
                pass
        return


    
    # اگر ریپلای به پرامپت ForceReply خودِ /ask است، بگذار ask_reply رسیدگی کند
    if is_reply_to_pending_ask:
        return

    # پاک‌کردن منشن از متن
    if bot_user.username:
        text = text.replace(f"@{bot_user.username}", "").strip()

    # محدودیت‌ها (min_gap و …)
    if not (await _chat_ai_should_answer(update, context, bot_user.username or "", bot_user.id)):
        # اگر admins_only روشن است و کاربرِ غیرادمین ما را خطاب کرده، پیام «فقط ادمین‌ها…» بده
        if is_group:
            admins_only = (chat_cfg_get(chat.id, "chat_ai_admins_only") or "off").strip().lower() in ("on","1","true","yes")
            if admins_only and addressed:
                # ادمین ناشناس یا ادمین واقعی؟
                is_anon_admin = (
                    (u and int(getattr(u, "id", 0)) == int(TG_ANON)) or
                    (getattr(update.effective_message, "sender_chat", None) is not None and update.effective_message.sender_chat.id == chat.id)
                )
                is_grp_admin = False
                try:
                    from shared_utils import is_user_admin_of_group
                    is_grp_admin = await is_user_admin_of_group(context.bot, u.id if u else 0, chat.id)
                except Exception:
                    is_grp_admin = False
    
                if not (is_grp_admin or is_anon_admin):
                    if _should_notify_mode_hint(chat.id):  # ضداسپم
                        m, mid = build_sender_html_from_update(update)
                        wm = await safe_reply_text(
                            update,
                            f"⛔ فقط ادمین‌های این گروه پاسخ می‌گیرند.\nخطاب به: {m} | ID: <code>{mid}</code>",
                            parse_mode=ParseMode.HTML,
                        )
                        try:
                            sec = chat_ai_autoclean_sec(chat.id)
                            if sec and sec > 0 and wm:
                                context.application.create_task(delete_after(context.bot, chat.id, wm.message_id, sec))
                                if chat.type in ("group", "supergroup"):
                                    context.application.create_task(delete_after(context.bot, chat.id, update.effective_message.message_id, sec))
                        except Exception:
                            pass
                    return
        return


    # پاسخ مدل
    log.info(f"Received message: '{text}' from chat: {chat.id}")
    sid = get_or_rotate_session(chat.id)

    stop_event = asyncio.Event()
    typing_task = context.application.create_task(
        _typing_loop(context.bot, chat.id, ChatAction.TYPING, stop_event)
    )
    try:
        flow_sid = sid if chat.type == 'private' else f"{sid}_u{u.id}"
        reply_text, src_count = await asyncio.to_thread(call_flowise, text, flow_sid, chat.id)
        if (src_count == 0) or is_unknown_reply(reply_text):
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
    await safe_reply_text(update, reply_text, reply_markup=feedback_keyboard(sid))

    # پاک‌سازی ForceReply قدیمی — فقط در گروه‌ها و فقط اگر autoclean>0
    try:
        if chat and chat.type in ("group", "supergroup"):
            fr = update.message.reply_to_message if update.message else None
            if fr and fr.from_user and fr.from_user.id == bot_user.id:
                if (fr.text or "").startswith("سوالت رو همینجا بنویس"):
                    sec = chat_ai_autoclean_sec(chat.id)
                    if sec and sec > 0:
                        context.application.create_task(
                            delete_after(context.bot, chat.id, fr.message_id, sec)
                        )
    except Exception:
        pass


# کال‌بک هندلر دکمه‌های فیدبک 👍/👎
async def on_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if not cq or not cq.data:
        return
    try:
        action, feedback, session_id = cq.data.split(":", 2)
    except ValueError:
        return await cq.answer("داده نامعتبر.", show_alert=False)
    if action != "fb" or feedback not in ("like", "dislike"):
        return await cq.answer("نامعتبر.", show_alert=False)
    chat = cq.message.chat
    chat_id = chat.id
    user_id = cq.from_user.id
    bot_message_id = cq.message.message_id
    # در چت خصوصی: فقط یک بازخورد کلی برای هر پیام (از هر کسی)
    if chat.type == 'private':
        if has_any_feedback_for_message(chat_id, bot_message_id):
            return await cq.answer("برای این پاسخ قبلاً یک بازخورد ثبت شده است.", show_alert=False)
        created = save_feedback(chat_id, user_id, session_id, bot_message_id, feedback)
        if created:
            # در خصوصی، به محض رأی دادن، دکمه‌ها را حذف کن
            try:
                await cq.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return await cq.answer("بازخوردت ثبت شد. ممنون 🙏", show_alert=False)
        else:
            return await cq.answer("برای این پاسخ قبلاً بازخورد ثبت شده است.", show_alert=False)
    # در گروه/سوپرگروه: هر کاربر فقط یک‌بار، ولی افراد مختلف می‌توانند رأی دهند
    created = save_feedback(chat_id, user_id, session_id, bot_message_id, feedback)
    if created:
        # (اختیاری) نمایش تعداد رأی‌ها روی دکمه‌ها
        try:
            likes, dislikes = count_feedback(chat_id, bot_message_id)
            await cq.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"👍 {likes}", callback_data=f"fb:like:{session_id}"),
                    InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"fb:dislike:{session_id}")
                ]])
            )
        except Exception:
            # اگر نتوانست ویرایش کند (مثلاً پیام قدیمی بود)، مشکلی نیست
            pass
        return await cq.answer("ثبت شد ✅", show_alert=False)
    else:
        return await cq.answer("تو قبلاً برای این پیام رأی داده‌ای.", show_alert=False)

# کال‌بک هندلر دکمه‌های مربوط به سؤالات بی‌پاسخ (گزارش برای آموزش)
async def on_unknown_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if not cq or not cq.data:
        return
    data = cq.data
    # فقط یک نوع دکمه تعریف شده: گزارش سؤال بی‌پاسخ
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
        return  # بعد از هندل کردن دکمه، ادامه نده
    # اگر نوع دیگری از دکمه بود (در حال حاضر نداریم)
    await cq.answer("دکمه معتبر نیست.", show_alert=False)



# --- REPLACE: health command handler (final) ---
@log_exceptions
async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t0 = perf_counter()

    # 1) DB ping
    try:
        t = perf_counter()
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        db_ms = int((perf_counter() - t) * 1000)
        db_line = f"پایگاه\u200cداده: ✅ {db_ms}ms"
    except Exception as e:
        db_line = f"پایگاه\u200cداده: ❌ {type(e).__name__}: {e}"

    # 2) Flowise ping (context-aware: PV/group-specific)
    try:
        from shared_utils import get_config, chat_cfg_get
    
        base = getenv("FLOWISE_BASE_URL", "").rstrip("/")  # جزو تنظیمات استقرار؛ ENV بماند
        api_key = getenv("FLOWISE_API_KEY")
        chat = update.effective_chat
    
        # --- Resolve chatflow_id by context (DB-first) ---
        cfid = None
        if chat and getattr(chat, "id", None):
            if chat.id > 0:
                # PV (چت خصوصی): اول از DB، بعد ENV
                cfid = (get_config("pv_chatflow_id") or getenv("PV_CHATFLOW_ID"))
            else:
                # Group/Supergroup: تنظیمِ اختصاصی همان گروه
                cfid = (
                    chat_cfg_get(chat.id, "chat_ai_chatflow_id")
                    or chat_cfg_get(chat.id, "chatflow_id")
                )
    
        # Fallback نهایی (سراسری)
        if not cfid:
            cfid = (
                get_config("chat_ai_default_chatflow_id")
                or getenv("MULTITENANT_CHATFLOW_ID")
                or getenv("CHATFLOW_ID")
            )
    
        if not base or not cfid:
            raise RuntimeError("Flowise base/chatflow در DB/ENV مقداردهی نشده است")
    
        # namespace ساده برای سازگاری با چت‌فلوهای چندسازمانی
        ns = (
            f"grp:{chat.id}" if (chat and getattr(chat, "id", None) and chat.id < 0)
            else (f"pv:{chat.id}" if (chat and getattr(chat, "id", None)) else "pv")
        )
    
        ok, fl_ms, fl_err = await asyncio.to_thread(
            ping_flowise, base, cfid, api_key, 8, {"namespace": ns}
        )
        flow_line = f"Flowise: {'✅' if ok else '❌'} {fl_ms}ms — cfid={cfid}" + ("" if ok else f" — {fl_err}")
    except Exception as e:
        flow_line = f"Flowise: ❌ {type(e).__name__}: {e}"


    # 3) JobQueue count (PTB یا APScheduler)
    jq = context.application.job_queue
    try:
        try:
            jobs_count = len(jq.jobs())  # PTB v20+
        except Exception:
            jobs_count = len(jq.scheduler.get_jobs())  # fallback to APScheduler
        jq_line = f"JobQueue: {jobs_count} کار زمان\u200cبندی\u200cشده"
    except Exception as e:
        jq_line = f"JobQueue: ❌ {type(e).__name__}: {e}"

    total_ms = int((perf_counter() - t0) * 1000)
    text = "🩺 وضعیت ربات\n" + "\n".join([db_line, flow_line, jq_line]) + f"\n⏱ کل: {total_ms}ms"

    await update.effective_message.reply_text(text, parse_mode=C.ParseMode.HTML)
# --- END REPLACE ---