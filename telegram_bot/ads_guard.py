# ads_guard.py
# -----------------------------------------------------------------------------
# AdsGuard (گارد تبلیغات) - ساختار ماژولار بدون ثبت هندلرهای دستوری
# - مدیریت تنظیمات از DB و ENV
# - whitelist کاربر/دامنه
# - جمع‌آوری نمونه‌ها (few-shots)
# - فراخوانی Flowise و ذخیره تصمیم
# - watchdog برای گروه‌ها (group=-1 در ads_commands ثبت می‌شود)
# -----------------------------------------------------------------------------

import os, time, json, re, logging
import asyncio
import psycopg2
import psycopg2.extras
import requests
import itertools
from shared_utils import TG_ANON, MET_ADS_ACTION, count_words, ensure_chat_defaults, is_addressed_to_bot
from typing import Optional, Callable, List, Tuple, Dict
from telegram.error import BadRequest
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes, ApplicationHandlerStop
from html import escape as _esc
from shared_utils import build_sender_html_from_msg
from shared_utils import cfg_get_str, cfg_get_int, cfg_get_float
from messages_service import t, tn
from tokens.models import pg_conn, ensure_group_settings, grant_weekly_if_needed, spend_one_for_ad
from datetime import datetime, timezone
from telegram.ext import ApplicationHandlerStop


log = logging.getLogger("ads_guard")

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

try:
    from telegram.constants import ANONYMOUS_ADMIN  # PTB v20+
except Exception:
    try:
        from telegram.constants import ANONYMOUS_ADMIN_ID as ANONYMOUS_ADMIN  # PTB v13
    except Exception:
        ANONYMOUS_ADMIN = 1087968824  # fallback @GroupAnonymousBot

class AdsGuard:
    """
    ماژول نگهبان تبلیغات:
      - تنظیمات پویا (DB/ENV)
      - whitelist کاربر/دامنه
      - ثبت نمونه‌ها برای few-shot
      - تماس با Flowise و ذخیرهٔ تصمیم
      - watchdog پیام‌های گروه
    """

    def __init__(
        self,
        get_db_conn: Callable[[], psycopg2.extensions.connection],
        is_admin_fn: Callable[[int], bool],
        flowise_base_url: str,
        flowise_api_key: str
    ):
        self.get_db_conn = get_db_conn
        self.is_admin = is_admin_fn
        self.flowise_base_url = (flowise_base_url or "").rstrip("/")
        self.flowise_api_key = flowise_api_key or ""

        # Defaults (DB-first: bot_config → ENV)
        self._feature_env = (cfg_get_str("ads_feature", "ADS_FEATURE", "off") or "off").strip().lower()
        self._chatflow_id_env = (cfg_get_str("ads_chatflow_id", "ADS_CHATFLOW_ID", "") or "").strip()
        self._threshold_env = cfg_get_float("ads_threshold", "ADS_THRESHOLD", 0.78)
        self._max_fewshots_env = cfg_get_int("ads_max_fewshots", "ADS_MAX_FEWSHOTS", 10)
        self._examples_hardcap_env = cfg_get_int("ads_examples_hardcap", "ADS_EXAMPLES_HARDCAP", 50)  # سقف نمونه‌ها برای هر گروه
        self._action_env = (cfg_get_str("ads_action", "ADS_ACTION", "none") or "none").strip().lower()
        # Reply exemption defaults (DB-first)
        self._reply_exempt_env = (cfg_get_str("ads_reply_exempt", "ADS_REPLY_EXEMPT", "on") or "on").strip().lower()
        self._reply_exempt_maxlen_env = cfg_get_int("ads_reply_exempt_maxlen", "ADS_REPLY_EXEMPT_MAXLEN", 160)
        self._reply_exempt_allow_contact_env = (cfg_get_str("ads_reply_exempt_allow_contact", "ADS_REPLY_EXEMPT_ALLOW_CONTACT", "on") or "on").strip().lower()
        self._reply_exempt_contact_maxlen_env = cfg_get_int("ads_reply_exempt_contact_maxlen", "ADS_REPLY_EXEMPT_CONTACT_MAXLEN", 360)


        # --- Caption / Forward policies (DB-first) ---
        # حداقل طول کپشن برای مدیای عادی
        self._caption_min_len_env = cfg_get_int("ads_caption_min_len", "ADS_CAPTION_MIN_LEN", 10)
        # مهلت حذف برای مدیای بدون کپشن (ثانیه)
        self._nocap_grace_sec_env = cfg_get_int("ads_nocap_grace_sec", "ADS_NOCAP_GRACE_SEC", 300)
        
        # اجازهٔ فوروارد از کانال/گروه/بات؟ (اگر off باشد، چنین فورواردی فوراً اعمال سیاست می‌گیرد)
        self._allow_forward_entities_env = (cfg_get_str("ads_allow_forward_entities", "ADS_ALLOW_FORWARD_ENTITIES", "on") or "on").strip().lower()
        # اگر فوروارد از کانال/گروه/بات «مجاز» باشد، حداقل طول کپشن (سخت‌گیرانه‌تر)
        self._forward_caption_min_len_env = cfg_get_int("ads_forward_caption_min_len", "ADS_FORWARD_CAPTION_MIN_LEN", 20)
        # مهلت حذف برای فورواردِ بدون کپشن (کوتاه‌تر)
        self._forward_grace_sec_env = cfg_get_int("ads_forward_grace_sec", "ADS_FORWARD_GRACE_SEC", 120)
        
        # اجازهٔ «ریپلای به مدیای بدون کپشن» به عنوان کپشن؟
        self._allow_reply_as_caption_env = (cfg_get_str("ads_allow_reply_as_caption", "ADS_ALLOW_REPLY_AS_CAPTION", "on") or "on").strip().lower()
        
        
        
        # --- تنظیمات UX/Throttle برای اخطار کپشن کوتاه و re-open ---
        self._short_warn_cooldown_sec_env = cfg_get_int("ads_short_warn_cooldown_sec", "ADS_SHORT_WARN_COOLDOWN_SEC", 20)
        self._reoffend_grace_sec_env = cfg_get_int("ads_reoffend_grace_sec", "ADS_REOFFEND_GRACE_SEC", 60)
        self._reoffend_cooldown_sec = cfg_get_int("ads_reoffend_cooldown_sec", "ADS_REOFFEND_COOLDOWN_SEC", 15)
        
        # جلوگیری از اسپم هشدار روی پیام‌های ادیت‌شده اما همچنان تبلیغاتی
        self._warn_edit_cooldown_sec_env = cfg_get_int("ads_warn_edit_cooldown_sec", "ADS_WARN_EDIT_COOLDOWN_SEC", 90)
        self._ad_warn_ts: Dict[Tuple[int, int], float] = {}      # آخرین زمان هشدار برای (chat_id, msg_id)
        self._ad_warn_msgid: Dict[Tuple[int, int], int] = {}     # پیام هشدار بات برای ویرایش/جمع کردن بعدی
        
        # prune memory for warn-dedup maps
        if len(self._ad_warn_ts) > 5000:
            cutoff = time.time() - max(60, self._warn_edit_cooldown_sec_env)
            self._ad_warn_ts = {k: ts for k, ts in self._ad_warn_ts.items() if ts >= cutoff}
            self._ad_warn_msgid = {k: mid for k, mid in self._ad_warn_msgid.items() if k in self._ad_warn_ts}


        # آخرین‌بار اخطار «کپشن کوتاه» به ازای (chat_id, msg_id)
        self._short_warn_ts: Dict[Tuple[int, int], float] = {}
        # آخرین‌بار re-open برای (chat_id, msg_id)
        self._reoffend_ts: Dict[Tuple[int, int], float] = {}

        # تنظیم اختیاری: رفتار پیام اخطار بعد از موفقیت (ویرایش به «✅» و حذف خودکار)
        self._warn_success_action_env = (cfg_get_str("ads_warn_success_action", "ADS_WARN_SUCCESS_ACTION", "edit") or "edit").strip().lower()
        self._warn_success_autodel_sec_env = cfg_get_int("ads_warn_success_autodel_sec", "ADS_WARN_SUCCESS_AUTODEL_SEC", 0)

        
        # وضعیت‌های موقت برای مدیای بدون کپشن: key=(chat_id, msg_id)
        self._pending_nocap: Dict[tuple, dict] = {}
        self._pending_tasks: Dict[tuple, asyncio.Task] = {}

        # نگاشت پیام اخطار → کلید پیامِ درانتظار (برای پشتیبانی ریپلای روی اخطار)
        # (chat_id, warn_msg_id) -> (chat_id, message_id)
        self._pending_nocap_by_warn: Dict[Tuple[int, int], tuple] = {}
        
        # نگهداری موقت «هشدار اینلاین در انتظار بستن» از مسیر ادیت کپشن
        # کلید: (chat_id, message_id) → مقدار: (warn_msg_id, by_user_id|None)
        self._deferred_warn_by_msg: Dict[Tuple[int, int], Tuple[int, int | None]] = {}

        # Flowise timeouts (tuple: connect, read)
        self._flowise_connect_timeout = _int_env("FLOWISE_CONNECT_TIMEOUT", 5)
        self._flowise_read_timeout = _int_env("FLOWISE_READ_TIMEOUT", 75)

        # Fallback for autoclean default from ENV (instead of hard-coded 120)
        self._autoclean_sec_env = cfg_get_int("ads_autoclean_sec", "ADS_AUTOCLEAN_SEC", 120)

        self._min_gap_sec = cfg_get_int("ads_min_gap_sec", "ADS_MIN_GAP_SEC", 2)


        # Rate-limit per chat
        self._last_run_ts_per_chat: Dict[int, float] = {}

        # کش ادمین‌های گروه (۵ دقیقه)
        self._admins_cache: Dict[int, Tuple[float, set]] = {}
        self._admins_ttl_sec: int = 300
        # cache for media-group captions: key=(chat_id, media_group_id) -> (ts, caption)
        self._mg_caption_cache: Dict[tuple, Tuple[float, str]] = {}
        self._mg_caption_ttl_sec: int = 172800  # 48 ساعت ttl

        # ضدتکرار پیام/آلبوم با TTL
        self._seen_messages: Dict[Tuple[int, int], float] = {}
        self._seen_media_groups: Dict[Tuple[int, str], float] = {}

        # --- فقط برای «آلبومِ بدون کپشن»: یکبار هشدار به‌ازای هر media_group
        self._seen_mg_nocap: Dict[Tuple[int, str], float] = {}  # (chat_id, mgid) -> ts
        # نگاشت آلبوم→کلید پیام درانتظار (تا اگر کاربر به هر آیتمی ریپلای داد، همان درانتظار لغو شود)
        self._pending_nocap_by_mgid: Dict[Tuple[int, str], tuple] = {}  # (chat_id, mgid) -> (chat_id, message_id)

        self._dedup_ttl_sec: int = 600  # 10 دقیقه



        # کش ساده whitelist
        self._wl_users_cache: Dict[Tuple[int, int], bool] = {}
        self._wl_domains_cache: Dict[Tuple[int, str], bool] = {}

        self._mute_hours_env = cfg_get_int("ads_mute_hours", "ADS_MUTE_HOURS", 100)

        # لیست پیام‌های هر آلبوم در حال انتظار (برای حذف گروهی)
        self._pending_album_msgs: Dict[Tuple[int, str], List[int]] = {}
        
        # نگهداری موقت شناسه‌های آلبوم‌های موفق (برای دکمه سکوت ادمین)
        self._successful_albums: Dict[Tuple[int, int], Dict] = {}
        self._successful_albums_ttl_sec: int = 1800 # 30 دقیقه
        
    # ---------- bot_config (DB) ----------
    
    def chat_get_config(self, chat_id: int, key: str) -> Optional[str]:
        try:
            with self.get_db_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT value FROM chat_config WHERE chat_id=%s AND key=%s",
                    (chat_id, key),
                )
                row = cur.fetchone()
                return row[0] if row else None
        except Exception:
            return None


    def chat_set_config(self, chat_id: int, key: str, value: str):
        try:
            with self.get_db_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO chat_config (chat_id, key, value)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (chat_id, key)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """, (chat_id, key, value))
                conn.commit()
        except Exception:
            pass
    
    def chat_autoclean_sec(self, chat_id: int) -> int:
        """Delay for auto-delete in seconds; 0/None means disabled. Fallback: ENV ADS_AUTOCLEAN_SEC."""
        v = self.chat_get_config(chat_id, "ads_autoclean_sec")
        try:
            return int(v) if v is not None else int(getattr(self, "_autoclean_sec_env", 120))
        except Exception:
            return int(getattr(self, "_autoclean_sec_env", 120))


    def chat_mute_hours(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_mute_hours")
        try:
            return int(v) if v is not None else self._mute_hours_env
        except Exception:
            return self._mute_hours_env



    async def _delete_after(self, bot, chat_id: int, message_id: int, delay: int):
        import asyncio
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            log.exception("AdsGuard: delete_message failed",
                          extra={"chat_id": chat_id, "message_id": message_id})
    
    
    
    async def _close_warn_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, warn_msg_id: int, target_user_id: int | None = None):
        """
        بستن پیام اخطار بات پس از موفقیت کاربر در افزودن کپشن:
        - اگر action=edit: پیام به «✅ ...» ادیت و سپس AutoDelete می‌شود
        - اگر action=delete: پیام اخطار فوراً حذف می‌شود
        """
        try:
            action = self.chat_warn_success_action(chat_id)
            if action == "edit":
                try:
                    # بعد از موفقیت: متنِ تشکر + دو دکمه (راهنما + سکوت)
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    # مدت سکوت را از DB بخوان تا روی دکمه و متن‌ها نشان دهیم
                    hours = max(1, int(self.chat_mute_hours(chat_id) or 100))
                    
                    buttons = [
                        [InlineKeyboardButton(t("ads.info.button", chat_id=chat_id), callback_data="adsw:info")]
                    ]
                    if target_user_id:
                        buttons.append([
                            InlineKeyboardButton(
                                tn("ads.mute.button.one", "ads.mute.button.many", hours, chat_id=chat_id),
                                callback_data=f"adsw:mute:{int(target_user_id)}"
                            )
                        ])
                    keyboard = InlineKeyboardMarkup(buttons)
                    
                    await context.bot.edit_message_text(
                        t("ads.caption.received.ok", chat_id=chat_id),
                        chat_id=chat_id,
                        message_id=warn_msg_id,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard
                    )

                except Exception as e:
                    # محتمل: Bot ادمین نیست/پیام قبلاً پاک یا خیلی قدیمی است
                    log.debug("ads_warn edit_message_text failed: chat_id=%s msg_id=%s err=%s",
                        chat_id, warn_msg_id, e)

            
                sec = self.chat_warn_success_autodel_sec(chat_id)
                if sec and sec > 0:
                    context.application.create_task(self._delete_after(context.bot, chat_id, warn_msg_id, sec))
            else:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=warn_msg_id)
                except Exception as e:
                    # حذف مستقیم پیام اخطار ممکن است محدودیت داشته باشد؛ در DEBUG ثبت می‌کنیم
                    log.debug("ads_warn delete_message failed: chat_id=%s msg_id=%s err=%s",
                        chat_id, warn_msg_id, e)


        except Exception as e:
            log.exception("close_warn_message failed: %s", e)


    
    def _get_config(self, key: str) -> Optional[str]:
        try:
            with self.get_db_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT value FROM bot_config WHERE key=%s", (key,))
                r = cur.fetchone()
                return r[0] if r else None
        except Exception:
            return None

    def _set_config(self, key: str, value: str):
        try:
            with self.get_db_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_config (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                """, (key, value))
                conn.commit()
        except Exception:
            pass

    # ---------- Runtime config ----------
    def feature_on(self) -> bool:
        v = self._get_config("ads_feature")
        if v is None:
            v = self._feature_env
        return str(v).lower() in ("1", "true", "on", "yes")

    def chatflow_id(self) -> str:
        return self._get_config("ads_chatflow_id") or self._chatflow_id_env

    def threshold(self) -> float:
        v = self._get_config("ads_threshold")
        try:
            return float(v) if v is not None else self._threshold_env
        except Exception:
            return self._threshold_env

    def max_fewshots(self) -> int:
        v = self._get_config("ads_max_fewshots")
        try:
            return int(v) if v is not None else self._max_fewshots_env
        except Exception:
            return self._max_fewshots_env

    def action(self) -> str:
        v = self._get_config("ads_action") or self._action_env
        v = (v or "").strip().lower()
        return v if v in ("warn", "delete", "none") else "none"

    def min_gap_sec(self) -> int:
        v = self._get_config("ads_min_gap_sec")
        try:
            return int(v) if v is not None else self._min_gap_sec
        except Exception:
            return self._min_gap_sec
    
    # ---------- Per-Chat runtime config ----------
    def chat_feature_on(self, chat_id: int) -> bool:
        v = self.chat_get_config(chat_id, "ads_feature")
        if v is None:
            v = self._feature_env
        return str(v).lower() in ("1","true","on","yes")

    def chat_chatflow_id(self, chat_id: int) -> str:
        return self.chat_get_config(chat_id, "ads_chatflow_id") or self._chatflow_id_env

    def chat_threshold(self, chat_id: int) -> float:
        v = self.chat_get_config(chat_id, "ads_threshold")
        try:
            return float(v) if v is not None else self._threshold_env
        except Exception:
            return self._threshold_env

    def chat_max_fewshots(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_max_fewshots")
        try:
            return int(v) if v is not None else self._max_fewshots_env
        except Exception:
            return self._max_fewshots_env
    
    def chat_examples_hardcap(self, chat_id: int) -> int:
        """
        سقف تعداد نمونه‌های ذخیره‌شده برای هر گروه (DB-first → ENV → default=50)
        """
        v = self.chat_get_config(chat_id, "ads_examples_hardcap")
        try:
            return int(v) if v is not None else self._examples_hardcap_env
        except Exception:
            return self._examples_hardcap_env

    def chat_examples_select_mode(self, chat_id: int) -> str:
        """
        انتخاب روش برداشتن few-shots:
        latest   → فقط جدیدترین‌ها (رفتار فعلی)
        balanced → بالانس AD/NOT_AD تا حد ممکن، سپس مرتب‌سازی کلی برحسب id DESC
        """
        v = self.chat_get_config(chat_id, "ads_examples_select")
        v = (v or "").strip().lower() if v is not None else "latest"
        return v if v in ("latest", "balanced") else "latest"


    def chat_action(self, chat_id: int) -> str:
        v = self.chat_get_config(chat_id, "ads_action") or self._action_env
        v = (v or "").strip().lower()
        return v if v in ("warn","delete","none") else "none"

    def chat_min_gap_sec(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_min_gap_sec")
        try:
            return int(v) if v is not None else self._min_gap_sec
        except Exception:
            return self._min_gap_sec
    
    
    def chat_reply_exempt(self, chat_id: int) -> bool:
        v = self.chat_get_config(chat_id, "ads_reply_exempt")
        if v is None:
            v = self._reply_exempt_env
        return str(v).lower() in ("1","true","on","yes")

    def chat_reply_exempt_maxlen(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_reply_exempt_maxlen")
        try:
            return int(v) if v is not None else self._reply_exempt_maxlen_env
        except Exception:
            return self._reply_exempt_maxlen_env

    def chat_reply_exempt_allow_contact(self, chat_id: int) -> bool:
        v = self.chat_get_config(chat_id, "ads_reply_exempt_allow_contact")
        if v is None:
            v = self._reply_exempt_allow_contact_env
        return str(v).lower() in ("1","true","on","yes")

    def chat_reply_exempt_contact_maxlen(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_reply_exempt_contact_maxlen")
        try:
            return int(v) if v is not None else self._reply_exempt_contact_maxlen_env
        except Exception:
            return self._reply_exempt_contact_maxlen_env

    
    def chat_caption_min_len(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_caption_min_len")
        try:
            return int(v) if v is not None else self._caption_min_len_env
        except Exception:
            return self._caption_min_len_env
    
    def chat_nocap_grace_sec(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_nocap_grace_sec")
        try:
            return int(v) if v is not None else self._nocap_grace_sec_env
        except Exception:
            return self._nocap_grace_sec_env
    
    def chat_allow_forward_entities(self, chat_id: int) -> bool:
        v = self.chat_get_config(chat_id, "ads_allow_forward_entities")
        if v is None:
            v = self._allow_forward_entities_env
        return str(v).lower() in ("1", "true", "on", "yes")
    
    def chat_forward_caption_min_len(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_forward_caption_min_len")
        try:
            return int(v) if v is not None else self._forward_caption_min_len_env
        except Exception:
            return self._forward_caption_min_len_env
    
    def chat_forward_grace_sec(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_forward_grace_sec")
        try:
            return int(v) if v is not None else self._forward_grace_sec_env
        except Exception:
            return self._forward_grace_sec_env
    
    def chat_allow_reply_as_caption(self, chat_id: int) -> bool:
        v = self.chat_get_config(chat_id, "ads_allow_reply_as_caption")
        if v is None:
            v = self._allow_reply_as_caption_env
        return str(v).lower() in ("1", "true", "on", "yes")
    
    
    def chat_short_warn_cooldown_sec(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_short_warn_cooldown_sec")
        try: return int(v) if v is not None else self._short_warn_cooldown_sec_env
        except Exception: return self._short_warn_cooldown_sec_env

    def chat_reoffend_grace_sec(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_reoffend_grace_sec")
        try: return int(v) if v is not None else self._reoffend_grace_sec_env
        except Exception: return self._reoffend_grace_sec_env

    def chat_reoffend_cooldown_sec(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_reoffend_cooldown_sec")
        try: return int(v) if v is not None else self._reoffend_cooldown_sec
        except Exception: return self._reoffend_cooldown_sec
    
    
    
    def chat_warn_edit_cooldown_sec(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_warn_edit_cooldown_sec")
        try:
            return int(v) if v is not None else self._warn_edit_cooldown_sec_env
        except Exception:
            return self._warn_edit_cooldown_sec_env

    
    
    
    def chat_warn_success_action(self, chat_id: int) -> str:
        v = self.chat_get_config(chat_id, "ads_warn_success_action") or self._warn_success_action_env
        v = (v or "").strip().lower()
        return v if v in ("edit", "delete") else "edit"

    def chat_warn_success_autodel_sec(self, chat_id: int) -> int:
        v = self.chat_get_config(chat_id, "ads_warn_success_autodel_sec")
        try:
            return int(v) if v is not None else self._warn_success_autodel_sec_env
        except Exception:
            return self._warn_success_autodel_sec_env

    
    
    # داخل کلاس AdsGuard  (به اندازه‌ی بقیه متدهای کلاس تورفتگی بدهید)
    async def on_edited_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        اگر کپشن/متنِ پیام ادیت شد:
        - اگر حداقل طول لازم را دارد: در صورت pending تایمر حذف لغو شود و پیام اخطار بسته شود؛ سپس به watchdog برود.
        - اگر کوتاه/خالی است:
            • اگر pending است → فقط یک تذکر کوتاه (throttle) بده.
            • اگر pending نبود (یعنی قبلاً موفق شده بود) → re-open با مهلت کوتاه و تایمر حذف.
        """
        msg = update.effective_message
        chat = update.effective_chat
        if not (msg and chat):
            return

        key = (chat.id, msg.message_id)
        pend = self._pending_nocap.get(key)
        was_pending = bool(pend)

        # متن/کپشن فعلی
        text = (msg.text or msg.caption or "").strip()
        # اگر عضو آلبوم است و کپشن خودش خالی است، از کش کپشن آلبوم بخوان
        if not text and getattr(msg, "media_group_id", None):
            try:
                text = self.caption_for_media_group(chat.id, msg.media_group_id) or ""
            except Exception:
                text = ""

        # آستانهٔ لازم: برای فورواردِ مجاز سخت‌تر، وگرنه عادی
        is_ent_fwd, _ = self._is_forward_from_entity(msg)
        need_len = self.chat_forward_caption_min_len(chat.id) if is_ent_fwd else self.chat_caption_min_len(chat.id)
        
        # معیار جدید: «تعداد کلمه»
        if count_words(text) >= max(1, need_len):
            if was_pending:
                # 1) لغو تایمر حذف
                try:
                    pending_task = self._pending_tasks.pop(key, None)
                    if pending_task:
                        pending_task.cancel()
                except Exception:
                    pass
                rec = self._pending_nocap.pop(key, None)
                warn_mid = (rec or {}).get("warn_msg_id")
                # 🧹 پاک‌سازی نگاشت‌های کمکی
                try:
                    mgid_val = (rec or {}).get("mgid")
                    if mgid_val:
                        self._pending_nocap_by_mgid.pop((chat.id, mgid_val), None)
                        album_key = (chat.id, mgid_val)
                        msg_ids = self._pending_album_msgs.pop(album_key, [])
                        if msg_ids and warn_mid:
                            success_key = (chat.id, int(warn_mid))
                            # در حالت ویرایش، reply_id وجود ندارد (None)
                            self._successful_albums[success_key] = {
                                "media_ids": msg_ids,
                                "reply_id": None,
                                "ts": time.time(),
                            }

                except Exception:
                    pass
                try:
                    warn_mid_map = (rec or {}).get("warn_msg_id")
                    if warn_mid_map:
                        self._pending_nocap_by_warn.pop((chat.id, int(warn_mid_map)), None)
                except Exception:
                    pass

                # ❗️به‌جای بستن فوری، بستن را تا بعد از نتیجهٔ تشخیص به تعویق می‌اندازیم
                if warn_mid:
                    try:
                        self._deferred_warn_by_msg[(chat.id, msg.message_id)] = (
                            int(warn_mid),
                            (rec or {}).get("by"),
                        )
                    except Exception:
                        # اگر warn_mid قابل تبدیل نبود، حداکثر فقط by را نگه می‌داریم
                        self._deferred_warn_by_msg[(chat.id, msg.message_id)] = (warn_mid, (rec or {}).get("by"))

            # 2) بازپردازش امن (چه pending بوده چه نبوده)
            try:
                self._seen_messages.pop((chat.id, msg.message_id), None)
            except Exception:
                pass

            # با متن تازه دوباره وارد پایپ‌لاین شو (watchdog تشخیص را انجام می‌دهد)
            await self.watchdog(update, context)
            return


        # --- اینجا یعنی کپشن/متن کوتاه یا خالی است ---
        now = time.time()

        if was_pending:
            # کپشن هنوز کوتاه/خالی است → همـان پیام هشدار اینلاین را ادیت کن (نه پیام جدید)
            cd = self.chat_short_warn_cooldown_sec(chat.id)
            last = self._short_warn_ts.get(key, 0)
            if now - last >= cd:
                self._short_warn_ts[key] = now
                try:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    warn_mid = (pend or {}).get("warn_msg_id")
                    if warn_mid:
                        buttons = [[InlineKeyboardButton("🧩 راهنما / مثال", callback_data=f"adsw:guide:{msg.message_id}")]]
                        keyboard = InlineKeyboardMarkup(buttons)
                        await context.bot.edit_message_text(
                            tn(
                                "ads.caption.too_short.one",
                                "ads.caption.too_short.many",
                                max(1, need_len),
                                chat_id=chat.id
                            ),
                            chat_id=chat.id,
                            message_id=warn_mid,
                            parse_mode=ParseMode.HTML,
                            reply_markup=keyboard
                        )

                except Exception:
                    pass
            return

        # [FIX]: re-open فقط برای «کپشن واقعی» یا «ریپلای به مدیای واقعاً pending» انجام شود
        has_media = bool(
            getattr(msg, "photo", None)
            or getattr(msg, "video", None)
            or getattr(msg, "animation", None)
            or getattr(msg, "document", None)
        )
        if not has_media:
            rp = getattr(msg, "reply_to_message", None)
            parent_key = (chat.id, rp.message_id) if rp else None
            # اگر این ریپلای به یک «مدیای pending بدون کپشن» نیست، re-open نکن
            if not (rp and parent_key in self._pending_nocap):
                return

        # قبلاً pending نبود ولی الان کوتاه/خالی شده → re-open با cooldown
        cd = self.chat_reoffend_cooldown_sec(chat.id)
        last = self._reoffend_ts.get(key, 0)
        if now - last < cd:
            return  # جلوگیری از اسپم روی ادیت‌های پیاپی
        self._reoffend_ts[key] = now




        grace = max(0, int(self.chat_reoffend_grace_sec(chat.id) or 60))
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            buttons = [[InlineKeyboardButton(t("ads.help.hint", chat_id=chat.id), callback_data=f"adsw:guide:{msg.message_id}")]]
            keyboard = InlineKeyboardMarkup(buttons)
            wm = await msg.reply_text(
                t("ads.nocap.reopen.text", chat_id=chat.id, grace=grace),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )


            # ثبت pending جدید
            self._pending_nocap[key] = {
                "by": (getattr(update.effective_user, "id", None)),
                "grace": grace,
                "ts": time.time(),
                "is_forward_entity": bool(self._is_forward_from_entity(msg)[0]),
                "mgid": str(getattr(msg, "media_group_id", "") or "") or None,
                "warn_msg_id": getattr(wm, "message_id", None),
            }
            # نگاشت اخطار → کلید
            try:
                warn_mid = getattr(wm, "message_id", None)
                if warn_mid:
                    self._pending_nocap_by_warn[(chat.id, int(warn_mid))] = key
            except Exception:
                pass

            # تایمر حذف برای re-open (تابع محلی با دسترسی به متغیرهای بالا)
            async def _arm_delete_reopen():
                try:
                    await asyncio.sleep(grace)
                    rec2 = self._pending_nocap.get(key)
                    if rec2:
                        await context.bot.delete_message(chat_id=chat.id, message_id=msg.message_id)
                        self._pending_nocap.pop(key, None)
                        mgid_val = rec2.get("mgid")
                        if mgid_val:
                            self._pending_nocap_by_mgid.pop((chat.id, mgid_val), None)
                        # 🧹 پاک‌سازی نگاشت اخطار
                        try:
                            warn_mid = rec2.get("warn_msg_id")
                            if warn_mid:
                                self._pending_nocap_by_warn.pop((chat.id, int(warn_mid)), None)
                        except Exception:
                            pass

                except Exception:
                    # اگر حذف نشد، فقط state را تمیز کن
                    try:
                        self._pending_nocap.pop(key, None)
                    except Exception:
                        pass

            # زمان‌بندی اجرای حذف (خارج از تابع، درست بعد از تعریف)
            self._pending_tasks[key] = context.application.create_task(_arm_delete_reopen())

        except Exception:
            # اگر هر مرحله‌ای در ساخت هشدار/ثبت state/آرمه کردن تایمر خطا داد، بات کرش نکند
            pass
    
    
    async def on_warn_buttons(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """هندلر دکمهٔ اینلاین راهنما: adsw:guide:<orig_msg_id>"""
        query = update.callback_query
        if not query:
            return
    
        # NEW: گرفتن chat از پیام مربوط به دکمه
        msg_obj = getattr(query, "message", None)
        chat = getattr(msg_obj, "chat", None)
        if not chat:
            await query.answer()
            return
    
        data = str(getattr(query, "data", "") or "")
        if data.startswith("adsw:guide:"):
            # نمایش متن راهنما از i18n
            await query.answer(
                t("ads.help.caption_alert", chat_id=chat.id),
                show_alert=True
            )
            return
    
        # سایر داده‌ها: پاسخ خالی تا UI گیر نکند
        await query.answer()


    async def on_warn_mute(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        دکمهٔ ادمینی: adsw:mute:<user_id>
        - کاربر را ساکت می‌کند، کل پست (تکی یا آلبوم) و پیام ریپلای احتمالی را حذف می‌کند.
        """
        query = update.callback_query
        if not query: return

        data = str(getattr(query, "data", "") or "")
        if not data.startswith("adsw:mute:"):
            await query.answer(); return

        msg_obj = getattr(query, "message", None)
        chat = getattr(msg_obj, "chat", None)
        if not chat:
            await query.answer(); return

        try:
            target_uid = int(data.split(":")[2])
            hours = max(1, int(self.chat_mute_hours(chat.id) or 100))
        except Exception:
            await query.answer("دادهٔ دکمه نامعتبر است.", show_alert=True); return

        # ... (بخش چک کردن دسترسی ادمین بدون تغییر باقی می‌ماند) ...
        try:
            me = int(query.from_user.id)
            if me == int(TG_ANON): is_admin = True
            else:
                cm = await context.bot.get_chat_member(chat.id, me)
                is_admin = str(getattr(cm, "status", "")) in ("administrator", "creator")
            if not is_admin:
                await query.answer("این دکمه فقط برای مدیران است.", show_alert=True); return
        except Exception:
            await query.answer("نتوانستم سطح دسترسی شما را بررسی کنم.", show_alert=True); return
        
        try:
            target_cm = await context.bot.get_chat_member(chat.id, target_uid)
            if str(getattr(target_cm, "status", "")) in ("administrator", "creator"):
                await query.answer("نمی‌توان مدیر/مالک را سایلنت کرد.", show_alert=True); return
        except Exception:
            await query.answer("نتوانستم وضعیت کاربر هدف را بررسی کنم.", show_alert=True); return

        # --- [منطق جدید] جمع‌آوری تمام پیام‌ها برای حذف ---
        msgs_to_delete = []
        success_key = (chat.id, query.message.message_id)
        
        album_data = self._successful_albums.pop(success_key, None)
        if album_data:
            # حالت آلبوم یا ریپلای
            if album_data.get("media_ids"):
                msgs_to_delete.extend(album_data["media_ids"])
            if album_data.get("reply_id"):
                msgs_to_delete.append(album_data["reply_id"])
        else:
            # حالت پیام تکی (که با ویرایش کپشن گرفته)
            target_msg = getattr(query.message, "reply_to_message", None)
            if target_msg:
                msgs_to_delete.append(target_msg.message_id)

        # --- اجرای عملیات ---
        try:
            from datetime import datetime, timedelta, timezone
            from telegram import ChatPermissions
            import html

            # 1. ساکت کردن کاربر
            until = datetime.now(timezone.utc) + timedelta(hours=hours)
            perms = ChatPermissions(can_send_messages=False, can_send_polls=False, can_send_other_messages=False)
            await context.bot.restrict_chat_member(chat_id=chat.id, user_id=target_uid, permissions=perms, until_date=until)

            # 2. حذف پیام(های) کاربر
            if msgs_to_delete:
                try:
                    await context.bot.delete_messages(chat_id=chat.id, message_ids=msgs_to_delete)
                except Exception as del_err:
                    log.warning(f"Could not delete messages in bulk: {del_err}; falling back to single delete.")
                    for _mid in msgs_to_delete:
                        try:
                            await context.bot.delete_message(chat_id=chat.id, message_id=_mid)
                        except Exception:
                            pass

            # 3. ویرایش پیام ربات
            user_name = (target_cm.user.first_name or "").strip() \
                or t("user.fallback_name", chat_id=chat.id, user_id=target_uid)
            edited_text = tn(
                "ads.mute.edited.one",
                "ads.mute.edited.many",
                hours,
                chat_id=query.message.chat.id,
                user_name=html.escape(user_name)
            )

            await context.bot.edit_message_text(text=edited_text, chat_id=chat.id, message_id=query.message.message_id, reply_markup=None)

            # 4. زمان‌بندی حذف خودکار برای پیام ویرایش‌شده
            autoclean_delay = self.chat_autoclean_sec(chat.id)
            if autoclean_delay > 0:
                context.application.create_task(self._delete_after(context.bot, chat.id, query.message.message_id, autoclean_delay))

            await query.answer()

        except Exception as e:
            await query.answer(f"عملیات ناموفق بود: {e}", show_alert=True)
        
        # Housekeeping: prune old entries by TTL
        now_ts = time.time()
        ttl = getattr(self, "_successful_albums_ttl_sec", 1800)
        self._successful_albums = {
            k: v for k, v in self._successful_albums.items()
            if isinstance(v, dict) and (now_ts - float(v.get("ts", now_ts)) <= ttl)
        }

    
    
    async def on_warn_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # هندلر دکمه "ℹ️ توضیحات": adsw:info
        query = update.callback_query
        if not query or getattr(query, "data", "") != "adsw:info":
            return
    
        hours = int(self.chat_mute_hours(query.message.chat.id) or 100)
    
        # متن باید < 200 کاراکتر و بدون HTML باشد
        text = tn(
            "ads.mute.info.alert.one",
            "ads.mute.info.alert.many",
            hours,
            chat_id=query.message.chat.id
        )
        
        # تضمین سقف ۲۰۰ کاراکتر برای answerCallbackQuery
        if len(text) > 200:
            text = text[:197] + "..."
        
        await query.answer(text, show_alert=True, cache_time=0)
            
    
    # ---------- DB schema ----------
    def ensure_tables(self):
        """ایجاد جداول موردنیاز (idempotent)"""
        with self.get_db_conn() as conn, conn.cursor() as cur:
            # bot_config جهت استقلال ماژول
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_config (
                    chat_id BIGINT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (chat_id, key)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ads_examples (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    text TEXT NOT NULL,
                    user_id BIGINT,
                    label TEXT NOT NULL DEFAULT 'AD',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            # سازگاری با جداول قبلی (اگر قبلاً ساخته شده‌اند)
            cur.execute("ALTER TABLE ads_examples ADD COLUMN IF NOT EXISTS label TEXT NOT NULL DEFAULT 'AD';")
            cur.execute("ALTER TABLE ads_examples ADD COLUMN IF NOT EXISTS chat_id BIGINT;")
            # ایندکس پیشنهادی برای بهبود سرعت لیست/بازیابی نمونه‌های هر گروه
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_examples_chat_created ON ads_examples (chat_id, created_at DESC);")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ads_decisions (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    user_id BIGINT,
                    text TEXT,
                    is_ad BOOLEAN NOT NULL,
                    score DOUBLE PRECISION,
                    reason TEXT,
                    decided_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ads_whitelist_users (
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    added_by BIGINT,
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (chat_id, user_id)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ads_whitelist_domains (
                    chat_id BIGINT NOT NULL,
                    domain TEXT NOT NULL,
                    added_by BIGINT,
                    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (chat_id, domain)
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_examples_created ON ads_examples (created_at DESC);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_decisions_chat_msg ON ads_decisions (chat_id, message_id);")
            
            # برای آمار و شبیه‌سازی: لیبل خام + ایندکس زمانی
            cur.execute("ALTER TABLE ads_decisions ADD COLUMN IF NOT EXISTS label TEXT;")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_decisions_chat_time ON ads_decisions (chat_id, decided_at DESC);")

            conn.commit()

    # ---------- whitelist & admin helpers ----------
    async def is_group_admin(self, bot, chat_id: int, user_id: int) -> bool:
        """Check admin with 5-min TTL cache."""
        now = time.time()
        cache = self._admins_cache.get(chat_id)
        if not cache or (now - cache[0] > self._admins_ttl_sec):
            try:
                admins = await bot.get_chat_administrators(chat_id)
                admin_ids = {adm.user.id for adm in admins if getattr(adm, 'user', None)}
                self._admins_cache[chat_id] = (now, admin_ids)
            except Exception:
                admin_ids = set()
                self._admins_cache[chat_id] = (now, admin_ids)
        else:
            admin_ids = cache[1]
        return user_id in admin_ids




    @staticmethod
    def _is_forward_from_entity(msg) -> tuple[bool, str]:
        """
        تشخیص اینکه پیام «فوروارد از کانال/گروه/بات» است یا نه.
        خروجی: (is_entity_forward, origin_type)
          origin_type یکی از: 'channel' | 'group' | 'supergroup' | 'bot' | 'user' | ''
        """
        try:
            # PTB کلاسیک
            if getattr(msg, "forward_from_chat", None):
                cht = msg.forward_from_chat
                # bot channel/group/supergroup
                if getattr(cht, "type", None) in ("channel", "group", "supergroup"):
                    return True, str(cht.type)
            if getattr(msg, "forward_from", None):
                # از کاربر عادی (پی‌وی)
                return False, "user"
            # اگر نسخه‌های جدید: forward_origin
            fo = getattr(msg, "forward_origin", None)
            if fo:
                # انواع جدید ممکن است name/type داشته باشند
                t = getattr(fo, "type", "") or getattr(fo, "sender_user", None) and "user" or ""
                if t in ("channel", "group", "supergroup", "bot"):
                    return True, t
                if t == "user":
                    return False, "user"
        except Exception:
            pass
        return False, ""






    def wl_user_add(self, chat_id: int, user_id: int, added_by: Optional[int] = None) -> bool:
        with self.get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ads_whitelist_users (chat_id, user_id, added_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id, user_id) DO NOTHING
            """, (chat_id, user_id, added_by))
            conn.commit()
            self._wl_users_cache[(chat_id, user_id)] = True
            return True

    def wl_user_del(self, chat_id: int, user_id: int) -> bool:
        with self.get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ads_whitelist_users WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
            conn.commit()
            self._wl_users_cache[(chat_id, user_id)] = False
            return True

    def wl_user_has(self, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        if key in self._wl_users_cache:
            return self._wl_users_cache[key]
        try:
            with self.get_db_conn() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM ads_whitelist_users WHERE chat_id=%s AND user_id=%s", (chat_id, user_id))
                ok = (cur.fetchone() is not None)
                self._wl_users_cache[key] = ok
                return ok
        except Exception:
            return False

    def wl_domain_add(self, chat_id: int, domain: str, added_by: Optional[int] = None) -> bool:
        domain = (domain or '').lower().strip()
        if domain.startswith('http://') or domain.startswith('https://'):
            try:
                from urllib.parse import urlparse
                domain = urlparse(domain).hostname or domain
            except Exception:
                pass
        with self.get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ads_whitelist_domains (chat_id, domain, added_by)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id, domain) DO NOTHING
            """, (chat_id, domain, added_by))
            conn.commit()
            self._wl_domains_cache[(chat_id, domain)] = True
            return True

    def wl_domain_del(self, chat_id: int, domain: str) -> bool:
        domain = (domain or '').lower().strip()
        with self.get_db_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM ads_whitelist_domains WHERE chat_id=%s AND domain=%s", (chat_id, domain))
            conn.commit()
            self._wl_domains_cache[(chat_id, domain)] = False
            return True

    def wl_domains_list(self, chat_id: int, limit: int = 50) -> List[Tuple[str, str]]:
        with self.get_db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT domain, added_at::text AS ts FROM ads_whitelist_domains
                WHERE chat_id=%s
                ORDER BY added_at DESC
                LIMIT %s
            """, (chat_id, limit))
            rows = cur.fetchall() or []
            return [(str(r['domain']), str(r['ts'])) for r in rows]

    def wl_users_list(self, chat_id: int, limit: int = 50) -> List[Tuple[int, str]]:
        with self.get_db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT user_id, added_at::text AS ts FROM ads_whitelist_users
                WHERE chat_id=%s
                ORDER BY added_at DESC
                LIMIT %s
            """, (chat_id, limit))
            rows = cur.fetchall() or []
            return [(int(r['user_id']), str(r['ts'])) for r in rows]

    @staticmethod
    def _extract_domains(text: str) -> List[str]:
        if not text:
            return []
        from urllib.parse import urlparse
        cand = set()
        for m in re.findall(r"https?://[^\s\)\]\>\<]+", text, flags=re.IGNORECASE):
            try:
                host = urlparse(m).hostname
            except Exception:
                host = None
            if host:
                cand.add(host.lower())
        for m in re.findall(
            r"\b([A-Za-z0-9\u0600-\u06FF][A-Za-z0-9\.\-\u0600-\u06FF]*\.[A-Za-z\u0600-\u06FF]{2,})(?:/\S*)?\b",
            text,
            flags=re.IGNORECASE,
        ):

            cand.add(m.split('/')[0].lower())
        return sorted(cand)
    
    
    @staticmethod
    def _is_request_intent(text: str) -> bool:
        """تشخیص نیتِ درخواست/یافتن/راهنمایی در متن والد (reply_to)."""
        if not text:
            return False
            
        # حذف نیم‌فاصله/علائم جهت‌دهی (ZWNJ/RLM/LRM) + یکدست‌سازی فاصله‌ها
        norm = re.sub(r"[\u200c\u200f\u200e]", " ", (text or "").strip())
        s = re.sub(r"\s+", " ", norm)

        # الگوهای پایهٔ درخواست/راهنمایی (حالت‌های محاوره‌ای + نیم‌فاصله)
        base_patterns = [
            # میخوام/می‌خوام/میخواستم/می‌خواستم + املای رایجِ «میخاستم»
            r"می\s*خوام", r"میخوام", r"می\s*خواستم", r"میخواستم", r"میخاستم",
            r"نیاز(?:\s|‌)?دارم", r"دنبال",

            # از کجا ... (می‌تونم/می‌شه/بخرم/تهیه کنم/گیر بیارم/پیدا کنم)
            r"(?:از\s+)?کجا(?:ی)?(?:\s+\S+){0,8}\s+(?:می\s*تونم|می‌تونم|میتونم|می\s*شه|میشه|پیدا\s*کنم|بخرم|تهیه\s*کنم|گیر\s*بیارم|هست)",

            # کلی: «کجا» و «پیدا می‌شه/می‌تونم»
            r"کجا(?:ی)?",
            r"پیدا(?:\s|‌)?(?:می[شس]ه|می\s*تونم|می‌تونم)?",

            # راهنمایی/معرفی/پیشنهاد
            r"راهنمایی(?:\s+کنید)?", r"معرفی(?:\s+کنید)?", r"پیشنهاد(?:\s|‌)?بدید",

            # «چه/کدوم جنسی/مدلی ... استفاده کنیم/مناسبه/بهتره»
            r"(?:چه|کدوم)(?:\s+\S+){0,8}\s+(?:استفاده\s*کنیم|مناسبه|بهتره)",

            # «دارین ... بفرستین» یا «پی وی قیمت»
            r"دار(?:ید|ین|ی)(?:\s+\S+){0,6}\s+بفرس(?:تین|تید)",
            r"(?:پی\.?\s*وی|پیوی)\s*قیمت",

            # قیمت/هزینه بپرسه حتی بدون «؟»
            r"(?:قیمت|هزینه)\s*(?:بده|بدید|بدين|اعلام|لطفاً|لطفا|چنده|چقد(?:ر|ه))",
            r"(?:دونه(?:\s*ای)?|تکی)\s*چند",

            # «موجود دارید/داره؟»
            r"(?:دوستان|همکار(?:ان|ای)|بچه(?:‌| )?ها|رفقا)(?:\s+\S+){0,6}\s+دار(?:ید|ین|ی)\s*[\?؟]",
            r"(?:موجود|موجودی)\s+دار(?:ه|ید|ین|ن|ند)"
        ]

        # «کسی ...» با فاصلهٔ آزاد بین «کسی» و فعل/عبارت تا 10 واژه
        someone_help_patterns = [
            r"(?:ا(?:گه|گر)\s+)?کسی(?:\s+\S+){0,10}\s+(?:هست|نیست)",
            r"(?:ا(?:گه|گر)\s+)?کسی(?:\s+\S+){0,10}\s+اطلاع\s+دار(?:ه|ید|ین|ن|ند)",
            r"(?:ا(?:گه|گر)\s+)?کسی(?:\s+\S+){0,10}\s+سراغ\s+دار(?:ه|ید|ین|ن|ند)",
            r"(?:ا(?:گه|گر)\s+)?کسی(?:\s+\S+){0,10}\s+موجود\s+دار(?:ه|ید|ین|ن|ند)",
            r"کسی(?:\s+\S+){0,10}\s+دار(?:ه|ید|ی|ن|ند)",
            r"کسی(?:\s+\S+){0,10}\s+(?:می\s*تونه|می‌تونه|میتونه|بتونه|بتونید|بتونی|بتونن)",
            r"کسی(?:\s+\S+){0,10}\s+(?:انجام\s*می(?:ده|دهد)|می\s*کنه|می‌کنه|می\s*کنن|می‌کنند|میکنه|میکنن)",
            r"کسی(?:\s+\S+){0,10}\s+انجام\s+نمی(?:ده|دهد)\s*[\?؟]+",
            r"کسی(?:\s+\S+){0,10}\s+می\s*شنا(?:س|سی|سید|سن)(?:ه)?",
            r"کسی(?:\s+\S+){0,10}\s+ندار(?:ه|ید|ین|ن|ند)\s*[\?؟]"
        ]
        
        patterns = base_patterns + someone_help_patterns

        if any(re.search(p, s, flags=re.IGNORECASE) for p in patterns):
            return True
        if ("?" in s or "؟" in s) and re.search(r"(بخر|خرید|تهیه)", s, flags=re.IGNORECASE):
            return True
        return False

    @staticmethod
    def _has_contact_like(text: str) -> bool:
        """تشخیص سادهٔ شماره/لینک/آیدی/ایمیل در پیام."""
        if not text:
            return False
        s = (text or "").lower()
        if re.search(r"(\+?98|0)?9\d{9}", s):  # موبایل ایران
            return True
        if re.search(r"\b\+?\d[\d\s\-]{8,}\d\b", s):  # شماره عمومی
            return True
        if re.search(r"(https?://|www\.)\S+", s):  # لینک/دامنه
            return True
        if re.search(r"@\w{3,}", s) or "t.me/" in s:  # آیدی تلگرام
            return True
        if re.search(r"[\w\.-]+@[\w\.-]+\.[a-z]{2,}", s):  # ایمیل
            return True
        return False

    
    
    def caption_for_media_group(self, chat_id: int, media_group_id) -> str:
        """Return cached caption for a media group (album), if any, honoring TTL."""
        try:
            if not media_group_id:
                return ""
            key = (chat_id, str(media_group_id))
            val = self._mg_caption_cache.get(key)
            if not val:
                return ""
            ts, cap = val
            if time.time() - ts > self._mg_caption_ttl_sec:
                self._mg_caption_cache.pop(key, None)
                return ""
            return cap or ""
        except Exception:
            return ""

            
    # ---------- few-shot examples ----------
    def add_example(self, chat_id: int, text: str, user_id: Optional[int], label: str = "AD") -> Tuple[bool, str]:
        """
        افزودن نمونه AD/NOT_AD برای همین گروه.
        اگر سقف (hardcap) پر باشد، ثبت انجام نمی‌شود و (False, "hardcap_reached") برمی‌گرداند.
        """
        cap = self.chat_examples_hardcap(chat_id)
        with self.get_db_conn() as conn, conn.cursor() as cur:
            # شمارش نمونه‌های موجود این گروه
            cur.execute("SELECT COUNT(*) FROM ads_examples WHERE chat_id = %s", (chat_id,))
            row = cur.fetchone()
            cnt = int(row[0]) if row and row[0] is not None else 0
            if cnt >= cap:
                return False, "hardcap_reached"
            # ثبت نمونه‌ی جدید
            cur.execute(
                "INSERT INTO ads_examples (chat_id, text, user_id, label) VALUES (%s, %s, %s, %s)",
                (chat_id, text, user_id, label)
            )
            conn.commit()
            return True, ""

    def list_examples_full(self, chat_id: int, limit: int = 10) -> List[Tuple[int, str, str, str]]:
        """نسخهٔ کامل نمونه‌ها برای تزریق به مدل (بدون برش ۱۸۰ کاراکتری).
        خروجی: [(id, text, ts, label), ...]
        توجه: برای لیست‌کردن در UI همچنان از list_examples (با preview) استفاده می‌شود تا پیام‌ها کوتاه بمانند.
        """
        with self.get_db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT id,
                       text,
                       created_at::text AS ts,
                       label
                FROM ads_examples
                WHERE chat_id = %s
                ORDER BY id DESC
                LIMIT %s
            """, (chat_id, limit))
            rows = cur.fetchall() or []
            return [
                (int(r["id"]), str(r["text"]), str(r["ts"]), str(r["label"]))
                for r in rows
            ]
    

    def list_examples(self, chat_id: int, limit: int = 10) -> List[Tuple[int, str, str, str]]:
        """
        نسخه‌ی خلاصه برای نمایش در UI (پریویو ۱۸۰ کاراکتری).
        خروجی: [(id, preview, ts, label), ...]
        """
        with self.get_db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT id,
                       LEFT(text, 180) AS preview,
                       created_at::text AS ts,
                       label
                FROM ads_examples
                WHERE chat_id = %s
                ORDER BY id DESC
                LIMIT %s
            """, (chat_id, limit))
            rows = cur.fetchall() or []
            return [
                (int(r["id"]), str(r["preview"]), str(r["ts"]), str(r["label"]))
                for r in rows
            ]

    
    def list_examples_balanced(self, chat_id: int, limit: int = 10) -> List[Tuple[int, str, str, str]]:
        """
        نمونه‌های بالانس‌شده بین AD و NOT_AD را تا حد ممکن برمی‌گرداند (با متن کامل برای مدل).
        خروجی: [(id, text, ts, label), ...]
        راهبرد:
          1) سهم هر برچسب: half_ad = limit // 2 ، half_not = limit - half_ad
          2) آخرین half_ad تا از AD و آخرین half_not تا از NOT_AD را می‌کشیم.
          3) اگر مجموع کم‌تر از limit بود، از باقیماندهٔ جدیدترین‌ها (بدون توجه به برچسب) پر می‌کنیم.
          4) ادغام + حذف تکراری + مرتب‌سازی id DESC + trim به limit.
        """
        half_ad = max(0, limit // 2)
        half_not = max(0, limit - half_ad)
    
        with self.get_db_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # مرحله 1 و 2: گرفتن آخرین‌ها بر اساس برچسب (متن کامل)
            cur.execute("""
                SELECT id, text, created_at::text AS ts, label
                FROM ads_examples
                WHERE chat_id = %s AND label = 'AD'
                ORDER BY id DESC
                LIMIT %s
            """, (chat_id, half_ad))
            rows_ad = cur.fetchall() or []
    
            cur.execute("""
                SELECT id, text, created_at::text AS ts, label
                FROM ads_examples
                WHERE chat_id = %s AND label = 'NOT_AD'
                ORDER BY id DESC
                LIMIT %s
            """, (chat_id, half_not))
            rows_not = cur.fetchall() or []
    
            picked_ids = {int(r["id"]) for r in itertools.chain(rows_ad, rows_not)}
            combined = list(rows_ad) + list(rows_not)
    
            # مرحله 3: اگر کم آوردیم، با جدیدترین‌های کلی پر کنیم (بدون تکرار) — متن کامل
            remain = limit - len(combined)
            if remain > 0:
                if picked_ids:
                    placeholders = ",".join(["%s"] * len(picked_ids))
                    cur.execute(f"""
                        SELECT id, text, created_at::text AS ts, label
                        FROM ads_examples
                        WHERE chat_id = %s
                          AND id NOT IN ({placeholders})
                        ORDER BY id DESC
                        LIMIT %s
                    """, [chat_id, *list(picked_ids), remain])
                else:
                    cur.execute("""
                        SELECT id, text, created_at::text AS ts, label
                        FROM ads_examples
                        WHERE chat_id = %s
                        ORDER BY id DESC
                        LIMIT %s
                    """, (chat_id, remain))
                extra = cur.fetchall() or []
                combined += extra
    
            # مرحله 4: مرتب‌سازی نهایی و trim
            combined = sorted(combined, key=lambda r: int(r["id"]), reverse=True)[:limit]
    
        return [
            (int(r["id"]), str(r["text"]), str(r["ts"]), str(r["label"]))
            for r in combined
        ]

    
    # ---------- prompt & Flowise ----------
    def _build_prompt(self, message_text: str, examples: List[Tuple[int, str, str, str]]) -> str:
        # پرامپت در Chatflow (نود Chat Prompt Template) تنظیم و اعمال می‌شود.
        # متن پیام و فیوشات‌ها از طریق overrideConfig.vars به همان نود تزریق می‌شوند.
        # برای جلوگیری از دوگانگیِ پرامپت، اینجا چیزی ارسال نمی‌کنیم.
        return ""

    def _fetch_examples(self, chat_id: int, limit: int) -> List[Tuple[int, str, str, str]]:
        """
        نمونه‌هایی که به «مدل» تزریق می‌شوند؛ balanced → فول‌تکست، latest → فول‌تکست
        """
        mode = self.chat_examples_select_mode(chat_id)
        if mode == "balanced":
            return self.list_examples_balanced(chat_id, limit=limit)  # فول‌تکست در همین متد
        return self.list_examples_full(chat_id, limit=limit)


    def _call_flowise_ads(
        self,
        prompt: str,
        message_text: Optional[str] = None,
        examples_str: Optional[str] = None,
        chat_id: Optional[int] = None,
        extra_vars: Optional[dict] = None,  # ← اضافه شد
    ) -> Tuple[Optional[dict], str]:
    
        cfid = self.chat_chatflow_id(chat_id) if chat_id is not None else self._chatflow_id_env
        if not (self.flowise_base_url and cfid):
            return None, "missing_chatflow_or_base_url"
    
        url = f"{self.flowise_base_url}/api/v1/prediction/{cfid}"
        headers = {"Content-Type": "application/json"}
        if self.flowise_api_key:
            headers["Authorization"] = f"Bearer {self.flowise_api_key}"
    
        payload = {
            "question": "",
            "overrideConfig": {
                "sessionId": f"ads_{chat_id or 'watch'}",
                "returnSourceDocuments": False,
                "vars": {}
            }
        }
    
        if message_text is not None:
            payload["overrideConfig"]["vars"]["text"] = message_text
        if examples_str is not None:
            payload["overrideConfig"]["vars"]["examples"] = examples_str
        if isinstance(extra_vars, dict) and extra_vars:
            # سیگنال‌های زمینه‌ای (مثلاً is_reply / has_contact)
            try:
                payload["overrideConfig"]["vars"].update({
                    k: (bool(v) if isinstance(v, (bool, int)) else v)
                    for k, v in extra_vars.items()
                    if v is not None
                })
            except Exception:
                pass




        def _try_parse_obj(d: dict) -> Optional[dict]:
            if not isinstance(d, dict):
                return None
            label = d.get("label") or d.get("Label") or d.get("LABEL")
            score = d.get("score") or d.get("Score") or d.get("SCORE")
            reason = d.get("reason") or d.get("Reason") or d.get("REASON")
            if label is None and "json" in d and isinstance(d["json"], dict):
                jj = d["json"]
                label = jj.get("label") or jj.get("Label") or jj.get("LABEL")
                score = jj.get("score") if jj.get("score") is not None else score
                reason = jj.get("reason") if jj.get("reason") is not None else reason
                if label is not None:
                    return {"label": label, "score": score, "reason": reason}
            if label is not None:
                return {"label": label, "score": score, "reason": reason}
            return None

        def _strip_code_fences(s: str) -> str:
            s = s.strip()
            if s.startswith("```json"):
                s = s[7:]
            elif s.startswith("```"):
                s = s[3:]
            if s.endswith("```"):
                s = s[:-3]
            return s.strip()



        try:
            r = requests.post(
                url, headers=headers, data=json.dumps(payload),
                timeout=(getattr(self, "_flowise_connect_timeout", 5), getattr(self, "_flowise_read_timeout", 75))
            )
            try:
                r.raise_for_status()
            except requests.exceptions.HTTPError as he:
                # لاگ دقیق خطا برای عیب‌یابی (HTTP 4xx/5xx)
                try:
                    body = r.text[:800]
                except Exception:
                    body = "<no-body>"
                log.warning("[ads] Flowise HTTP %s: %s | resp: %s", r.status_code, he, body)
                raise

            data = r.json()
            
            if os.getenv("ADS_DEBUG", "0") == "1":
                try:
                    keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                except Exception:
                    keys = "?"
                log.info("[ads] HTTP %s, top-level=%s", r.status_code, keys)

            if isinstance(data, dict):
                obj = _try_parse_obj(data)
                if obj is not None:
                    return obj, ""

                res = data.get("result")
                if isinstance(res, dict) and isinstance(res.get("json"), dict):
                    obj2 = _try_parse_obj(res["json"]) or res["json"]
                    if isinstance(obj2, dict) and (obj2.get("label") or (isinstance(obj2, dict) and isinstance(obj2.get("json"), dict) and obj2["json"].get("label"))):
                        return obj2, ""

                if isinstance(data.get("json"), dict):
                    obj3 = _try_parse_obj(data["json"]) or data["json"]
                    if isinstance(obj3, dict) and obj3.get("label"):
                        return obj3, ""

                text_val = None
                if data.get("text"):
                    text_val = data["text"]
                else:
                    if isinstance(res, dict) and res.get("text"):
                        text_val = res["text"]
                    elif isinstance(res, list) and res and isinstance(res[0], dict) and res[0].get("text"):
                        text_val = res[0]["text"]

                if text_val:
                    text_val = _strip_code_fences(str(text_val))
                    try:
                        parsed = json.loads(text_val)
                        obj = _try_parse_obj(parsed) or (parsed if isinstance(parsed, dict) else None)
                        if obj is not None:
                            return obj, ""
                        else:
                            return None, "parsed_text_not_object"
                    except Exception as je:
                        return None, "invalid_json_text"

            return None, "unexpected_response_shape"
        except Exception as e:
            return None, f"exception:{e}"



    # ---------- watchdog ----------
    def _check_domain_whitelisted(self, chat_id: int, domains: List[str]) -> bool:
        try:
            with self.get_db_conn() as conn, conn.cursor() as cur:
                q = "SELECT domain FROM ads_whitelist_domains WHERE chat_id=%s AND domain = ANY(%s) LIMIT 1"
                cur.execute(q, (chat_id, domains))
                return cur.fetchone() is not None
        except Exception:
            return False

    def _save_decision(self, chat_id, message_id, user_id, text, label, is_ad, score, reason):
        """
        ذخیرهٔ تصمیم مدل برای هر پیام (برای آمار/شبیه‌سازی لازم است label خام هم بماند).
        """
        try:
            with self.get_db_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO ads_decisions (chat_id, message_id, user_id, text, label, is_ad, score, reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (chat_id, message_id, user_id, text, label, bool(is_ad), score, reason))
                conn.commit()
        except Exception:
            pass


    async def watchdog(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        chat = update.effective_chat
        if not chat:
            return
        if not self.chat_feature_on(chat.id):
            return

        u = update.effective_user
        if not msg or chat.type not in ("group", "supergroup"):
            return
        
        # اطمینان از ساخت/تکمیل تنظیمات گروه در DB (پیش‌فرض‌ها از bot_config → یا fallback)
        try:
            ensure_chat_defaults(chat.id)
        except Exception:
            pass


        # --- Defer-close variables: اگر کپشن کافی شد، پیام هشدار را فعلاً نبند؛
        # بعد از تشخیص تبلیغات (is_ad) تصمیم می‌گیریم که "ببندیم/حذف کنیم".
        wm_to_close: int | None = None     # message_id پیام هشدار اینلاین «کپشن لازم»
        wm_close_by: int | None = None     # user_id ارسال‌کننده برای دکمهٔ سکوت
        
        # اگر on_edited_message قبلاً بستن را به تعویق انداخته، اینجا تحویل بگیر
        try:
            _def = self._deferred_warn_by_msg.pop((chat.id, msg.message_id), None)
            if _def:
                wm_to_close = int(_def[0]) if _def[0] is not None else None
                wm_close_by = _def[1]
        except Exception:
            pass

        if u and u.is_bot:
            return
        
        # ضدتکرار امن: اگر همین message_id را همین‌ تازگی دیده‌ایم، عبور کن
        try:
            now = time.time()
            _k = (chat.id, msg.message_id)
            if self._seen_messages.get(_k, 0) > now - self._dedup_ttl_sec:
                return
            self._seen_messages[_k] = now
            if len(self._seen_messages) > 5000:
                cutoff = now - self._dedup_ttl_sec
                self._seen_messages = {k:t for k,t in self._seen_messages.items() if t >= cutoff}
        except Exception:
            pass
        
        # Housekeeping اضافی برای dedup آلبوم‌ها (ایمن و سبک)
        try:
            cutoff2 = time.time() - self._dedup_ttl_sec
            if len(self._seen_mg_nocap) > 2000:
                self._seen_mg_nocap = {k: t for k, t in self._seen_mg_nocap.items() if t >= cutoff2}
        except Exception:
            pass

        # معافیت ادمین ناشناس / مدیران
        try:
            if (getattr(msg, "sender_chat", None) is not None and msg.sender_chat.id == chat.id) \
                or (u and int(u.id) == int(TG_ANON)):
                return
        except Exception:
            pass
        try:
            if u and await self.is_group_admin(context.bot, chat.id, u.id):
                return
        except Exception:
            pass

        # معافیت whitelist کاربر
        try:
            if u and self.wl_user_has(chat.id, u.id):
                return
        except Exception:
            pass
        
        target_msg = msg
        text = (msg.text or msg.caption or "").strip()

        # --- مدیریت ریپلای به عنوان کپشن برای مدیای در حال انتظار ---
        reply = getattr(msg, "reply_to_message", None)
        if text and reply and self.chat_allow_reply_as_caption(chat.id):
            pending_key = None
            key_from_warn = self._pending_nocap_by_warn.get((chat.id, reply.message_id))
            if key_from_warn and self._pending_nocap.get(key_from_warn):
                pending_key = key_from_warn

            if not pending_key:
                direct_key = (chat.id, reply.message_id)
                if self._pending_nocap.get(direct_key):
                    pending_key = direct_key
                else:
                    mgid = getattr(reply, "media_group_id", None)
                    if mgid:
                        key_from_mgid = self._pending_nocap_by_mgid.get((chat.id, str(mgid)))
                        if key_from_mgid and self._pending_nocap.get(key_from_mgid):
                            pending_key = key_from_mgid
            
            if pending_key:
                rec = self._pending_nocap.get(pending_key)
                if rec:
                    task = self._pending_tasks.pop(pending_key, None)
                    if task: task.cancel()
                    
                    self._pending_nocap.pop(pending_key, None)
                    mgid_val = rec.get("mgid")
                    warn_mid_val = rec.get("warn_msg_id")
                    if mgid_val:
                        self._pending_nocap_by_mgid.pop((chat.id, mgid_val), None)
                        album_key = (chat.id, mgid_val)
                        msg_ids = self._pending_album_msgs.pop(album_key, [])
                        if msg_ids and warn_mid_val:
                            success_key = (chat.id, int(warn_mid_val))
                            # در حالت ریپلای، شناسه پیام ریپلای را هم ذخیره کن
                            self._successful_albums[success_key] = {
                                "media_ids": msg_ids,
                                "reply_id": msg.message_id,
                                "ts": time.time(),
                            }

                    # این پیام هشدار فعلاً "بسته" نشود؛ بعد از تشخیص ادز تصمیم می‌گیریم
                    if rec.get("warn_msg_id"): 
                        self._pending_nocap_by_warn.pop((chat.id, int(rec["warn_msg_id"])), None)
                        wm_to_close = int(rec["warn_msg_id"])
                        wm_close_by = rec.get("by")


                    # پیام ریپلای کاربر دیگر حذف نمی‌شود (طبق درخواست قبلی)
                    target_msg = reply

        if not text:
            mgid = getattr(target_msg, "media_group_id", None)
            if mgid:
                text = self.caption_for_media_group(chat.id, mgid) or ""
        
        try:
            mgid = getattr(target_msg, "media_group_id", None)
            if mgid and target_msg.caption:
                key = (chat.id, str(mgid))
                self._mg_caption_cache[key] = (time.time(), target_msg.caption)
                if len(self._mg_caption_cache) > 5000:
                    cutoff = time.time() - self._mg_caption_ttl_sec
                    self._mg_caption_cache = {k: v for k, v in self._mg_caption_cache.items() if v[0] >= cutoff}
        except Exception:
            pass
        
        def _has_media(m):
            if not m: return False
            return any(getattr(m, attr, None) for attr in ("photo", "video", "document", "animation", "audio", "voice", "video_note"))
        
        if _has_media(target_msg) and getattr(target_msg, "reply_to_message", None):
            return
        
        is_ent_fwd, _ = self._is_forward_from_entity(target_msg)
        
        if is_ent_fwd and not self.chat_allow_forward_entities(chat.id):
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=target_msg.message_id)
            except Exception:
                try:
                    wm = await target_msg.reply_text(
                        t("ads.forward.not_allowed", chat_id=chat.id),
                        parse_mode=ParseMode.HTML
                    )
                    # --- Metrics: بعد از موفقیت ارسال هشدار، warn را بشمار
                    try:
                        MET_ADS_ACTION.labels(action="warn").inc()
                    except Exception:
                        pass
        
                    sec = self.chat_autoclean_sec(chat.id)
                    if sec and sec > 0:
                        context.application.create_task(
                            self._delete_after(context.bot, chat.id, wm.message_id, sec)
                        )
                except Exception:
                    pass
            return

        
        final_text = text.strip()
        
        
        
        # --- [NEW] Mention-to-bot safe-exempt ---------------------------------
        # اگر پیام «خطاب به خودِ بات» بود (reply به بات یا mention @bot)
        # و متنِ باقی‌مانده کوتاه/درخواستی و بدون Contact-like خارجی بود،
        # از پایپ‌لاین تشخیص تبلیغ عبور بده تا به چت‌بات برسد.
        try:
            me = context.application.bot_data.get("me")
            bot_username = ((me.username or "").strip().lower()) if me else ""
            bot_id = me.id if me else 0

            if bot_username and is_addressed_to_bot(update, bot_username, bot_id):
                # منشن خودِ بات را از متن حذف کن تا فقط محتوای واقعی بررسی شود
                cleaned = re.sub(rf"@{re.escape(bot_username)}\b", "", final_text, flags=re.IGNORECASE).strip()

                # شرط‌های معافیت:
                # 1) متن خالی/خیلی کوتاه یا حالت پرسشی/درخواستی (سلام، چی شد؟ کمک کن، ...)، یا
                # 2) کوتاه و فاقد الگوهای تماس/لینکِ خارجی (شماره/URL/آیدی غیر از خودِ بات)
                short_ok = len(cleaned) <= self.chat_reply_exempt_maxlen(chat.id)
                req_like = self._is_request_intent(cleaned)  # «کمکم کن»، «می‌خوام بدونم»، علامت ؟ / ؟ و ...
                has_contact = self._has_contact_like(cleaned)  # لینک/شماره/ایمیل/آیدی تلگرام

                if req_like or (short_ok and not has_contact):
                    return  # ← امن است؛ نگذار به Flowise برود
        except Exception:
            # هر مشکلی پیش آمد، اجازه بده مسیر عادی ادامه پیدا کند (no regression)
            pass
        # -----------------------------------------------------------------------

        
        
        
        if not final_text and _has_media(target_msg):
            mgid = getattr(target_msg, "media_group_id", None)
            
            # [جدید] اگر آلبوم است، شناسه پیام را به لیست آن اضافه کن
            if mgid:
                album_key = (chat.id, str(mgid))
                self._pending_album_msgs.setdefault(album_key, []).append(target_msg.message_id)

            # فقط برای اولین پیام آلبوم اخطار بفرست
            if mgid:
                now = time.time()
                _mk = (chat.id, str(mgid))
                if self._seen_mg_nocap.get(_mk, 0) > now - self._dedup_ttl_sec:
                    return # برای این آلبوم قبلاً اخطار داده‌ایم
            
            grace = self.chat_forward_grace_sec(chat.id) if is_ent_fwd else self.chat_nocap_grace_sec(chat.id)
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                buttons = [[InlineKeyboardButton("🧩 راهنما / مثال", callback_data=f"adsw:guide:{target_msg.message_id}")]]
                keyboard = InlineKeyboardMarkup(buttons)
                grace_txt = f"{grace//60} دقیقه" if grace >= 60 else f"{grace} ثانیه"
                wm = await target_msg.reply_text(
                    t("ads.nocap.warn", chat_id=chat.id, grace=grace_txt),
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard
                )

            except Exception:
                wm = None

            key = (chat.id, target_msg.message_id)
            self._pending_nocap[key] = {
                "by": (u.id if u else None), "grace": grace, "ts": time.time(),
                "is_forward_entity": bool(is_ent_fwd), "mgid": str(mgid) if mgid else None,
                "warn_msg_id": getattr(wm, "message_id", None),
            }
            if wm: self._pending_nocap_by_warn[(chat.id, wm.message_id)] = key
            if mgid:
                _mk = (chat.id, str(mgid))
                self._pending_nocap_by_mgid[_mk] = key
                self._seen_mg_nocap[_mk] = time.time()

            async def _arm_delete():
                try:
                    await asyncio.sleep(grace)
                    rec = self._pending_nocap.get(key)
                    if rec:
                        mgid_val = rec.get("mgid")
                        if mgid_val:
                            # [جدید] حذف گروهی تمام پیام‌های آلبوم
                            album_key = (chat.id, mgid_val)
                            msg_ids_to_delete = self._pending_album_msgs.pop(album_key, [])
                            if msg_ids_to_delete:
                                try:
                                    await context.bot.delete_messages(chat_id=chat.id, message_ids=msg_ids_to_delete)
                                except Exception:
                                    for _mid in msg_ids_to_delete:
                                        try:
                                            await context.bot.delete_message(chat_id=chat.id, message_id=_mid)
                                        except Exception:
                                            pass

                        else:
                            # حذف تکی برای مدیای غیرآلبومی
                            await context.bot.delete_message(chat_id=chat.id, message_id=target_msg.message_id)

                        warn_msg_id = rec.get("warn_msg_id")
                        if warn_msg_id:
                            try:
                                await context.bot.delete_message(chat_id=chat.id, message_id=warn_msg_id)
                            except Exception:
                                pass
                        
                        # پاکسازی وضعیت
                        self._pending_nocap.pop(key, None)
                        if mgid_val: self._pending_nocap_by_mgid.pop((chat.id, mgid_val), None)
                        if warn_msg_id: self._pending_nocap_by_warn.pop((chat.id, warn_msg_id), None)
                except Exception:
                    rec = self._pending_nocap.pop(key, None)
                    if rec:
                        mgid_val = rec.get("mgid")
                        if mgid_val:
                            self._pending_nocap_by_mgid.pop((chat.id, mgid_val), None)
                            self._pending_album_msgs.pop((chat.id, mgid_val), None) # [جدید] پاکسازی لیست آلبوم
                        if rec.get("warn_msg_id"):
                            self._pending_nocap_by_warn.pop((chat.id, rec.get("warn_msg_id")), None)
            
            self._pending_tasks[key] = context.application.create_task(_arm_delete())
            return

        if not final_text:
            return

        # ... بقیه کد watchdog بدون تغییر باقی می‌ماند ...
        try:
            ref_for_exempt = getattr(target_msg, "reply_to_message", None)
            if ref_for_exempt and self.chat_reply_exempt(chat.id):
                ref_text = (ref_for_exempt.text or ref_for_exempt.caption or "").strip()
                if ref_text and self._is_request_intent(ref_text):
                    short_ok = len(final_text) <= self.chat_reply_exempt_maxlen(chat.id)
                    allow_contact = self.chat_reply_exempt_allow_contact(chat.id)
                    contact_ok = allow_contact and self._has_contact_like(final_text) and (len(final_text) <= self.chat_reply_exempt_contact_maxlen(chat.id))
                    if short_ok or contact_ok:
                        return
        except Exception:
            pass

        try:
            domains = self._extract_domains(final_text)
            if domains and await asyncio.to_thread(self._check_domain_whitelisted, chat.id, domains):
                return
        except Exception:
            pass

        if final_text.startswith("/ads"):
            return

        now = time.time()
        if now - self._last_run_ts_per_chat.get(chat.id, 0.0) < self.chat_min_gap_sec(chat.id):
            return
        self._last_run_ts_per_chat[chat.id] = now

        try:
            await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

        examples = self._fetch_examples(chat.id, self.chat_max_fewshots(chat.id))
        examples_str = "\n\n".join([f"مثال {i+1}:\n[{e[3]}]\n{e[1]}" for i, e in enumerate(examples)])
        prompt = self._build_prompt(final_text, examples)
        is_reply_flag = bool(getattr(target_msg, "reply_to_message", None))
        has_contact_flag = self._has_contact_like(final_text)
        
        parsed, err = await asyncio.to_thread(
            self._call_flowise_ads, prompt,
            message_text=final_text, examples_str=examples_str, chat_id=chat.id,
            extra_vars={"is_reply": is_reply_flag, "has_contact": has_contact_flag}
        )

        is_ad, score, reason, label = False, None, None, "NOT_AD"
        if parsed and isinstance(parsed, dict):
            label = str(parsed.get("label", "")).upper()
            score = parsed.get("score")
            reason = parsed.get("reason")
            try:
                score = float(score) if score is not None else None
            except Exception:
                score = None
            is_ad = (label == "AD") and (score is None or score >= self.chat_threshold(chat.id))

        await asyncio.to_thread(
            self._save_decision, chat.id, target_msg.message_id, u.id if u else None, final_text,
            label, is_ad, score, reason
        )

        if not is_ad:
            # کپشن کافی و تبلیغاتی نیست => همان هشدار اینلاین را به «✅ کپشن دریافت شد» ادیت کن
            if wm_to_close:
                try:
                    await self._close_warn_message(context, chat.id, wm_to_close, target_user_id=wm_close_by)
                except Exception:
                    pass
            return

        try:
            mgid = getattr(target_msg, "media_group_id", None)
            if mgid:
                now = time.time()
                _mk = (chat.id, str(mgid))
                if self._seen_media_groups.get(_mk, 0) > now - self._dedup_ttl_sec: return
                self._seen_media_groups[_mk] = now
                if len(self._seen_media_groups) > 2000:
                    cutoff = now - self._dedup_ttl_sec
                    self._seen_media_groups = {k:t for k,t in self._seen_media_groups.items() if t >= cutoff}
        except Exception: pass

        act = self.chat_action(chat.id)

        # --- Tokens (MVP-0): require 1 token per AD per week when act != 'delete' ---
        try:
            if act != "delete":
                nowdt = datetime.now(timezone.utc)
                # اطمینان از وجود تنظیمات گروه (DB-first)
                with pg_conn() as _conn:
                    ensure_group_settings(_conn, chat.id)
                # گرانت تنبل هفته جاری + تلاش برای خرج ۱ ژتون
                with pg_conn() as _conn:
                    grant_weekly_if_needed(_conn, chat.id, (u.id if u else 0), nowdt)
                    ok, new_bal = spend_one_for_ad(_conn, chat.id, (u.id if u else 0))
                if not ok:
                    # سهمیه تمام است → پیام تبلیغاتی را حذف کن و اطلاع بده
                    try:
                        await context.bot.delete_message(chat.id, target_msg.message_id)
                    except Exception:
                        pass
                    try:
                        await context.bot.send_message(
                            chat_id=chat.id,
                            reply_to_message_id=getattr(target_msg, "message_id", None),
                            text="⛔️ سهمیهٔ تبلیغ هفتگی شما تمام شده است. برای آگهی بعدی باید تا هفتهٔ بعد صبر کنید."
                        )
                    except Exception:
                        pass
                    return  # ادامه مسیر را متوقف کن (هشدار/… نده)
        except Exception:
            # هر خطایی در ماژول ژتون نباید AdsGuard را از کار بیندازد
            pass
        
        # --- Metrics: Ads action decision (بدون تغییر رفتار قبلی)
        try:
            if act == "none":
                MET_ADS_ACTION.labels(action="none").inc()
        except Exception:
            pass

        if act == "none":
            return
        elif act in ("warn", "delete"):
            # اگر قبل‌تر پیام هشدار «کپشن لازم» داشتیم، الان که تبلیغاتی شد حذفش کن
            if wm_to_close:
                try:
                    await context.bot.delete_message(chat_id=chat.id, message_id=wm_to_close)
                except Exception:
                    pass
                # و اگر قبلاً داده‌های آلبوم را برای دکمهٔ سکوت ذخیره کردیم، پاکشان کن تا استیت تمیز بماند
                try:
                    self._successful_albums.pop((chat.id, wm_to_close), None)
                except Exception:
                    pass

            sender_mention, sender_id_html = build_sender_html_from_msg(target_msg)
            _warn_text = t(
                "ads.warn.detected",
                chat_id=chat.id,
                sender_mention=sender_mention,
                sender_id_html=sender_id_html,
            )

            if act == "delete":
                # شمارش تصمیم delete (صرف نظر از موفق/ناموفق بودن حذف)
                try:
                    MET_ADS_ACTION.labels(action="delete").inc()
                except Exception:
                    pass
                try:
                    await context.bot.delete_message(chat_id=chat.id, message_id=target_msg.message_id)
                    raise ApplicationHandlerStop()  # جلوگیری از ارسال هشدار بعد از حذف موفق
                except Exception:
                    pass  # اگر حذف نشد، هشدار می‌دهیم
            # --- جلوگیری از هشدارهای تکراری روی ادیت‌های پیاپی اما همچنان AD ---
            # (بخش‌های بعدی فایل شما بدون تغییر می‌مانند)
            # در نقطه‌ای که پیام هشدار ارسال می‌شود (reply_text)، پس از ارسال:
            #   MET_ADS_ACTION.labels(action="warn").inc()

            
            # --- جلوگیری از هشدارهای تکراری روی ادیت‌های پیاپی اما همچنان AD ---
            key = (chat.id, target_msg.message_id)
            now = time.time()
            cd = max(0, int(self.chat_warn_edit_cooldown_sec(chat.id) or 0))
            last = self._ad_warn_ts.get(key, 0)
            if last and (now - last) < cd:
                # اگر پیام هشدار قبلی را داریم، همان را ادیت کن (اختیاری برای UX بهتر)
                old_mid = self._ad_warn_msgid.get(key)
                if old_mid:
                    try:
                        await context.bot.edit_message_text(
                            chat_id=chat.id,
                            message_id=old_mid,
                            text=t("ads.warn.still_ad", chat_id=chat.id),
                            parse_mode=ParseMode.HTML
                        )
                    except BadRequest as e:
                        # Bot API وقتی متن تغییری نکرده باشد، خطای زیر را می‌دهد؛ سایلنت نادیده بگیر
                        # "Bad Request: message is not modified"
                        if "message is not modified" in str(e).lower():
                            pass
                        else:
                            # سایر BadRequestها (مثل پیام پیدا نشد/اجازه کافی نیست) را اگر خواستی لاگ کن یا دوباره raise کن
                            # log.debug(f"edit_message_text failed: {e}")
                            pass
                    except Exception:
                        # هر خطای غیرمنتظره‌ی دیگر را قورت بده تا UX خراب نشود
                        pass
                # در هر صورت پیام هشدار جدید نساز
                return
            
            try:
                wm = await target_msg.reply_text(_warn_text, parse_mode=ParseMode.HTML)
            
                # --- Metrics: بعد از موفقیت ارسال هشدار، warn را بشمار
                try:
                    MET_ADS_ACTION.labels(action="warn").inc()
                except Exception:
                    pass
            
                # ثبت زمان و شناسه پیام هشدار برای جلوگیری از تکرار
                key = (chat.id, target_msg.message_id)
                self._ad_warn_ts[key] = time.time()
                if wm and getattr(wm, "message_id", None):
                    self._ad_warn_msgid[key] = wm.message_id
            
                sec = self.chat_autoclean_sec(chat.id)
                if sec and sec > 0:
                    context.application.create_task(
                        self._delete_after(context.bot, chat.id, wm.message_id, sec)
                    )
            except Exception:
                pass

            
            # توقف پردازش سایر هندلرها برای جلوگیری از پاسخ ChatAI
            if act in ("warn", "delete"):
                log.debug("AdsGuard: stopping other handlers (act=%s)", act)
                raise ApplicationHandlerStop()
