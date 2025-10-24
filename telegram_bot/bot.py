import asyncio
import os, logging
from flowise_client import ping_flowise
from datetime import timedelta
os.makedirs("/app/logs", exist_ok=True)

from logging_setup import setup_logging, update_log_context
setup_logging()
log = logging.getLogger(__name__)


# ایمپورت ماژول‌های جداشده و سایر وابستگی‌ها
from shared_utils import (
    BOT_TOKEN, FLOWISE_BASE_URL, FLOWISE_API_KEY,
    is_admin, db_conn, wait_for_db_ready, ensure_tables,
    get_config, set_config,  # ← اضافه شد: خواندن/نوشتن تنظیمات سراسری در DB
    MET_FLOWISE_UP, MET_BOT_ERRORS
)  # noqa: E402

from admin_commands import loglevel_cmd, lognoise_cmd, audit_cmd

from admin_commands import dm_cmd, allow_cmd, block_cmd, users_cmd, unknowns_cmd, chat_cmd, fixcommands_cmd, lang_cmd, on_lang_set

from user_commands import start, help_cmd, whoami, clear_history, export_history, ask_cmd, ask_reply, on_message, on_feedback, on_unknown_buttons, manage  # noqa: E402

# --- اضافه برای پنل مدیریتی (PV) ---
from user_commands import panel_open, panel_on_cb, panel_on_force_reply, health, home_on_cb  # noqa: E402

from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters  # noqa: E402
from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat, BotCommandScopeChatAdministrators,
    Update,
)
from ads_guard import AdsGuard  # noqa: E402
from ads_commands import register_ads_commands
from admin_commands import register_superadmin_tools

from tokens.handlers import register_token_handlers
from tokens.jobs import schedule_weekly_grants


logger = logging.getLogger(__name__)

async def _flowise_warmup_job(context):
    """
    پینگ ادواری به Flowise برای گرم نگه‌داشتن chatflow
    - وقتی ID خالی باشد با لاگ واضح «skipped» رد می‌شود
    - در خطای 412 پیام راهنما می‌دهد
    - متریک flowise_up را ۱/۰ می‌کند
    """
    try:
        from shared_utils import get_config  # DB-first

        base = (os.getenv("FLOWISE_BASE_URL") or "").rstrip("/")  # جزو تنظیمات استقرار؛ ENV بماند
        # DB-first برای chatflow_id:
        cfid = (
            get_config("warmup_chatflow_id")
            or get_config("chat_ai_default_chatflow_id")
            or os.getenv("MULTITENANT_CHATFLOW_ID")
            or os.getenv("CHATFLOW_ID")
            or ""
        )
        api_key = os.getenv("FLOWISE_API_KEY")  # کلید دسترسی سرویس؛ ENV بماند


        if not base or not cfid:
            MET_FLOWISE_UP.set(0)  # بدون پیکربندی معتبر، فعلاً down بدان
            logger.debug(
                "Flowise warmup skipped: base or chatflow id is empty [base=%r, cfid=%r]",
                base, cfid
            )

            return

        ok, ms, err = await asyncio.to_thread(ping_flowise, base, cfid, api_key, 8)
        if ok:
            MET_FLOWISE_UP.set(1)
            logger.debug("Flowise warmed in %sms [base=%s chatflow=%s]", ms, base, cfid)
        else:
            MET_FLOWISE_UP.set(0)
            hint = ""
            if "id not provided" in str(err).lower():
                hint = " (راهنما: یکی از WARMUP_CHATFLOW_ID یا MULTITENANT_CHATFLOW_ID یا CHATFLOW_ID را ست کنید)"
            logger.warning("Flowise warmup failed: %s (in %sms) [base=%s chatflow=%s]%s",
                           err, ms, base, cfid, hint)
    except Exception:
        MET_FLOWISE_UP.set(0)
        logger.exception("Warmup job error")



# --- Seed ENV defaults into DB once (without overwriting admin-changed values) ---
def _seed_env_defaults_to_db():
    """
    ایده:
      - فقط یک بار در بوت: اگر کلید در bot_config نبود، مقدارش را از ENV (یا پیش‌فرض ثابت) در DB ذخیره کن.
      - اگر بود، دست به مقدار DB نزن (تغییرات ادمین‌ها حفظ می‌شود).
    توجه: تنظیمات per-chat (chat_config) را اینجا دستکاری نمی‌کنیم؛
          آن‌ها را ادمین‌ها از داخل پنل برای هر گروه ست می‌کنند و خودش پایدار است.
    """
    # (env_var, bot_config_key, default_if_env_missing)
    # NOTE:
    #   - These pairs are used ONCE at startup to seed DB defaults (DB-first).
    #   - If a key already exists in bot_config, we DO NOT overwrite it (admin changes persist).
    #   - Keep env var names aligned with /mnt/data/env. SUPER_ADMIN_IDS accepts CSV or JSON list.
    #   - After seeding, DB is the single source of truth. ENV is only for the first boot (12factor-friendly).

    PAIRS = [
        # --- Global Ads defaults (fallback when per-chat setting is missing) ---
        ("ADS_FEATURE",                         "ads_feature",                         "off"),
        ("ADS_ACTION",                          "ads_action",                          "none"),   # none|warn|delete
        ("ADS_THRESHOLD",                       "ads_threshold",                       "0.78"),
        ("ADS_MAX_FEWSHOTS",                    "ads_max_fewshots",                    "10"),
        ("ADS_MIN_GAP_SEC",                     "ads_min_gap_sec",                     "2"),
        ("ADS_AUTOCLEAN_SEC",                   "ads_autoclean_sec",                   "0"),
        ("ADS_CHATFLOW_ID",                     "ads_chatflow_id",                     ""),

        # Reply-exempt defaults
        ("ADS_REPLY_EXEMPT",                    "ads_reply_exempt",                    "on"),
        ("ADS_REPLY_EXEMPT_MAXLEN",             "ads_reply_exempt_maxlen",             "160"),
        ("ADS_REPLY_EXEMPT_ALLOW_CONTACT",      "ads_reply_exempt_allow_contact",      "on"),
        ("ADS_REPLY_EXEMPT_CONTACT_MAXLEN",     "ads_reply_exempt_contact_maxlen",     "360"),

        # --- Global Chat-AI defaults (when per-group is not set yet) ---
        ("CHAT_AI_DEFAULT_ENABLED",             "chat_ai_default_enabled",             "off"),
        # mention|reply|command|all
        ("CHAT_AI_DEFAULT_MODE",                "chat_ai_default_mode",                "mention"),
        ("CHAT_AI_DEFAULT_MIN_GAP_SEC",         "chat_ai_default_min_gap_sec",         "2"),
        ("CHAT_AI_AUTOCLEAN_SEC",               "chat_ai_default_autoclean_sec",       "0"),

        # --- PV invite & picker defaults (DB-first seed) ---
        ("PV_GROUP_LIST_LIMIT",                 "pv_group_list_limit",                 "12"),
        ("PV_INVITE_LINKS",                     "pv_invite_links",                     ""),
        ("PV_INVITE_EXPIRE_HOURS",              "pv_invite_expire_hours",              "12"),
        ("PV_INVITE_MEMBER_LIMIT",              "pv_invite_member_limit",              "0"),

        # If you have a public Chatflow, keep a global fallback here
        ("MULTITENANT_CHATFLOW_ID",             "chat_ai_default_chatflow_id",         ""),
        ("BOT_DEFAULT_LANG",                    "default_lang",                        "fa"),
        
        # --- Flowise warmup (DB-first seed) ---
        ("WARMUP_CHATFLOW_ID",                   "warmup_chatflow_id",                 ""),
        ("FLOWISE_WARMUP_INTERVAL_SEC",         "flowise_warmup_interval_sec",        "420"),

        # --- Global policy & Super Admin (RBAC/Least-Privilege) ---
        ("DM_GLOBAL",                           "dm_global",                           "on"),
        ("DM_POLICY",                           "dm_policy",                           "block_all"),
        # e.g. "5620665435" or '["5620665435","123456789"]' (seed-only; then DB-first)
        ("SUPER_ADMIN_IDS",                     "super_admin_ids",                     ""),

        # --- Logging (optional but recommended to seed for consistency) ---
        ("LOG_LEVEL",                           "log_level",                           "INFO"),
        # ("LOG_FORMAT",                        "log_format",                          "console"),
        # ("ENABLE_FILE_LOG",                   "enable_file_log",                     ""),
        # ("LOG_NOISY_LEVEL",                   "log_noisy_level",                     "WARNING"),
    ]

    # اجرای امن: اگر DB آماده نباشد، خطا ایجاد نکند (startup قبلاً wait_for_db_ready را صدا می‌زند)
    try:
        for env_key, cfg_key, default in PAIRS:
            v = os.getenv(env_key, default)
            try:
                # اگر قبلاً در DB نبود، مقدار ENV/پیشفرض را بنویس
                if get_config(cfg_key) is None:
                    set_config(cfg_key, str(v))
            except Exception as e:
                # در صورت خطا، ادامه بده (no regression)
                log.warning(f"seed '{cfg_key}' failed: {e}")
    except Exception as e:
        log.warning(f"_seed_env_defaults_to_db unexpected error: {e}")



# تابع غیرفعال‌سازی Webhook و راه‌اندازی اولیه (Startup)
async def _on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (if existed).")
    except Exception as e:
        log.warning(f"delete_webhook failed (ignored): {e}")

    await wait_for_db_ready(max_wait_sec=90)
    ensure_tables()
    _seed_env_defaults_to_db()   # ← پیش‌فرض‌های ENV فقط اگر در DB نبودند، seed می‌شوند
    await _set_menu_commands(app.bot)

    # ساخت جداول اختصاصی AdsGuard (در صورت استفاده)
    try:
        if app.bot_data.get("ads_guard"):
            await asyncio.get_running_loop().run_in_executor(
                None, app.bot_data["ads_guard"].ensure_tables
            )
    except Exception:
        pass

    # کش کردن اطلاعات بات برای استفاده در سایر بخش‌ها (افزایش کارایی)
    me = await app.bot.get_me()
    app.bot_data["me"] = me
    log.info(f"Cached bot info: @{me.username} (id={me.id})")
    
    # --- Warmup Flowise periodically (keeps chatflow hot) ---
    from shared_utils import get_config
    try:
        _warmup_sec = int(get_config("flowise_warmup_interval_sec") or os.getenv("FLOWISE_WARMUP_INTERVAL_SEC", "420"))
    except Exception:
        _warmup_sec = 420  # 7 دقیقه
    
    app.job_queue.run_repeating(
        _flowise_warmup_job,
        interval=timedelta(seconds=_warmup_sec),
        first=timedelta(seconds=20),
        name="flowise-warmup",
    )


# تنظیم منوی دستورات برای حالت خصوصی و گروه
from telegram import BotCommand
from telegram import (
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeChat,            # برای PV اختصاصی سوپرادمین
    BotCommandScopeChatMember,      # برای منوی اختصاصی یک کاربر در یک چت
)

# ---[ کمک‌تابع: شناسه‌های سوپرادمین ]---
def _get_super_admin_ids() -> list[int]:
    """
    شناسه‌های سوپرادمین را از DB بخوان (DB-first).
    اگر هم‌اکنون تابع آماده داری (مثلاً shared_utils.get_super_admin_ids)، همان را صدا بزن.
    این نسخه صرفاً یک placeholder است.
    """
    from shared_utils import get_super_admin_ids  # ترجیحاً ماژول خودت
    return list(map(int, get_super_admin_ids()))

async def _set_menu_commands(bot):
    """
    تعریف منو بر اساس اسکوپ:
      1) PV (همه کاربران)
      2) همهٔ گروه‌ها (کاربران عادی)
      3) ادمین‌های گروه
      4) PV اختصاصی سوپرادمین‌ها (SA-only, سراسری)
    برای زبان فارسی نیز نسخهٔ language_code='fa' ست می‌شود.
    """

    # 1) منوی PV (چت خصوصی) برای همه
    pv_cmds = [
        BotCommand("start",   "شروع/ریست"),
        BotCommand("help",    "راهنما"),
        BotCommand("panel",   "پنل مدیریت گروه‌ها"),
        BotCommand("ask",     "پرسش"),
        BotCommand("whoami",  "شناسه شما"),
        BotCommand("health",  "وضعیت سرویس‌ها"),
    ]

    # 2) منوی همهٔ گروه‌ها (کاربران عادی)
    group_cmds = [
        BotCommand("start",   "شروع/ریست"),
        BotCommand("help",    "راهنما"),
        BotCommand("ask",     "پرسش"),
        BotCommand("whoami",  "شناسه شما"),
        BotCommand("health",  "وضعیت سرویس‌ها"),
        # عمداً /chat و /ads در این اسکوپ نیست (ادمین‌محورند)
    ]

    # 3) منوی ادمین‌های گروه
    admin_cmds = [
        BotCommand("manage",      "مدیریت گروه در PV"),
        BotCommand("ads",         "مدیریت گارد تبلیغات"),
        BotCommand("chat",        "روشن/خاموش چت (ادمین)"),
        BotCommand("lang",        "انتخاب زبان گروه"),
        BotCommand("unknowns",    "۲۰ سؤال بی‌پاسخ اخیر"),
        BotCommand("users",       "۵۰ کاربر اخیر + وضعیت DM"),
        BotCommand("fixcommands", "بازتنظیم منو همین چت"),
    ]

    # ست منوها برای اسکوپ‌های عمومی
    await bot.set_my_commands(pv_cmds,    scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(group_cmds, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeAllChatAdministrators())

    # نسخهٔ فارسی (language_code='fa')
    await bot.set_my_commands(pv_cmds,    scope=BotCommandScopeAllPrivateChats(),       language_code="fa")
    await bot.set_my_commands(group_cmds, scope=BotCommandScopeAllGroupChats(),         language_code="fa")
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeAllChatAdministrators(), language_code="fa")

    # 4) --- منوی PV اختصاصی سوپرادمین‌ها ---
    # در چت خصوصی با هر SA، دستورات کامل SA را نشان بده (همهٔ ابزارهای مدیریتی کنار ابزارهای عمومی)
    sa_cmds_pv = [
        BotCommand("start",      "شروع/ریست"),
        BotCommand("help",       "راهنما"),
        BotCommand("panel",      "پنل مدیریت گروه‌ها"),
        BotCommand("ask",        "پرسش"),
        BotCommand("export",     "خروجی تاریخچه"),
        BotCommand("whoami",     "شناسه شما"),
        BotCommand("health",     "وضعیت سرویس‌ها"),
        # ابزارهای مدیریتی/سراسری که SA بیشتر استفاده می‌کند:
        BotCommand("fixcommands","بازتنظیم منوها"),
        BotCommand("audit",      "ممیزی تغییرات"),
        BotCommand("loglevel",   "سطح لاگ"),
        BotCommand("lognoise",   "فیلتر لاگ کتابخانه‌ها"),
        BotCommand("dm",         "سیاست DM (on/off/status)"),
        BotCommand("chat",       "چت هوش‌مصنوعی (on/off/status)"),
    ]

    try:
        sa_ids = _get_super_admin_ids()
    except Exception as e:
        sa_ids = []
        # اگر لود SA شکست خورد، بی‌سروصدا فقط اسکوپ‌های عمومی اعمال می‌مانند.
        # لاگ‌کردن خطا به انتخاب شما
        # logger.exception("Failed to load super admin IDs: %s", e)

    for sa in sa_ids:
        # PV سوپرادمین خاص
        await bot.set_my_commands(sa_cmds_pv, scope=BotCommandScopeChat(chat_id=sa))
        await bot.set_my_commands(sa_cmds_pv, scope=BotCommandScopeChat(chat_id=sa), language_code="fa")

# تابع اصلی اجرای بات
def run():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # --- Sentry (اختیاری) -----------------------------------------------------
    _sentry_dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if _sentry_dsn:
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=_sentry_dsn,
                environment=os.getenv("SENTRY_ENV", "production"),
                traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05") or "0.05"),
            )
            log.info("Sentry initialized \u2705")
        except Exception:
            log.exception("Sentry init failed")
    # --------------------------------------------------------------------------

    
    # این هندلر فقط کانتکست لاگ را پر می‌کند و ادامه می‌دهد
    async def _set_log_ctx(update: Update, context: ContextTypes.DEFAULT_TYPE):
        update_log_context(update, op=None)
    
    # باید اول از همه اجرا شود تا بقیهٔ لاگ‌ها کانتکست داشته باشند
    app.add_handler(MessageHandler(filters.ALL, _set_log_ctx), group=-9999)
    
    # -----------------------------
    # Prometheus /metrics (اختیاری)
    # -----------------------------
    # براساس ENV روشن/خاموش می‌شود (DB-first برای بیزنس، ولی برای observability از env استفاده می‌کنیم)
    _m_enabled = str(os.getenv("METRICS_ENABLED", "0")).strip().lower() in ("1", "on", "true", "yes")
    if _m_enabled:
        try:
            from prometheus_client import start_http_server, Counter  # import محلی تا وابستگی فقط در صورت نیاز لود شود
            _m_port = int(os.getenv("METRICS_PORT", "9308") or "9308")
            _m_addr = os.getenv("METRICS_ADDR", "0.0.0.0")  # اگر لازم شد با 127.0.0.1 محدود کن (داخل کانتینر)
            # Counter ساده برای شمارش آپدیت‌ها (برچسب نوع آپدیت)
            MET_UPDATES = Counter("tg_updates_total", "Telegram updates received", ["type"])
            
            # هندلر سبک برای افزایش کانتر
            async def _metrics_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
                try:
                    t = type(update).__name__
                    MET_UPDATES.labels(t).inc()
                except Exception:
                    pass
            
            # قبل از هرچیز و فقط اگر metrics روشن است
            app.add_handler(MessageHandler(filters.ALL, _metrics_update), group=-9998)
            
            # راه‌اندازی HTTP exporter (daemon thread; non-blocking)
            start_http_server(_m_port, addr=_m_addr)
            log.info(f"Prometheus /metrics started on { _m_addr }:{ _m_port } ✅")
        except Exception as e:
            log.exception("Prometheus metrics init failed", extra={"err": type(e).__name__})
    
    # راه‌اندازی AdsGuard (ماژول کنترل تبلیغات)
    ads = AdsGuard(
        get_db_conn=db_conn,
        is_admin_fn=is_admin,
        flowise_base_url=FLOWISE_BASE_URL,
        flowise_api_key=FLOWISE_API_KEY,
    )
    app.bot_data["ads_guard"] = ads
    register_ads_commands(app, ads)
    register_superadmin_tools(app)
    register_token_handlers(app)
    schedule_weekly_grants(app)
    
    # ثبت هندلرهای دستورات کاربری
    app.add_handler(CommandHandler("start", start))
    # --- پنل مدیریتی در پی‌وی ---
    app.add_handler(CommandHandler("panel", panel_open))
    app.add_handler(CallbackQueryHandler(panel_on_cb, pattern=r"^v1\|"))
    # --- کال‌بک‌های خانه‌ی آغازین (h|...) ---
    app.add_handler(CallbackQueryHandler(home_on_cb, pattern=r"^h\|"))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("manage", manage))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("export", export_history))
    app.add_handler(CommandHandler("ask", ask_cmd))
    app.add_handler(CommandHandler("chat", chat_cmd))  # مدیریت روشن/خاموش چت هوش‌مصنوعی (فقط ادمین)
    app.add_handler(CommandHandler("lang", lang_cmd))
    # ثبت هندلرهای دستورات ادمین
    app.add_handler(CommandHandler("dm", dm_cmd))
    app.add_handler(CommandHandler("allow", allow_cmd))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("unknowns", unknowns_cmd))
    app.add_handler(CommandHandler("fixcommands", fixcommands_cmd))
    # callback برای انتخاب زبان
    app.add_handler(CallbackQueryHandler(on_lang_set, pattern=r"^lang:set:(fa|en|ar|tr|ru)$"))
    # هندلرهای کال‌بک برای دکمه‌های فیدبک و گزارش سؤال نامعلوم
    app.add_handler(CallbackQueryHandler(on_feedback, pattern=r"^fb:"))
    app.add_handler(CallbackQueryHandler(on_unknown_buttons, pattern=r"^kb:report:\d+$"))
    
    app.add_handler(CallbackQueryHandler(ads.on_warn_info,    pattern=r"^adsw:info$"))
    app.add_handler(CallbackQueryHandler(ads.on_warn_mute,    pattern=r"^adsw:mute:\d+:\d+$"))
    app.add_handler(CallbackQueryHandler(ads.on_warn_buttons, pattern=r"^adsw:guide:\d+$"))
    
    # --- ForceReply ورودی‌های پنل (cfid و ...): قبل از on_message تا زودتر مصرف شود ---
    app.add_handler(
        MessageHandler(filters.TEXT & filters.REPLY & filters.ChatType.PRIVATE, panel_on_force_reply),
        group=0
    )
    
    # پاسخ به ForceReply فقط با این هندلر انجام شود
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY, ask_reply), group=1)

    # هندلر پیام‌های متنی (غیردستور) - اصلاح‌شده
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,  # ← اجازه بده ریپلای‌ها هم به on_message برسند
            on_message,
            block=False
        ),
        group=2
    )


    # هندلر خطاهای عمومی
    app.add_error_handler(on_error)
    # تنظیمات اولیه پس از بوت
    app.post_init = _on_startup

    log.info("Bot is starting to poll...")
    
    allowed = [
        Update.MESSAGE,
        Update.EDITED_MESSAGE,
        Update.CALLBACK_QUERY,
        Update.CHAT_MEMBER,  # برای رویدادهای مدیریتی گروه
    ]
    app.run_polling(
        poll_interval=0.5,
        timeout=50,
        drop_pending_updates=True,
        allowed_updates=allowed,
    )



# هندلر عمومی خطاها
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    # لاگ ساخت‌یافته
    log.exception("Unhandled error", extra={"err": type(context.error).__name__})
    try:
        MET_BOT_ERRORS.inc()
    except Exception:
        pass
    # گزارش اختیاری به Sentry (اگر DSN تنظیم باشد)
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            try:
                # کانتکست مفید برای عیب‌یابی
                chat = getattr(update, "effective_chat", None)
                user = getattr(update, "effective_user", None)
                scope.set_tag("chat_type", getattr(chat, "type", None))
                scope.set_tag("chat_id", getattr(chat, "id", None))
                scope.set_user({"id": getattr(user, "id", None), "username": getattr(user, "username", None)})
            except Exception:
                pass
            sentry_sdk.capture_exception(context.error)
    except Exception:
        pass


if __name__ == "__main__":
    run()
