# ads_commands.py
# -----------------------------------------------------------------------------
# ثبت دستورات مربوط به AdsGuard و فعال‌سازی watchdog
# -----------------------------------------------------------------------------

import asyncio
import re
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram import Message

import html  # برای escape کردن عنوان گروه در HTML
from telegram.constants import ParseMode
from messages_service import t, tn
from shared_utils import check_admin_status, is_superadmin
from shared_utils import resolve_target_chat_id

async def _delete_msg_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    msg_id = data.get("msg_id")
    if chat_id and msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass


# Anonymous admin id (PTB v22+ / v13 fallback)
from shared_utils import TG_ANON



from shared_utils import safe_reply_text, upsert_user_from_update, is_admin, db_conn

def register_ads_commands(app, ads_guard):
    """
    دستورات /ads ... + معادل‌های قدیمی، و ثبت watchdog:
      - watchdog قبل از سایر MessageHandlerها با group=-1 اضافه می‌شود.
      - تغییر در این فایل، منطق چت‌بات/ادمین و فایل‌های دیگر را متاثر نمی‌کند.
    """
    # --- پیش‌فیلتر گروه‌ها برای تشخیص تبلیغ ---
    # فقط آپدیتِ message، نه edited_message و نه کامندها
    # فقط Photo/Video (پیام‌های معمولی - نه ادیت)
    # 1) Media بدون کپشن → هشدار + تایمر + حذف امن (بدون تغییر رفتاری)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (filters.PHOTO | filters.VIDEO)
            & ~filters.COMMAND
            & ~filters.UpdateType.EDITED_MESSAGE,
            ads_guard.watchdog,
        ),
        group=-1,
    )
    
    # 1.1) متن‌های عادی غیرِدستور → بررسی تبلیغ (جدید)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & ~filters.COMMAND
            & ~filters.REPLY
            & ~filters.UpdateType.EDITED_MESSAGE,
            ads_guard.watchdog,
        ),
        group=-1,
    )
    
    # 2) ریپلای متنی برای عکس/ویدیو بی‌کپشن → لغو/بررسی (بدون تغییر)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & filters.REPLY
            & ~filters.UpdateType.EDITED_MESSAGE,
            ads_guard.watchdog,
        ),
        group=-1,
    )
    
    # 3) ادیت کپشن/متن → لغو تایمر (اگر بود) و بررسی مجدد
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.UpdateType.EDITED_MESSAGE,
            ads_guard.on_edited_message,
        ),
        group=-1,
    )



    
    # دکمه‌های اینلاینِ هشدار کپشن
    app.add_handler(
        CallbackQueryHandler(ads_guard.on_warn_buttons, pattern=r"^adsw:guide:"),
        group=-1,
    )

    # دکمهٔ ادمینی سکوت ۱۰۰ساعت
    app.add_handler(
        CallbackQueryHandler(ads_guard.on_warn_mute, pattern=r"^adsw:mute:"),
        group=-1,
    )
    
    
    # دکمهٔ توضیحات (بعد از پذیرش کپشن)
    app.add_handler(CallbackQueryHandler(ads_guard.on_warn_info, pattern=r"^adsw:info$"), group=-1)

    # --- کمکی: تعیین مجاز بودن ادمین بات ---
    def _is_anonymous_group_admin(update: Update) -> bool:
        chat = update.effective_chat
        msg = update.effective_message
        u = update.effective_user
        try:
            return (
                chat and chat.type in ['group', 'supergroup'] and msg and (
                    (getattr(msg, "sender_chat", None) is not None and msg.sender_chat.id == chat.id)
                    or (u and int(u.id) == int(TG_ANON))
                )
            )
        except Exception:
            return False

    async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        u = update.effective_user
        chat = update.effective_chat
        if not u:
            return False

        if chat and chat.type in ['group', 'supergroup']:
            if _is_anonymous_group_admin(update):
                return True
            try:
                from shared_utils import check_admin_status
                ok, _, _ = await check_admin_status(context.bot, u.id, chat.id)
                return ok
            except Exception:
                return False

        # PV → فقط سوپرادمین
        try:
            from shared_utils import is_superadmin
            return is_superadmin(u.id)
        except Exception:
            from shared_utils import is_superadmin
            return is_superadmin(u.id)

        
        
    async def _target_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
        gid = await resolve_target_chat_id(update, context)
        if gid is None:
            await safe_reply_text(update, t("panel.manage.in_pv_hint.short", chat_id=update.effective_chat.id if update.effective_chat else None))
        return gid

    async def _auto_cleanup_pair(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_msg: Message | None):
        """پیام دستور و پیام پاسخ ربات را بعد از تاخیر تنظیمی گروه حذف می‌کند."""
        try:
            chat_id = await _target_chat_id(update, context)
            if not chat_id:
                return
            delay = ads_guard.chat_autoclean_sec(chat_id)
            if delay and delay > 0:
                q = context.application.job_queue
                # حذف پیام ربات
                if bot_msg and getattr(bot_msg, "message_id", None):
                    q.run_once(_delete_msg_job, when=delay, data={"chat_id": chat_id, "msg_id": bot_msg.message_id})
                # حذف خود پیام دستور
                if update.effective_message and getattr(update.effective_message, "message_id", None):
                    q.run_once(_delete_msg_job, when=delay, data={"chat_id": chat_id, "msg_id": update.effective_message.message_id})
        except Exception:
            pass


    # --- هندلر /ads با زیرفرمان‌ها ---
    async def ads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        args = [str(a).strip() for a in (context.args or []) if a is not None]
        sub = args[0].lower() if args else "status"
        
        # گروه هدف (در گروه = همین چت؛ در پی‌وی = گروه فعال ادمین)
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return

        # اتصال زیرفرمان‌های جدید (Stats & Simulate)
        # فقط ادمین‌ها به stats/simulate دسترسی داشته باشند (اختیاری)
        if sub in ("stats", "simulate"):
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))
                await _auto_cleanup_pair(update, context, m)
                return m
            if sub == "stats":
                return await ads_stats_cmd(update, context)
            else:
                return await ads_simulate_cmd(update, context)


        if sub in ("status", "info", "?"):
            feature_status = "ON" if ads_guard.chat_feature_on(chat_id) else "OFF"
            info = (
                f"feature: {feature_status}\n"
                f"action: {ads_guard.chat_action(chat_id)}\n"
                f"threshold: {ads_guard.chat_threshold(chat_id):.2f}\n"
                f"max_fewshots: {ads_guard.chat_max_fewshots(chat_id)}\n"
                f"examples_select: {ads_guard.chat_examples_select_mode(chat_id)}\n"
                f"min_gap_sec: {ads_guard.chat_min_gap_sec(chat_id)}\n"
                f"chatflow_id: {ads_guard.chat_chatflow_id(chat_id) or '-'}\n"
            )
            m = await safe_reply_text(update, info)
            await _auto_cleanup_pair(update, context, m)
            return m

        if sub in ("on", "off"):
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

                await _auto_cleanup_pair(update, context, m)
                return m
            ads_guard.chat_set_config(chat_id, "ads_feature", sub)
            m = await safe_reply_text(update, f"ADS_FEATURE = {sub.upper()}")
            await _auto_cleanup_pair(update, context, m)
            return m
        
        elif sub in ("autoclean", "autodel", "autodelete"):
            return await ads_autoclean_cmd(update, context)
        
        elif sub in ("reply", "reply_exempt"):
            return await ads_reply_cmd(update, context)
        elif sub in ("replylen", "reply_maxlen"):
            return await ads_replylen_cmd(update, context)
        elif sub in ("replycontact", "reply_contact"):
            return await ads_replycontact_cmd(update, context)
        elif sub in ("replycontactlen", "reply_contact_maxlen"):
            return await ads_replycontactlen_cmd(update, context)
        elif sub == "captionlen":
            return await ads_captionlen_cmd(update, context)
        elif sub == "nocap_grace":
            return await ads_nocap_grace_cmd(update, context)
        elif sub in ("allow_forward", "forward"):
            return await ads_allow_forward_cmd(update, context)
        elif sub == "fwd_captionlen":
            return await ads_fwd_captionlen_cmd(update, context)
        elif sub == "fwd_grace":
            return await ads_fwd_grace_cmd(update, context)
        elif sub in ("reply_as_caption", "replyascaption"):
            return await ads_reply_as_caption_cmd(update, context)
        elif sub == "warn_success_action":
            return await ads_warn_success_action_cmd(update, context)
        elif sub in ("warn_success_autodel", "warn_success_autodel_sec"):
            return await ads_warn_success_autodel_cmd(update, context)
        elif sub in ("mute_hours", "mutehours"):
            return await ads_mute_hours_cmd(update, context)


        
        
        if sub == "threshold":
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

                await _auto_cleanup_pair(update, context, m)
                return m
            if len(args) < 2:
                m = await safe_reply_text(update, "استفاده: /ads threshold <0..1>")
                await _auto_cleanup_pair(update, context, m)
                return m
            try:
                v = float(args[1])
                if not (0.0 <= v <= 1.0):
                    raise ValueError()
            except Exception:
                m = await safe_reply_text(update, "عددی بین 0 و 1 بده.")
                await _auto_cleanup_pair(update, context, m)
                return m
            ads_guard.chat_set_config(chat_id, "ads_threshold", str(v))
            m = await safe_reply_text(update, f"threshold = {v}")
            await _auto_cleanup_pair(update, context, m)
            return m

        if sub == "action":
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

                await _auto_cleanup_pair(update, context, m)
                return m
            if len(args) < 2 or args[1].lower() not in ("warn", "delete", "none"):
                m = await safe_reply_text(update, "استفاده: /ads action warn|delete|none")
                await _auto_cleanup_pair(update, context, m)
                return m
            ads_guard.chat_set_config(chat_id, "ads_action", args[1].lower())
            m = await safe_reply_text(update, f"ADS_ACTION = {args[1].lower()}")
            await _auto_cleanup_pair(update, context, m)
            return m

        if sub == "add":
            return await ads_add_cmd(update, context)
        if sub == "notad":
            return await ads_notad_cmd(update, context)

        if sub == "list":
            return await ads_list_cmd(update, context)

        if sub == "chatflow":
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

                await _auto_cleanup_pair(update, context, m)
                return m
            if len(args) < 2:
                m = await safe_reply_text(update, "استفاده: /ads chatflow <CHATFLOW_ID>")
                await _auto_cleanup_pair(update, context, m)
                return m
            ads_guard.chat_set_config(chat_id, "ads_chatflow_id", args[1])
            m = await safe_reply_text(update, "ADS_CHATFLOW_ID set ✅")
            await _auto_cleanup_pair(update, context, m)
            return m

        if sub == "fewshots":
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

                await _auto_cleanup_pair(update, context, m)
                return m
            if len(args) < 2:
                m = await safe_reply_text(update, "استفاده: /ads fewshots <1..50>")
                await _auto_cleanup_pair(update, context, m)
                return m
            try:
                v = int(args[1]); assert 1 <= v <= 50
            except Exception:
                m = await safe_reply_text(update, "یک عدد صحیح بین 1 تا 50 بده.")
                await _auto_cleanup_pair(update, context, m)
                return m
            ads_guard.chat_set_config(chat_id, "ads_max_fewshots", str(v))
            m = await safe_reply_text(update, f"ADS_MAX_FEWSHOTS = {v}")
            await _auto_cleanup_pair(update, context, m)
            return m
        
        if sub in ("balance", "bal"):
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admins"), chat_id=update.effective_chat.id if update.effective_chat else None)
                await _auto_cleanup_pair(update, context, m)
                return m
            if len(args) < 2 or args[1].lower() not in ("on", "off"):
                m = await safe_reply_text(update, "استفاده: /ads balance on|off")
                await _auto_cleanup_pair(update, context, m)
                return m
            mode = "balanced" if args[1].lower() == "on" else "latest"
            ads_guard.chat_set_config(chat_id, "ads_examples_select", mode)
            m = await safe_reply_text(update, f"ads_examples_select = {mode}")
            await _auto_cleanup_pair(update, context, m)
            return m

        
        if sub == "gap":
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

                await _auto_cleanup_pair(update, context, m)
                return m
            if len(args) < 2:
                m = await safe_reply_text(update, "استفاده: /ads gap <sec>")
                await _auto_cleanup_pair(update, context, m)
                return m
            try:
                v = int(args[1]); assert v >= 0
            except Exception:
                m = await safe_reply_text(update, "یک عدد صحیح ۰ یا بیشتر بده.")
                await _auto_cleanup_pair(update, context, m)
                return m
            ads_guard.chat_set_config(chat_id, "ads_min_gap_sec", str(v))
            m = await safe_reply_text(update, f"ADS_MIN_GAP_SEC = {v}")
            await _auto_cleanup_pair(update, context, m)
            return m

        if sub == "wuser":
            return await ads_wuser_cmd(update, context)

        if sub == "wdomain":
            return await ads_wdomain_cmd(update, context)
        
        
        # --- /ads examples ... ---
        if sub == "examples":
            # /ads examples count  → فقط تعداد نمونه‌ها
            # /ads examples clear YES → پاک‌کردن همه نمونه‌ها (با تایید YES)
            op = (args[1].lower() if len(args) >= 2 else "").strip()
            if op == "count":
                try:
                    # count (فقط همین گروه)
                    chat_id = await _target_chat_id(update, context)
                    if not chat_id:
                        return

                    with db_conn() as conn, conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM ads_examples WHERE chat_id = %s;", (chat_id,))
                        cnt = cur.fetchone()[0]
                    
                    txt = tn(
                        "ads.examples.count.one",
                        "ads.examples.count.many",
                        cnt,
                        chat_id=update.effective_chat.id if update.effective_chat else None
                    )

                    m = await safe_reply_text(update, txt)
                    await _auto_cleanup_pair(update, context, m)
                    return m

                except Exception as e:
                    m = await safe_reply_text(update, f"خطا در شمارش: {e}")
                    await _auto_cleanup_pair(update, context, m)
                    return m
                    
            elif op == "stats":
                return await ads_examples_stats_cmd(update, context)

            
            
            elif op == "clear":
                if not await _require_admin(update, context):
                    m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

                    await _auto_cleanup_pair(update, context, m)
                    return m
                # نیاز به تایید صریح دارد
                confirm = (args[2].upper() if len(args) >= 3 else "")
                if confirm != "YES":
                    m = await safe_reply_text(update, t("ads.examples.clear.prompt", chat_id=update.effective_chat.id if update.effective_chat else None))
                    await _auto_cleanup_pair(update, context, m)
                    return m
                try:
                    # clear (فقط همین گروه)
                    chat_id = await _target_chat_id(update, context)
                    if not chat_id:
                        return
                    with db_conn() as conn, conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM ads_examples WHERE chat_id = %s;", (chat_id,))
                        before = cur.fetchone()[0] or 0
                        cur.execute("DELETE FROM ads_examples WHERE chat_id = %s;", (chat_id,))
                        conn.commit()
                    txt = tn(
                        "ads.examples.cleared.one",
                        "ads.examples.cleared.many",
                        before,
                        chat_id=update.effective_chat.id if update.effective_chat else None
                    )

                    m = await safe_reply_text(update, txt)

                    await _auto_cleanup_pair(update, context, m)
                    return m
                except Exception as e:
                    m = await safe_reply_text(update, f"خطا در حذف: {e}")
                    await _auto_cleanup_pair(update, context, m)
                    return m
            else:
                m = await safe_reply_text(update, t("ads.examples.title", chat_id=update.effective_chat.id if update.effective_chat else None))
                await _auto_cleanup_pair(update, context, m)
                return m

        help_text = (
            "مدیریت گارد تبلیغات:\n"
            "/ads on|off|status|action|threshold|chatflow|fewshots|balance|gap|wuser|wdomain|autoclean|...\n"
            "/ads action warn|delete|none\n"
            "/ads add <متن>  (یا روی پیام نمونه ریپلای)\n"
            "/ads notad <متن>  (یا روی پیام غیرتبلیغاتی ریپلای)\n"
            "/ads list [n]\n"
            "/ads stats [24h|7d|all]\n"
            "/ads simulate <thr> [24h|7d|all]\n"
            "/ads examples count | clear YES\n"
            "/ads examples stats\n"
            "/ads_examples_clear YES\n"
            "/ads chatflow <id>\n"
            "/ads fewshots <n>\n"
            "/ads balance on|off  (بالانس AD/NOT_AD در انتخاب few-shots)\n"
            "/ads gap <sec>\n"
            "/ads wuser add|remove|list [user_id]\n"
            "/ads wdomain add|remove|list [domain]\n"
            "/ads autoclean <sec|Xm|off>\n"
            "/ads reply on|off\n"
            "/ads replylen <n>\n"
            "/ads replycontact on|off\n"
            "/ads replycontactlen <n>\n"
            "/ads wuser add|remove|list [user_id]\n"
            "/ads wdomain add|remove|list [domain]\n"
            "/ads autoclean <sec|Xm|off>\n"
            "/ads reply on|off\n"
            "/ads replylen <n>\n"
            "/ads replycontact on|off\n"
            "/ads replycontactlen <n>\n"
            "— تنظیمات سناریوی کپشن/فوروارد —\n"
            "/ads captionlen <n>\n"
            "/ads nocap_grace <sec|Xm|off>\n"
            "/ads allow_forward on|off\n"
            "/ads fwd_captionlen <n>\n"
            "/ads fwd_grace <sec|Xm|off>\n"
            "/ads reply_as_caption on|off\n"
            "— پیام اخطار پس از موفقیت —\n"
            "/ads warn_success_action edit|delete\n"
            "/ads warn_success_autodel <sec|Xm|off>\n"
            "/ads mute_hours <n>\n"
        )


        m = await safe_reply_text(update, help_text)
        await _auto_cleanup_pair(update, context, m)
        return m

    # --- معادل‌های قدیمی برای backward-compat ---
    async def ads_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        # اگر ادمین نیست → همانجا پیام بده و خارج شو
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
    
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        ads_guard.chat_set_config(chat_id, "ads_feature", "on")
        m = await safe_reply_text(update, "ADS_FEATURE = ON")
        await _auto_cleanup_pair(update, context, m)
        return m



    async def ads_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        # اگر ادمین نیست → همانجا خروج
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
    
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        ads_guard.chat_set_config(chat_id, "ads_feature", "off")
        m = await safe_reply_text(update, "ADS_FEATURE = OFF")
        await _auto_cleanup_pair(update, context, m)
        return m



    async def ads_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        feature_status = "ON" if ads_guard.chat_feature_on(chat_id) else "OFF"
        info = (
            f"feature: {feature_status}\n"
            f"action: {ads_guard.chat_action(chat_id)}\n"
            f"threshold: {ads_guard.chat_threshold(chat_id):.2f}\n"
            f"max_fewshots: {ads_guard.chat_max_fewshots(chat_id)}\n"
            f"examples_select: {ads_guard.chat_examples_select_mode(chat_id)}\n"
            f"min_gap_sec: {ads_guard.chat_min_gap_sec(chat_id)}\n"
            f"chatflow_id: {ads_guard.chat_chatflow_id(chat_id) or '-'}\n"
        )
        m = await safe_reply_text(update, info)
        await _auto_cleanup_pair(update, context, m)
        return m


    async def ads_action_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        # ادمین؟
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
    
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        args = context.args or []
        if not args or args[0].lower() not in ("warn", "delete", "none"):
            # با فرم جدید /ads action هم‌خوان شود
            m = await safe_reply_text(update, "استفاده: /ads action warn|delete|none")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        ads_guard.chat_set_config(chat_id, "ads_action", args[0].lower())
        m = await safe_reply_text(update, f"ADS_ACTION = {args[0].lower()}")
        await _auto_cleanup_pair(update, context, m)
        return m


    async def ads_autoclean_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        # فقط ادمین (همان require_admin عمومی این فایل را استفاده می‌کنیم)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
    
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        args = context.args or []
        val = (args[1] if len(args) >= 2 else "").strip().lower()
    
        if not val:
            sec = ads_guard.chat_autoclean_sec(chat_id)
            txt = tn(
                "ads.autoclean.status.one",
                "ads.autoclean.status.many",
                sec,
                chat_id=chat_id
            )
            m = await safe_reply_text(update, txt)

            await _auto_cleanup_pair(update, context, m)
            return m
    
        # مقادیر مجاز: عدد ثانیه یا 'off'
        if val in ("off", "disable", "0"):
            ads_guard.chat_set_config(chat_id, "ads_autoclean_sec", "0")
            m = await safe_reply_text(update, "🧹 پاک‌سازی خودکار: غیرفعال شد.")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        # پشتیبانی ساده از پسوند m/s (مثلاً 2m یا 120s)
        try:
            if val.endswith("m"):
                seconds = int(float(val[:-1]) * 60)
            elif val.endswith("s"):
                seconds = int(float(val[:-1]))
            else:
                seconds = int(float(val))  # ثانیه
            if seconds < 0:
                raise ValueError()
        except Exception:
            m = await safe_reply_text(update, "فرمت: /ads autoclean <ثانیه|Xm|off>\nمثال: /ads autoclean 120  یا  /ads autoclean 2m")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        ads_guard.chat_set_config(chat_id, "ads_autoclean_sec", str(seconds))
        txt = tn(
            "ads.autoclean.set.one",
            "ads.autoclean.set.many",
            seconds,
            chat_id=chat_id
        )
        m = await safe_reply_text(update, txt)

        await _auto_cleanup_pair(update, context, m)
        return m
    
    
    async def ads_reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        args = context.args or []
        if len(args) < 2 or args[1].lower() not in ("on","off","true","false","1","0","yes","no"):
            m = await safe_reply_text(update, "استفاده: /ads reply on|off")
            await _auto_cleanup_pair(update, context, m)
            return m
        v = "on" if args[1].lower() in ("on","true","1","yes") else "off"
        ads_guard.chat_set_config(chat_id, "ads_reply_exempt", v)
        m = await safe_reply_text(update, f"reply_exempt = {v}")
        await _auto_cleanup_pair(update, context, m)
        return m

    async def ads_replylen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        args = context.args or []
        if len(args) < 2:
            m = await safe_reply_text(update, "استفاده: /ads replylen <max_chars>")
            await _auto_cleanup_pair(update, context, m)
            return m
        try:
            n = int(float(args[1]))
            if n < 0:
                raise ValueError()
        except Exception:
            m = await safe_reply_text(update, "یک عدد >= 0 بده (۰ یعنی بدون محدودیت طول برای معافیت کوتاه).")
            await _auto_cleanup_pair(update, context, m)
            return m
        ads_guard.chat_set_config(chat_id, "ads_reply_exempt_maxlen", str(n))
        txt = tn(
            "ads.reply_exempt_maxlen.set.one",
            "ads.reply_exempt_maxlen.set.many",
            n,
            chat_id=chat_id
        )
        m = await safe_reply_text(update, txt)

        await _auto_cleanup_pair(update, context, m)
        return m

    async def ads_replycontact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        args = context.args or []
        if len(args) < 2 or args[1].lower() not in ("on","off","true","false","1","0","yes","no"):
            m = await safe_reply_text(update, "استفاده: /ads replycontact on|off")
            await _auto_cleanup_pair(update, context, m)
            return m
        v = "on" if args[1].lower() in ("on","true","1","yes") else "off"
        ads_guard.chat_set_config(chat_id, "ads_reply_exempt_allow_contact", v)
        m = await safe_reply_text(update, f"reply_exempt_allow_contact = {v}")
        await _auto_cleanup_pair(update, context, m)
        return m

    async def ads_replycontactlen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        args = context.args or []
        if len(args) < 2:
            m = await safe_reply_text(update, "استفاده: /ads replycontactlen <max_chars>")
            await _auto_cleanup_pair(update, context, m)
            return m
        try:
            n = int(float(args[1]))
            if n < 0:
                raise ValueError()
        except Exception:
            m = await safe_reply_text(update, "یک عدد >= 0 بده (۰ یعنی بدون محدودیت طول برای معافیت پاسخ‌های حاوی تماس).")
            await _auto_cleanup_pair(update, context, m)
            return m
        ads_guard.chat_set_config(chat_id, "ads_reply_exempt_contact_maxlen", str(n))
        txt = tn(
            "ads.reply_exempt_contact_maxlen.set.one",
            "ads.reply_exempt_contact_maxlen.set.many",
            n,
            chat_id=chat_id
        )
        m = await safe_reply_text(update, txt)

        await _auto_cleanup_pair(update, context, m)
        return m

    
    
    async def _parse_seconds(arg: str) -> int:
        """
        ورودی‌هایی مثل "90" یا "2m"/"2M" یا "45s" را به ثانیه تبدیل می‌کند.
        برگشت: عدد ثانیه (>=0). اگر "off" باشد، 0 برمی‌گرداند.
        """
        a = (arg or "").strip().lower()
        if a in ("off", "0", "no"):
            return 0
        try:
            if a.endswith("m"):
                return max(0, int(float(a[:-1]) * 60))
            if a.endswith("s"):
                return max(0, int(float(a[:-1])))
            return max(0, int(float(a)))
        except Exception:
            raise ValueError("bad seconds")
    
    async def ads_captionlen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m
        chat_id = await _target_chat_id(update, context); 
        if not chat_id: return
        args = context.args or []
        if len(args) < 2:
            m = await safe_reply_text(update, "استفاده: /ads captionlen <n>")
            await _auto_cleanup_pair(update, context, m); return m
        try:
            n = int(float(args[1])); 
            if n < 0: raise ValueError()
        except Exception:
            m = await safe_reply_text(update, "یک عدد >= 0 بده.")
            await _auto_cleanup_pair(update, context, m); return m
        ads_guard.chat_set_config(chat_id, "ads_caption_min_len", str(n))
        txt = tn(
            "ads.caption_min_len.set.one",
            "ads.caption_min_len.set.many",
            n,
            chat_id=chat_id
        )
        m = await safe_reply_text(update, txt)

        await _auto_cleanup_pair(update, context, m); return m
    
    async def ads_nocap_grace_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m
        chat_id = await _target_chat_id(update, context); 
        if not chat_id: return
        args = context.args or []
        if len(args) < 2:
            m = await safe_reply_text(update, "استفاده: /ads nocap_grace <sec|Xm|off>")
            await _auto_cleanup_pair(update, context, m); return m
        try:
            sec = await _parse_seconds(args[1])
        except Exception:
            m = await safe_reply_text(update, "مثال: 90 یا 2m یا off")
            await _auto_cleanup_pair(update, context, m); return m
        ads_guard.chat_set_config(chat_id, "ads_nocap_grace_sec", str(sec))
        m = await safe_reply_text(update, f"nocap_grace_sec = {sec}")
        await _auto_cleanup_pair(update, context, m); return m
    
    async def ads_allow_forward_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # فوروارد از گروه/کانال/بات: on|off (فوروارد از PV مشمول این قاعده نیست)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m
        chat_id = await _target_chat_id(update, context); 
        if not chat_id: return
        args = context.args or []
        if len(args) < 2 or args[1].lower() not in ("on","off","true","false","1","0","yes","no"):
            m = await safe_reply_text(update, "استفاده: /ads allow_forward on|off")
            await _auto_cleanup_pair(update, context, m); return m
        v = "on" if args[1].lower() in ("on","true","1","yes") else "off"
        ads_guard.chat_set_config(chat_id, "ads_allow_forward_entities", v)
        m = await safe_reply_text(update, f"allow_forward_entities = {v}")
        await _auto_cleanup_pair(update, context, m); return m
    
    async def ads_fwd_captionlen_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # حداقل طول سخت‌گیرانه برای کپشن فورواردهای مجاز
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m
        chat_id = await _target_chat_id(update, context); 
        if not chat_id: return
        args = context.args or []
        if len(args) < 2:
            m = await safe_reply_text(update, "استفاده: /ads fwd_captionlen <n>")
            await _auto_cleanup_pair(update, context, m); return m
        try:
            n = int(float(args[1])); 
            if n < 0: raise ValueError()
        except Exception:
            m = await safe_reply_text(update, "یک عدد >= 0 بده.")
            await _auto_cleanup_pair(update, context, m); return m
        ads_guard.chat_set_config(chat_id, "ads_forward_caption_min_len", str(n))
        txt = tn(
            "ads.forward_caption_min_len.set.one",
            "ads.forward_caption_min_len.set.many",
            n,
            chat_id=chat_id
        )
        m = await safe_reply_text(update, txt)

        await _auto_cleanup_pair(update, context, m); return m
    
    
    
    async def ads_warn_success_action_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m

        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return

        args = context.args or []
        if len(args) < 2 or args[1].lower() not in ("edit", "delete"):
            m = await safe_reply_text(update, "استفاده: /ads warn_success_action edit|delete")
            await _auto_cleanup_pair(update, context, m); return m

        v = args[1].lower()
        ads_guard.chat_set_config(chat_id, "ads_warn_success_action", v)
        m = await safe_reply_text(update, f"ads_warn_success_action = {v}")
        await _auto_cleanup_pair(update, context, m); return m


    async def ads_warn_success_autodel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m

        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return

        args = context.args or []
        if len(args) < 2:
            m = await safe_reply_text(update, "استفاده: /ads warn_success_autodel <sec>")
            await _auto_cleanup_pair(update, context, m); return m

        # از همان پارسر سراسری استفاده می‌کنیم (مثل nocap_grace)
        try:
            sec = await _parse_seconds(args[1])
        except Exception:
            m = await safe_reply_text(update, "فرمت: /ads warn_success_autodel <ثانیه|Xm|off>\nمثال: /ads warn_success_autodel 10  یا  /ads warn_success_autodel off")
            await _auto_cleanup_pair(update, context, m); return m

        ads_guard.chat_set_config(chat_id, "ads_warn_success_autodel_sec", str(sec))
        if sec == 0:
            m = await safe_reply_text(update, "ads_warn_success_autodel_sec = OFF")
        else:
            m = await safe_reply_text(update, f"ads_warn_success_autodel_sec = {sec}")
        await _auto_cleanup_pair(update, context, m); return m

    
    
    
    async def ads_fwd_grace_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # مهلت سخت‌گیرانه‌تر برای فورواردهای مجاز
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m
        chat_id = await _target_chat_id(update, context); 
        if not chat_id: return
        args = context.args or []
        if len(args) < 2:
            m = await safe_reply_text(update, "استفاده: /ads fwd_grace <sec|Xm|off>")
            await _auto_cleanup_pair(update, context, m); return m
        try:
            sec = await _parse_seconds(args[1])
        except Exception:
            m = await safe_reply_text(update, "مثال: 180 یا 3m یا off")
            await _auto_cleanup_pair(update, context, m); return m
        ads_guard.chat_set_config(chat_id, "ads_forward_grace_sec", str(sec))
        m = await safe_reply_text(update, f"forward_grace_sec = {sec}")
        await _auto_cleanup_pair(update, context, m); return m
    
    async def ads_reply_as_caption_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m
        chat_id = await _target_chat_id(update, context); 
        if not chat_id: return
        args = context.args or []
        if len(args) < 2 or args[1].lower() not in ("on","off","true","false","1","0","yes","no"):
            m = await safe_reply_text(update, "استفاده: /ads reply_as_caption on|off")
            await _auto_cleanup_pair(update, context, m); return m
        v = "on" if args[1].lower() in ("on","true","1","yes") else "off"
        ads_guard.chat_set_config(chat_id, "ads_allow_reply_as_caption", v)
        m = await safe_reply_text(update, f"reply_as_caption = {v}")
        await _auto_cleanup_pair(update, context, m); return m

    
    
    async def ads_mute_hours_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m); return m

        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return

        args = context.args or []
        if len(args) < 2:
            m = await safe_reply_text(update, "استفاده: /ads mute_hours <n>")
            await _auto_cleanup_pair(update, context, m); return m

        try:
            n = int(args[1]); assert n > 0
        except Exception:
            m = await safe_reply_text(update, "یک عدد صحیحِ مثبت بده مثل: /ads mute_hours 100")
            await _auto_cleanup_pair(update, context, m); return m

        ads_guard.chat_set_config(chat_id, "ads_mute_hours", str(n))
        txt = tn(
            "ads.mute.hours.set.one",
            "ads.mute.hours.set.many",
            n,
            chat_id=chat_id
        )
        m = await safe_reply_text(update, txt)

        await _auto_cleanup_pair(update, context, m); return m

    
    

    async def ads_threshold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        # فقط ادمین
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
    
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        args = context.args or []
        if not args:
            m = await safe_reply_text(update, "استفاده: /ads threshold <0..1>")
            await _auto_cleanup_pair(update, context, m)
            return m
        try:
            v = float(args[0])
            if not (0.0 <= v <= 1.0):
                raise ValueError()
        except Exception:
            m = await safe_reply_text(update, "عددی بین 0 و 1 بده.")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        ads_guard.chat_set_config(chat_id, "ads_threshold", str(v))
        m = await safe_reply_text(update, f"threshold = {v}")
        await _auto_cleanup_pair(update, context, m)
        return m



    async def ads_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
        
        msg = update.effective_message
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return

        # اولویت 1: اگر روی پیام ریپلای کرده‌ای، از همان پیام (متن یا کپشن) بخوان
        reply = msg.reply_to_message
        text = ""
        if reply:
            text = (reply.text or reply.caption or "").strip()
            # اگر آیتم آلبوم است و خودش کپشن ندارد، چند تلاش کوتاه برای کش آلبوم انجام بده
            if (not text) and getattr(reply, "media_group_id", None):
                for _ in range(4):  # حداکثر تا ~1 ثانیه (4 * 0.25s)
                    try:
                        text = ads_guard.caption_for_media_group(chat_id, reply.media_group_id) or ""
                    except Exception:
                        text = ""
                    if text:
                        break
                    await asyncio.sleep(0.25)


        # اولویت 2: اگر هنوز خالی بود، از آرگومان‌های بعد از دستور بخوان
        if not text:
            args = context.args or []
            if args and args[0].lower() == "add":
                text = " ".join(args[1:]).strip()
            else:
                text = " ".join(args).strip()

        # اولویت 3: اگر باز هم خالی بود، هرچه بعد از دستور در همین پیام آمده را بردار
        if not text:
            raw = (msg.text or msg.caption or "")
            import re
            # پشتیبانی از /ads@BotName add ...
            text = re.sub(
                r"^/(?:ads(?:@[\w_]+)?(?:\s+add)?|ads_add(?:@[\w_]+)?)\b",
                "",
                raw,
                flags=re.IGNORECASE
            ).strip()


        # اگر باز هم چیزی نداریم، ارور راهنما
        if not text:
            m = await safe_reply_text(update, "⛔️ متن نمونه را بعد از دستور بنویس یا روی پیام نمونه ریپلای کن.")
            await _auto_cleanup_pair(update, context, m)
            return m

        
        ok, reason = ads_guard.add_example(chat_id, text, update.effective_user.id if update.effective_user else None)
        if not ok and reason == "hardcap_reached":
            m = await safe_reply_text(update, f"⛔️ حدّاکثر {ads_guard.chat_examples_hardcap(chat_id)} نمونه برای این گروه مجاز است. برای پاک‌سازی از /ads examples clear YES استفاده کن.")
            await _auto_cleanup_pair(update, context, m)
            return m
        m = await safe_reply_text(update, "نمونه تبلیغاتی ثبت شد ✅")
        await _auto_cleanup_pair(update, context, m)


    
    async def ads_notad_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
    
        # استفاده از همان _require_admin که بالاتر در همین فایل تعریف شده
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
    
        msg = update.effective_message
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        # اولویت 1: ریپلای
        reply = msg.reply_to_message
        text = ""
        if reply:
            text = (reply.text or reply.caption or "").strip()
            # اگر آیتم آلبوم است و خودش کپشن ندارد، چند تلاش کوتاه برای کش آلبوم انجام بده
            if (not text) and getattr(reply, "media_group_id", None):
                for _ in range(4):  # حداکثر تا ~1 ثانیه (4 * 0.25s)
                    try:
                        text = ads_guard.caption_for_media_group(chat_id, reply.media_group_id) or ""
                    except Exception:
                        text = ""
                    if text:
                        break
                    await asyncio.sleep(0.25)

        # اولویت 2: آرگومان‌ها
        if not text:
            args = context.args or []
            if args and args[0].lower() == "notad":
                text = " ".join(args[1:]).strip()
            else:
                text = " ".join(args).strip()
    
        # اولویت 3: متن بعد از دستور (با پشتیبانی از @mention)
        if not text:
            raw = (msg.text or msg.caption or "")
            import re
            text = re.sub(
                r"^/(?:ads(?:@[\w_]+)?(?:\s+notad)?|ads_notad(?:@[\w_]+)?)\b",
                "",
                raw,
                flags=re.IGNORECASE
            ).strip()
    
        if not text:
            m = await safe_reply_text(update, "⛔️ متن نمونه را بعد از دستور بنویس یا روی پیام غیرتبلیغاتی ریپلای کن.")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        # ثبت نمونه غیرتبلیغاتی
        ok, reason = ads_guard.add_example(
            chat_id,
            text,
            update.effective_user.id if update.effective_user else None,
            label="NOT_AD",
        )
        if not ok and reason == "hardcap_reached":
            m = await safe_reply_text(update, f"⛔️ حدّاکثر {ads_guard.chat_examples_hardcap(chat_id)} نمونه برای این گروه مجاز است. برای پاک‌سازی از /ads examples clear YES استفاده کن.")
            await _auto_cleanup_pair(update, context, m)
            return m
        m = await safe_reply_text(update, "نمونه غیرتبلیغاتی ثبت شد ✅")
        await _auto_cleanup_pair(update, context, m)


    # --- ADD: clear all examples command ---
    import logging
    logger = logging.getLogger(__name__)
    
    async def ads_clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        پاک‌کردن همه‌ی نمونه‌های تبلیغاتی/غیرتبلیغاتی از جدول ads_examples
        فقط برای ادمین. برای تأیید باید بنویسی: /ads_clear confirm
        """
        # 1) فقط ادمین‌ها
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m

    
        # 2) نیاز به تایید صریح
        confirm = " ".join(context.args or []).strip().lower()
        if confirm != "confirm":
            m = await safe_reply_text(
                update,
                "⚠️ این دستور «همه‌ی نمونه‌ها» را پاک می‌کند.\n"
                "برای تأیید بفرست:\n\n"
                "/ads_clear confirm"
            )
            await _auto_cleanup_pair(update, context, m)
            return m
    
        # 3) پاک‌سازی سریع و استاندارد جدول با TRUNCATE
        try:
            chat_id = await _target_chat_id(update, context)
            if not chat_id:
                return
            with db_conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM ads_examples WHERE chat_id = %s;", (chat_id,))
                conn.commit()
            m = await safe_reply_text(update, t("ads.examples.clear.ok", chat_id=update.effective_chat.id if update.effective_chat else None))
            await _auto_cleanup_pair(update, context, m)
            return m
        except Exception as e:
            logger.exception("Failed to clear ads_examples", exc_info=e)
            m = await safe_reply_text(update, t("ads.examples.clear.err", chat_id=update.effective_chat.id if update.effective_chat else None, reason=f"{e}"))
            await _auto_cleanup_pair(update, context, m)
            return m
    # --- /ADD ---



    async def ads_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
        limit = 10
        if context.args:
            try:
                limit = max(1, min(50, int(context.args[0])))
            except Exception:
                pass
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        rows = ads_guard.list_examples(chat_id, limit=limit)
        if not rows:
            m = await safe_reply_text(update, t("ads.examples.empty", chat_id=update.effective_chat.id if update.effective_chat else None))
            await _auto_cleanup_pair(update, context, m)
            return m
        lines = [t("ads.examples.title", chat_id=update.effective_chat.id if update.effective_chat else None)]
        import html as _html
        for (i, preview, ts, label) in rows:
            tag = "[AD]" if label == "AD" else "[NOT_AD]"
            safe_preview = _html.escape(preview or "")
            lines.append(f"- # {i:>3} | {tag} | {ts} | {safe_preview}")
        m = await safe_reply_text(update, "\n".join(lines), parse_mode=ParseMode.HTML)
        await _auto_cleanup_pair(update, context, m)
        return m



    async def ads_examples_clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
        args = context.args or []
        if not (len(args) >= 1 and str(args[0]).upper() == "YES"):
            m = await safe_reply_text(update, t("ads.examples.clear.prompt", chat_id=update.effective_chat.id if update.effective_chat else None))
            await _auto_cleanup_pair(update, context, m)
            return m
        try:
            chat_id = await _target_chat_id(update, context)
            if not chat_id:
                return
            with db_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM ads_examples WHERE chat_id = %s;", (chat_id,))
                before = cur.fetchone()[0] or 0
                cur.execute("DELETE FROM ads_examples WHERE chat_id = %s;", (chat_id,))
                conn.commit()
            txt = tn(
                "ads.examples.cleared.one",
                "ads.examples.cleared.many",
                before,
                chat_id=update.effective_chat.id if update.effective_chat else None,
                n=before
            )
            m = await safe_reply_text(update, txt)

            await _auto_cleanup_pair(update, context, m)
            return m
        except Exception as e:
            m = await safe_reply_text(update, f"خطا در حذف: {e}")
            await _auto_cleanup_pair(update, context, m)
            return m
            
    async def ads_examples_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        آمار نمونه‌های ذخیره‌شده در همین گروه (few-shot):
        - تعداد کل نمونه‌ها
        - تعداد AD و NOT_AD
        - درصد هرکدام
        نکته: همه‌چیز بر اساس chat_id فعلی فیلتر می‌شود.
        """
        # ثبت/به‌روز کردن اطلاعات کاربر (برای گزارش‌ها/دیتابیس)
        upsert_user_from_update(update)
    
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        try:
            # به دیتابیس وصل شو و بر اساس برچسب‌ها شمارش کن
            with db_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT UPPER(COALESCE(label,'AD')) AS lbl, COUNT(*)
                    FROM ads_examples
                    WHERE chat_id = %s
                    GROUP BY 1
                    ORDER BY 1;
                    """,
                    [chat_id],
                )
                rows = cur.fetchall() or []
    
            # نرمال‌سازی نتایج
            total = sum(int(cnt or 0) for _, cnt in rows)
            ad = 0
            notad = 0
            other = 0
            for lbl, cnt in rows:
                lbl_u = str(lbl or "").upper()
                c = int(cnt or 0)
                if lbl_u == "AD":
                    ad = c
                elif lbl_u in ("NOT_AD", "NOTAD", "NEG", "SAFE"):
                    notad = c
                else:
                    other += c
    
            # محاسبهٔ درصدها
            def pct(x: int, tot: int) -> float:
                return (x * 100.0 / tot) if tot > 0 else 0.0
    
            # گرفتن عنوان گروه برای نمایش یوزرفرندلی
            try:
                chat = await context.bot.get_chat(chat_id)
                gtitle = getattr(chat, "title", None) or str(chat_id)
            except Exception:
                gtitle = str(chat_id)
            gtitle_html = html.escape(gtitle)
            
            lines = [
                f"📚 Examples stats (<b>{gtitle_html}</b>):",
                f"- total: {total}",
                f"- AD: {ad}  ({pct(ad, total):.1f}%)",
                f"- NOT_AD: {notad}  ({pct(notad, total):.1f}%)",
            ]

    
            if other > 0:
                lines.append(f"- other labels: {other}  ({pct(other, total):.1f}%)")
    
            # نکتهٔ راهنما برای تعادل داده
            if total > 0:
                ratio_tip = "✅ تعادل خوبه." if min(ad, notad) / max(ad, notad or 1) >= 0.5 else "ℹ️ بهتره از نوعِ کمتر، نمونهٔ بیشتری اضافه کنی."
                lines.append(f"\nنکته: {ratio_tip}")
            else:
                lines.append("\nهنوز نمونه‌ای ثبت نشده. با /ads add و /ads notad نمونه اضافه کن.")
    
            m = await safe_reply_text(update, "\n".join(lines), parse_mode=ParseMode.HTML)
            await _auto_cleanup_pair(update, context, m)
            return m
    
        except Exception as e:
            m = await safe_reply_text(update, f"❌ خطا در examples stats: {e}")
            await _auto_cleanup_pair(update, context, m)
            return m

            

    async def ads_wuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        args = context.args or []
        if not args:
            m = await safe_reply_text(update, t("ads.wuser.usage", chat_id=update.effective_chat.id if update.effective_chat else None))
            await _auto_cleanup_pair(update, context, m)
            return m
    
        sub = args[0].lower()
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        if sub == "list":
            rows = ads_guard.wl_users_list(chat_id)
            if not rows:
                m = await safe_reply_text(update, t("ads.wuser.empty", chat_id=update.effective_chat.id if update.effective_chat else None))
                await _auto_cleanup_pair(update, context, m)
                return m
            lines = [t("ads.wuser.list.title", chat_id=update.effective_chat.id if update.effective_chat else None)]
            for uid, ts in rows:
                lines.append(t("ads.wuser.list.item", chat_id=update.effective_chat.id if update.effective_chat else None, uid=uid, ts=ts))
            m = await safe_reply_text(update, "\n".join(lines), parse_mode=ParseMode.HTML)
            await _auto_cleanup_pair(update, context, m)
            return m
    
        if sub in ("add", "remove"):
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))
                await _auto_cleanup_pair(update, context, m)
                return m
    
            target_id = None
            if len(args) >= 2:
                try:
                    target_id = int(args[1])
                except Exception:
                    target_id = None
            if not target_id and update.effective_message.reply_to_message:
                target_id = update.effective_message.reply_to_message.from_user.id
            if not target_id:
                m = await safe_reply_text(update, t("ads.wuser.reply_or_id", chat_id=update.effective_chat.id if update.effective_chat else None))
                await _auto_cleanup_pair(update, context, m)
                return m
    
            if sub == "add":
                ads_guard.wl_user_add(chat_id, target_id, update.effective_user.id if update.effective_user else None)
                m = await safe_reply_text(update, t("ads.wuser.add.ok", chat_id=update.effective_chat.id if update.effective_chat else None, target_id=target_id))
                await _auto_cleanup_pair(update, context, m)
                return m
            else:
                ads_guard.wl_user_del(chat_id, target_id)
                m = await safe_reply_text(update, t("ads.wuser.remove.ok", chat_id=update.effective_chat.id if update.effective_chat else None, target_id=target_id))
                await _auto_cleanup_pair(update, context, m)
                return m
    
        m = await safe_reply_text(update, t("ads.wuser.usage", chat_id=update.effective_chat.id if update.effective_chat else None))
        await _auto_cleanup_pair(update, context, m)
        return m


    async def ads_wdomain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upsert_user_from_update(update)
        args = context.args or []
        if not args:
            m = await safe_reply_text(update, t("ads.wdomain.usage", chat_id=update.effective_chat.id if update.effective_chat else None))
            await _auto_cleanup_pair(update, context, m)
            return m
    
        sub = args[0].lower()
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        if sub == "list":
            rows = ads_guard.wl_domains_list(chat_id)
            if not rows:
                m = await safe_reply_text(update, t("ads.wdomain.empty", chat_id=update.effective_chat.id if update.effective_chat else None))
                await _auto_cleanup_pair(update, context, m)
                return m
            lines = [t("ads.wdomain.list.title", chat_id=update.effective_chat.id if update.effective_chat else None)]
            for dom, ts in rows:
                lines.append(t("ads.wdomain.list.item", chat_id=update.effective_chat.id if update.effective_chat else None, domain=dom, ts=ts))
            m = await safe_reply_text(update, "\n".join(lines), parse_mode=ParseMode.HTML)
            await _auto_cleanup_pair(update, context, m)
            return m
    
        if sub in ("add", "remove"):
            if not await _require_admin(update, context):
                m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))
                await _auto_cleanup_pair(update, context, m)
                return m
    
            domain = None
            if len(args) >= 2:
                domain = args[1]
            if not domain and update.effective_message.reply_to_message:
                txt = (update.effective_message.reply_to_message.text or update.effective_message.reply_to_message.caption or "")
                domains = ads_guard._extract_domains(txt)
                domain = domains[0] if domains else None
            if not domain:
                m = await safe_reply_text(update, t("ads.wdomain.need_domain", chat_id=update.effective_chat.id if update.effective_chat else None))
                await _auto_cleanup_pair(update, context, m)
                return m
    
            if sub == "add":
                ads_guard.wl_domain_add(chat_id, domain, update.effective_user.id if update.effective_user else None)
                m = await safe_reply_text(update, t("ads.wdomain.add.ok", chat_id=update.effective_chat.id if update.effective_chat else None, domain=domain))
                await _auto_cleanup_pair(update, context, m)
                return m
            else:
                ads_guard.wl_domain_del(chat_id, domain)
                m = await safe_reply_text(update, t("ads.wdomain.remove.ok", chat_id=update.effective_chat.id if update.effective_chat else None, domain=domain))
                await _auto_cleanup_pair(update, context, m)
                return m
    
        m = await safe_reply_text(update, t("ads.wdomain.usage", chat_id=update.effective_chat.id if update.effective_chat else None))
        await _auto_cleanup_pair(update, context, m)
        return m


    async def ads_probe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """اجرای تست مدل روی یک متن (یا پیام ریپلای‌شده) با خروجی خوانا و امن."""
        upsert_user_from_update(update)
        if not await _require_admin(update, context):
            m = await safe_reply_text(update, t("errors.only_admin_short", chat_id=update.effective_chat.id if update.effective_chat else None))

            await _auto_cleanup_pair(update, context, m)
            return m
    
        # ورودی
        msg = update.effective_message
        text = " ".join(context.args or []).strip()
        if not text and msg and msg.reply_to_message:
            text = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()
        if not text:
            m = await safe_reply_text(update, "استفاده: /ads_probe <متن> (یا روی پیام ریپلای کن و بزن /ads_probe)")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        # گروه هدف
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
    
        # آماده‌سازی few-shot و پرامپت
        _k = ads_guard.chat_max_fewshots(chat_id)
        examples = ads_guard._fetch_examples(chat_id, _k)
        examples_str = "\n\n".join([f"مثال {i+1}:\n[{e[3]}]\n{e[1]}" for i, e in enumerate(examples[:_k])])
        prompt = ads_guard._build_prompt(text, examples)
    
        # تماس با Flowise (thread)
        parsed, err = await asyncio.to_thread(
            ads_guard._call_flowise_ads, prompt, text, examples_str, chat_id,
            {"is_reply": False, "has_contact": ads_guard._has_contact_like(text)}
        )
        if err and not parsed:
            m = await safe_reply_text(update, f"❌ خطا در تماس با مدل: {err}")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        # محاسبهٔ نتیجه با آستانهٔ فعلی
        thr = ads_guard.chat_threshold(chat_id)
        label, score, reason = None, None, None
        is_ad = False
        if parsed and isinstance(parsed, dict):
            label = str(parsed.get("label", "")).upper()
            try:
                score = float(parsed.get("score")) if parsed.get("score") is not None else None
            except Exception:
                score = None
            reason = parsed.get("reason")
            is_ad = (label == "AD") and (score is None or score >= thr)
    
        # خروجی یکدست (حتی اگر parsed ناقص باشد)
        lines = [
            "🔎 نتیجهٔ Probe:",
            f"label: {label or '-'}",
            f"score: {score if score is not None else '-'}",
            f"reason: {reason or '-'}",
            f"threshold: {thr:.2f}",
            f"→ decision at current policy: {'AD ✅' if is_ad else 'NOT_AD'}",
        ]
        m = await safe_reply_text(update, "\n".join(lines), parse_mode=ParseMode.HTML)
        await _auto_cleanup_pair(update, context, m)
        return m
    
    async def ads_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        آمار سریع از تصمیم‌های اخیر این گروه در جدول ads_decisions.
        فرمت: /ads stats [24h|7d|all]  یا  /ads_stats [24h|7d|all]
        """
        upsert_user_from_update(update)
    
        # بازه زمانی
        arg = (context.args[0].lower() if context.args else "24h").strip()
        since_sql = None
        if arg in ("24h", "24", "day"):
            since_sql = "NOW() - INTERVAL '24 hours'"
            window_label = "24h"
        elif arg in ("7d", "7", "week"):
            since_sql = "NOW() - INTERVAL '7 days'"
            window_label = "7d"
        else:
            window_label = "all"
    
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        try:
            with db_conn() as conn, conn.cursor() as cur:
                params = [chat_id]
                where_time = ""
                if since_sql:
                    where_time = "AND decided_at >= " + since_sql
                q = f"""
                    SELECT
                      COUNT(*) AS total,
                      SUM(CASE WHEN is_ad THEN 1 ELSE 0 END) AS ad_hits,
                      AVG(score) AS avg_score
                    FROM ads_decisions
                    WHERE chat_id = %s {where_time};
                """
                cur.execute(q, params)
                row = cur.fetchone() or (0, 0, None)
                total = int(row[0] or 0)
                ad_hits = int(row[1] or 0)
                avg_score = (row[2] if row[2] is not None else None)
    
                # نمایش نرخ
                rate = (ad_hits / total * 100.0) if total > 0 else 0.0
    
            # نام گروه برای تیتر
            try:
                chat = await context.bot.get_chat(chat_id)
                gtitle = getattr(chat, "title", None) or str(chat_id)
            except Exception:
                gtitle = str(chat_id)
            gtitle_html = html.escape(gtitle)
            
            info_lines = [
                f"📊 Stats ({window_label}) — <b>{gtitle_html}</b>:",
                f"- total checked: {total}",
                f"- AD predicted: {ad_hits}  ({rate:.1f}%)",
                f"- avg score: {avg_score:.3f}" if avg_score is not None else "- avg score: -",
            ]
            m = await safe_reply_text(update, "\n".join(info_lines), parse_mode=ParseMode.HTML)

            
            await _auto_cleanup_pair(update, context, m)
            return m
        except Exception as e:
            m = await safe_reply_text(update, f"❌ خطا در stats: {e}")
            await _auto_cleanup_pair(update, context, m)
            return m
    
    async def ads_simulate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        شبیه‌سازی با آستانهٔ دلخواه (بدون اعمال واقعی).
        فرمت: /ads simulate <thr> [24h|7d|all]  یا  /ads_simulate <thr> [24h|7d|all]
        """
        upsert_user_from_update(update)
        args = context.args or []
        if not args:
            m = await safe_reply_text(update, "استفاده: /ads simulate <0..1> [24h|7d|all]")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        # آستانه
        try:
            thr = float(args[0])
            assert 0.0 <= thr <= 1.0
        except Exception:
            m = await safe_reply_text(update, "آستانه باید عددی بین 0 و 1 باشد.")
            await _auto_cleanup_pair(update, context, m)
            return m
    
        # بازه زمانی
        arg_window = (args[1].lower() if len(args) >= 2 else "24h").strip()
        since_sql = None
        if arg_window in ("24h", "24", "day"):
            since_sql = "NOW() - INTERVAL '24 hours'"
            window_label = "24h"
        elif arg_window in ("7d", "7", "week"):
            since_sql = "NOW() - INTERVAL '7 days'"
            window_label = "7d"
        else:
            window_label = "all"
    
        chat_id = await _target_chat_id(update, context)
        if not chat_id:
            return
        try:
            with db_conn() as conn, conn.cursor() as cur:
                params = [chat_id, thr]
                where_time = ""
                if since_sql:
                    where_time = "AND decided_at >= " + since_sql
    
                # اگر label='AD' و (score IS NULL یا score>=thr) → AD در آستانهٔ جدید
                q = f"""
                    SELECT
                      COUNT(*) AS total,
                      SUM(CASE WHEN (label = 'AD' AND (score IS NULL OR score >= %s)) THEN 1 ELSE 0 END) AS would_ad
                    FROM ads_decisions
                    WHERE chat_id = %s {where_time};
                """
                # توجه: ترتیب پارامترها با q هماهنگ باشد
                cur.execute(q, [thr, chat_id])
                row = cur.fetchone() or (0, 0)
                total = int(row[0] or 0)
                would_ad = int(row[1] or 0)
                ratio = (would_ad / total * 100.0) if total > 0 else 0.0
    
            # نام گروه برای تیتر
            try:
                chat = await context.bot.get_chat(chat_id)
                gtitle = getattr(chat, "title", None) or str(chat_id)
            except Exception:
                gtitle = str(chat_id)
            gtitle_html = html.escape(gtitle)
            
            lines = [
                f"🧪 Simulate ({window_label}) — <b>{gtitle_html}</b>:",
                f"- threshold: {thr:.2f}",
                f"- total checked: {total}",
                f"- would be AD: {would_ad}  ({ratio:.1f}%)",
                "⚠️ این فقط شبیه‌سازی است؛ پیام‌ها حذف یا اخطار نمی‌شوند.",
            ]
            m = await safe_reply_text(update, "\n".join(lines), parse_mode=ParseMode.HTML)
            
            
            await _auto_cleanup_pair(update, context, m)
            return m
        except Exception as e:
            m = await safe_reply_text(update, f"❌ خطا در simulate: {e}")
            await _auto_cleanup_pair(update, context, m)
            return m

    
    # --- ثبت هندلرها ---
    app.add_handler(CommandHandler("ads", ads_cmd))
    app.add_handler(CommandHandler("ads_on", ads_on_cmd))
    app.add_handler(CommandHandler("ads_off", ads_off_cmd))
    app.add_handler(CommandHandler("ads_status", ads_status_cmd))
    app.add_handler(CommandHandler("ads_action", ads_action_cmd))
    app.add_handler(CommandHandler("ads_threshold", ads_threshold_cmd))
    app.add_handler(CommandHandler("ads_add", ads_add_cmd))
    app.add_handler(CommandHandler("ads_notad", ads_notad_cmd))
    app.add_handler(CommandHandler("ads_clear", ads_clear_cmd))
    app.add_handler(CommandHandler("ads_list", ads_list_cmd))
    app.add_handler(CommandHandler("ads_probe", ads_probe_cmd))
    app.add_handler(CommandHandler("ads_wuser", ads_wuser_cmd))
    app.add_handler(CommandHandler("ads_wdomain", ads_wdomain_cmd))
    app.add_handler(CommandHandler("ads_examples_clear", ads_examples_clear_cmd))
    app.add_handler(CommandHandler("ads_examples_stats", ads_examples_stats_cmd))
    app.add_handler(CommandHandler("ads_stats", ads_stats_cmd))
    app.add_handler(CommandHandler("ads_simulate", ads_simulate_cmd))

