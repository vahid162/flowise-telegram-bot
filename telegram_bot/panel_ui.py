# panel_ui.py
# ---------------------------------------------------------------------------
# پنل مدیریتی چندماژوله (Inline) با فرمت callback_data کوتاه و نسخه‌دار:
#   v1|m=<module>|a=<action>[:<value>]
# مثال‌ها:
#   v1|m=sys|a=home
#   v1|m=sys|a=tab:ads
#   v1|m=sys|a=tab:chat
#   v1|m=sys|a=pick:<chat_id>       ← انتخاب گروه در PV
#   v1|m=ads|a=feature:toggle
#   v1|m=ads|a=act:cycle
#   v1|m=ads|a=thr:+   /   v1|m=ads|a=thr:-
#   v1|m=ads|a=few:+   /   v1|m=ads|a=few:-
#   v1|m=ads|a=gap:+   /   v1|m=ads|a=gap:-
#   v1|m=ads|a=auc:cycle            ← autoclean: off → 30 → 60 → 120 → off
#   v1|m=ads|a=cfid:edit            ← ForceReply
#   v1|m=chat|a=enable:toggle
#   v1|m=chat|a=mode:cycle          ← mention ↔ all
#   v1|m=chat|a=gap:+ / gap:-
#   v1|m=chat|a=cfid:edit           ← ForceReply
#
# نکته: تأیید سطح دسترسی (ادمین بودن در گروه فعال) خارج از این فایل انجام می‌شود.
# ---------------------------------------------------------------------------

import html
import os
from shared_utils import (
    pv_group_list_limit, pv_invite_links, pv_invite_expire_hours, pv_invite_member_limit
)
from typing import Tuple, Dict, Optional
from messages_service import t
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from shared_utils import chat_cfg_get, chat_cfg_set, list_admin_groups, get_active_admin_group, cfg_get_str, cfg_get_int, cfg_get_float
from shared_utils import (
    CHAT_AI_DEFAULT_ENABLED, CHAT_AI_DEFAULT_MODE, CHAT_AI_DEFAULT_MIN_GAP_SEC, CHAT_AI_DEFAULT_AUTOCLEAN_SEC,
    ADS_DEFAULT_FEATURE, ADS_DEFAULT_ACTION, ADS_DEFAULT_THRESHOLD,
    ADS_DEFAULT_MAX_FEWSHOTS, ADS_DEFAULT_MIN_GAP_SEC, ADS_DEFAULT_AUTOCLEAN_SEC
)


# ----------------------------- ثابت‌ها/کلیدها ------------------------------
ADS_KEYS = dict(
    feature="ads_feature",
    action="ads_action",
    thr="ads_threshold",
    few="ads_max_fewshots",
    gap="ads_min_gap_sec",
    auc="ads_autoclean_sec",
    cfid="ads_chatflow_id",
    rpx="ads_reply_exempt",
    rpxlen="ads_reply_exempt_maxlen",
    rpxc="ads_reply_exempt_allow_contact",
    rpxclen="ads_reply_exempt_contact_maxlen",

)

CHAT_KEYS = dict(
    enable="chat_ai_enabled",
    mode="chat_ai_mode",              # mention|all
    admins="chat_ai_admins_only",     # NEW: فقط ادمین‌ها می‌پرسند
    gap="chat_ai_min_gap_sec",
    aauc="chat_ai_autoclean_sec",     # NEW: autoclean (sec) برای پیام‌های راهنمای Chat AI
    cfid="chat_ai_chatflow_id",
    ns="chat_ai_namespace",           # پیش‌فرض: grp:<chat_id> (در UI فعلاً فقط نمایش)
)



# پیش‌فرض‌های معقول در صورت نبود مقدار در DB
DEFAULTS = dict(
    # --- Ads (DB-first: bot_config → ENV) ---
    ads_feature=(cfg_get_str("ads_feature", "ADS_FEATURE", ADS_DEFAULT_FEATURE) or "off").strip().lower(),
    ads_action=(cfg_get_str("ads_action", "ADS_ACTION", ADS_DEFAULT_ACTION) or "none").strip().lower(),
    ads_threshold=str(cfg_get_float("ads_threshold", "ADS_THRESHOLD", float(ADS_DEFAULT_THRESHOLD))),
    ads_max_fewshots=str(cfg_get_int("ads_max_fewshots", "ADS_MAX_FEWSHOTS", int(ADS_DEFAULT_MAX_FEWSHOTS))),
    ads_min_gap_sec=str(cfg_get_int("ads_min_gap_sec", "ADS_MIN_GAP_SEC", int(ADS_DEFAULT_MIN_GAP_SEC))),
    ads_autoclean_sec=str(cfg_get_int("ads_autoclean_sec", "ADS_AUTOCLEAN_SEC", int(ADS_DEFAULT_AUTOCLEAN_SEC))),
    ads_chatflow_id=(cfg_get_str("ads_chatflow_id", "ADS_CHATFLOW_ID", "") or ""),

    # Reply-exempt flags (DB-first)
    ads_reply_exempt=(cfg_get_str("ads_reply_exempt", "ADS_REPLY_EXEMPT", "on") or "on").strip().lower(),
    ads_reply_exempt_maxlen=str(cfg_get_int("ads_reply_exempt_maxlen", "ADS_REPLY_EXEMPT_MAXLEN", 160)),
    ads_reply_exempt_allow_contact=(cfg_get_str("ads_reply_exempt_allow_contact", "ADS_REPLY_EXEMPT_ALLOW_CONTACT", "on") or "on").strip().lower(),
    ads_reply_exempt_contact_maxlen=str(cfg_get_int("ads_reply_exempt_contact_maxlen", "ADS_REPLY_EXEMPT_CONTACT_MAXLEN", 360)),

    # --- Chat-AI (DB-first: bot_config → ENV) ---
    chat_ai_enabled=(cfg_get_str("chat_ai_default_enabled", "CHAT_AI_DEFAULT_ENABLED", CHAT_AI_DEFAULT_ENABLED) or "off").strip().lower(),
    chat_ai_mode=(lambda _v: ("all" if _v=="all" else "mention"))(
        (cfg_get_str("chat_ai_default_mode", "CHAT_AI_DEFAULT_MODE", CHAT_AI_DEFAULT_MODE) or "mention").strip().lower()
    ),
    chat_ai_admins_only="off",  # NEW: پیش‌فرض خاموش
    chat_ai_min_gap_sec=str(cfg_get_int("chat_ai_default_min_gap_sec", "CHAT_AI_DEFAULT_MIN_GAP_SEC", int(CHAT_AI_DEFAULT_MIN_GAP_SEC))),
    chat_ai_autoclean_sec=str(cfg_get_int("chat_ai_default_autoclean_sec", "CHAT_AI_AUTOCLEAN_SEC", int(CHAT_AI_DEFAULT_AUTOCLEAN_SEC))),
    chat_ai_chatflow_id=(cfg_get_str("chat_ai_default_chatflow_id", "MULTITENANT_CHATFLOW_ID", "") or ""),
)



# ----------------------------- توابع کمکی UI ------------------------------
def _val(chat_id: int, key: str) -> str:
    v = chat_cfg_get(chat_id, key)
    if v is None:
        v = DEFAULTS.get(key, "")
    return str(v)

def _fmt_on_off(v: str) -> str:
    return "✅ ON" if str(v).lower() in ("on","1","true","yes") else "⛔ OFF"

def _fmt_action(v: str) -> str:
    v = (v or "").lower()
    return {"none":"⏸ none", "warn":"⚠️ warn", "delete":"🗑 delete"}.get(v, "⏸ none")

def _fmt_mode(v: str) -> str:
    m = (v or "mention").strip().lower()
    m = "all" if m == "all" else "mention"
    emoji = {"mention": "@", "all": "∗"}[m]
    return f"{emoji} {m}"

def _cycle_action(v: str) -> str:
    order = ["none","warn","delete"]
    try:
        i = order.index((v or "none").lower())
    except Exception:
        i = 0
    return order[(i+1)%len(order)]

def _cycle_mode(v: str) -> str:
    # دوحالته: mention ↔ all
    order = ["mention", "all"]
    try:
        i = order.index((v or "mention").lower())
    except Exception:
        i = 0
    return order[(i + 1) % len(order)]

def _cycle_auc(v: str) -> str:
    # off -> 30 -> 60 -> 120 -> off
    try:
        n = int(v)
    except Exception:
        n = 0
    nxt = {0:30, 30:60, 60:120, 120:0}.get(n, 0)
    return str(nxt)

# ----------------------------- رندر Home و تب‌ها ---------------------------
def render_home(chat_id: int, gtitle: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    # متن خلاصهٔ وضعیت هر ماژول (فقط نمایش؛ تغییر در تب‌ها)
    ads_feature = _fmt_on_off(_val(chat_id, ADS_KEYS["feature"]))
    ads_action  = _fmt_action(_val(chat_id, ADS_KEYS["action"]))
    chat_enable = _fmt_on_off(_val(chat_id, CHAT_KEYS["enable"]))
    chat_mode   = _fmt_mode(_val(chat_id, CHAT_KEYS["mode"]))

    # برچسب شفاف گروه در هدر (اگر عنوان داشت با Bold، وگرنه خود ID)
    tag = f" — <b>{html.escape(gtitle)}</b>" if gtitle else f" — <code>{chat_id}</code>"

    text = (
        t("panel.home.title", chat_id=chat_id, tag=tag) +
        f"🛡️ AdsGuard: {ads_feature} | {ads_action}\n"
        f"🤖 Chat AI: {chat_enable} | {chat_mode}\n" +
        t("panel.home.body", chat_id=chat_id)
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(t("panel.nav.chat", chat_id=chat_id), callback_data="v1|m=sys|a=tab:chat"),
         InlineKeyboardButton(t("panel.nav.ads", chat_id=chat_id), callback_data="v1|m=sys|a=tab:ads")],
    ])
    return text, kb


def _render_ads(chat_id: int, gtitle: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    feat = _val(chat_id, ADS_KEYS["feature"])
    act  = _val(chat_id, ADS_KEYS["action"])
    thr  = _val(chat_id, ADS_KEYS["thr"])
    few  = _val(chat_id, ADS_KEYS["few"])
    gap  = _val(chat_id, ADS_KEYS["gap"])
    auc  = _val(chat_id, ADS_KEYS["auc"])
    cfid = _val(chat_id, ADS_KEYS["cfid"]) or "—"
    rpx   = _val(chat_id, ADS_KEYS["rpx"])
    rpxlen = _val(chat_id, ADS_KEYS["rpxlen"])
    rpxc  = _val(chat_id, ADS_KEYS["rpxc"])
    rpxclen = _val(chat_id, ADS_KEYS["rpxclen"])


    tag = f" — <b>{html.escape(gtitle)}</b>" if gtitle else f" — <code>{chat_id}</code>"

    text = (
        f"🛡️ <b>AdsGuard</b>{tag}\n"
        f"• feature: {_fmt_on_off(feat)}\n"
        f"• action:  {_fmt_action(act)}\n"
        f"• threshold: {thr}\n"
        f"• fewshots:  {few}\n"
        f"• min_gap:   {gap}s\n"
        f"• autoclean: {auc if auc!='0' else 'off'}\n"
        f"• reply_exempt: {_fmt_on_off(rpx)} (short≤{rpxlen})\n"
        f"• reply_contact: {_fmt_on_off(rpxc)} (len≤{rpxclen})\n"
        f"• chatflow_id: <code>{cfid}</code>\n"
    )
    kb = InlineKeyboardMarkup([
        # ردیف ۱: سوییچ کلی و چرخه‌ی اقدام
        [InlineKeyboardButton("ON/OFF", callback_data="v1|m=ads|a=feature:toggle"),
         InlineKeyboardButton("act ⟳", callback_data="v1|m=ads|a=act:cycle")],

        # ردیف ۲: آستانه تشخیص (threshold)
        [InlineKeyboardButton("thr −", callback_data="v1|m=ads|a=thr:-"),
         InlineKeyboardButton("thr ＋", callback_data="v1|m=ads|a=thr:+")],

        # ردیف ۳: تعداد few-shots
        [InlineKeyboardButton("few −", callback_data="v1|m=ads|a=few:-"),
         InlineKeyboardButton("few ＋", callback_data="v1|m=ads|a=few:+")],

        # ردیف ۴: حداقل فاصله بین ارزیابی‌ها (gap)
        [InlineKeyboardButton("gap −", callback_data="v1|m=ads|a=gap:-"),
         InlineKeyboardButton("gap ＋", callback_data="v1|m=ads|a=gap:+")],

        # ردیف ۵: سوییچ‌های reply و contact در یک ردیف
        [InlineKeyboardButton("reply ⟳", callback_data="v1|m=ads|a=rpx:toggle"),
         InlineKeyboardButton("contact ⟳", callback_data="v1|m=ads|a=rpxc:toggle")],

        # ردیف ۶ (۴ستونه): کنترل طول مجاز هر کدام با ± (گام 10تایی)
        [InlineKeyboardButton("R len −", callback_data="v1|m=ads|a=rpxlen:-"),
         InlineKeyboardButton("R len ＋", callback_data="v1|m=ads|a=rpxlen:+"),
         InlineKeyboardButton("C len −", callback_data="v1|m=ads|a=rpxclen:-"),
         InlineKeyboardButton("C len ＋", callback_data="v1|m=ads|a=rpxclen:+")],

        # ردیف ۷: رفتن به پایین (autoclean و chatflow_id)
        [InlineKeyboardButton("autoclean ⟳", callback_data="v1|m=ads|a=auc:cycle"),
         InlineKeyboardButton("✎ chatflow_id", callback_data="v1|m=ads|a=cfid:edit")],

        # ردیف ۸: بازگشت به خانه
        [InlineKeyboardButton("⬅️ Home", callback_data="v1|m=sys|a=home")]
    ])

    return text, kb


def _render_chat(chat_id: int, gtitle: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    en   = _val(chat_id, CHAT_KEYS["enable"])
    mode = _val(chat_id, CHAT_KEYS["mode"])
    admins = _val(chat_id, CHAT_KEYS["admins"])    # NEW
    gap  = _val(chat_id, CHAT_KEYS["gap"])
    aauc = _val(chat_id, CHAT_KEYS["aauc"])
    cfid = _val(chat_id, CHAT_KEYS["cfid"]) or "—"
    ns   = _val(chat_id, CHAT_KEYS["ns"]) or f"grp:{chat_id}"

    tag = f" — <b>{html.escape(gtitle)}</b>" if gtitle else f" — <code>{chat_id}</code>"

    text = (
        f"🤖 <b>Chat AI</b>{tag}\n"
        f"• enabled: {_fmt_on_off(en)}\n"
        f"• mode:    {_fmt_mode(mode)}\n"
        f"• admins_only: {_fmt_on_off(admins)}\n"
        f"• min_gap: {gap}s\n"
        f"• autoclean: {aauc if aauc!='0' else 'off'}\n"
        f"• chatflow_id: <code>{cfid}</code>\n"
        f"• namespace: <code>{ns}</code>\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("ON/OFF", callback_data="v1|m=chat|a=enable:toggle"),
         InlineKeyboardButton("mode ⟳", callback_data="v1|m=chat|a=mode:cycle")],
        [InlineKeyboardButton("admins-only ⟳", callback_data="v1|m=chat|a=admins:toggle")],  # NEW
        [InlineKeyboardButton("gap −", callback_data="v1|m=chat|a=gap:-"),
         InlineKeyboardButton("gap ＋", callback_data="v1|m=chat|a=gap:+")],
        [InlineKeyboardButton("✎ chatflow_id", callback_data="v1|m=chat|a=cfid:edit")],
        [InlineKeyboardButton("autoclean ⟳", callback_data="v1|m=chat|a=cauc:cycle")],
        [InlineKeyboardButton("⬅️ Home", callback_data="v1|m=sys|a=home")]
    ])
    return text, kb


def render_module_panel(module: str, chat_id: int, gtitle: Optional[str] = None) -> Tuple[str, InlineKeyboardMarkup]:
    if module == "ads":
        return _render_ads(chat_id, gtitle=gtitle)
    return _render_chat(chat_id, gtitle=gtitle)

# ----------------------------- Group Picker -------------------------------
async def render_group_picker_text_kb(bot, user_id: int):
    """
    ساخت متن و کیبورد انتخاب گروه برای PV (همیشه لیست‌محور).
    - اگر هیچ گروهی Bind نشده باشد: پیام راهنما + دکمه «افزودن گروه».
    - اگر گروه فعال وجود دارد: ردیف «⭐ ادامه با همین گروه: <title>».
    - سپس تا سقف پیکربندی (pv_group_list_limit) بقیهٔ گروه‌ها.
    - ردیف آخر: «➕ افزودن گروه جدید».
    """
    gids = list_admin_groups(user_id) or []
    limit = int(pv_group_list_limit() or 10)

    # اگر هیچ گروهی Bind نشده
    if not gids:
        text = t("panel.manage.in_pv_hint")
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t("panel.group_picker.add_button"), callback_data="v1|m=sys|a=help:add")]]
        )
        return text, kb

    # گروه فعال برای پین در ابتدای لیست
    try:
        active_gid = get_active_admin_group(user_id)
    except Exception:
        active_gid = None

    rows = []

    # اگر گروه فعال معتبر است و در لیست وجود دارد → ردیف «ادامه با همین گروه»
    if active_gid and active_gid in gids:
        title = f"{active_gid}"
        try:
            chat = await bot.get_chat(active_gid)  # Telegram Bot API: getChat
            title = getattr(chat, "title", None) or title
        except Exception:
            pass
        rows.append([
            InlineKeyboardButton(
                t("panel.group_picker.continue_btn", title=title),
                callback_data=f"v1|m=sys|a=pick:{active_gid}"
            )
        ])
        # برای پرهیز از تکرار در لیست پایین‌تر
        gids = [g for g in gids if g != active_gid]

    # حالا بقیهٔ گروه‌ها تا سقف limit
    for gid in gids[:limit]:
        title = f"{gid}"
        bad = False
        try:
            chat = await bot.get_chat(gid)  # Telegram Bot API: getChat
            title = getattr(chat, "title", None) or title
        except Exception:
            # اگر عنوان قابل دریافت نیست، این گروه از دید ربات ناسالم/خارج از دسترس فرض شود
            bad = True
        label = f"⚠️ {title}" if bad else f"{title}"
        cb    = "v1|m=sys|a=help:add" if bad else f"v1|m=sys|a=pick:{gid}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    # ردیف آخر: افزودن گروه جدید
    rows.append([InlineKeyboardButton(t("panel.group_picker.add_button"), callback_data="v1|m=sys|a=help:add")])

    text = t("panel.group_picker.title")
    kb = InlineKeyboardMarkup(rows)
    return text, kb



# ----------------------------- Dispatcher/Parser --------------------------
def parse_callback(data: str) -> Dict[str, str]:
    """
    پارس callback_data کوتاه. خروجی:
      { "v":"v1", "m":"ads|chat|sys", "a":"act", "val":optional }
    """
    out = {"v":"v1","m":"","a":"","val":""}
    try:
        if not data.startswith("v1|"):
            return out
        body = data.split("v1|",1)[1]
        parts = body.split("|")
        pm = {}
        for p in parts:
            if "=" in p:
                k,v = p.split("=",1)
                pm[k]=v
        out["m"] = pm.get("m","")
        aa = pm.get("a","")
        if ":" in aa:
            out["a"], out["val"] = aa.split(":",1)
        else:
            out["a"] = aa
    except Exception:
        pass
    return out

# ----------------------------- Handlerهای ماژول‌ها ------------------------
def handle_ads_action(chat_id: int, action: str, val: str) -> Dict:
    updated = {}
    if action == "feature":
        cur = _val(chat_id, ADS_KEYS["feature"])
        newv = "off" if cur.lower() in ("on","1","true","yes") else "on"
        chat_cfg_set(chat_id, ADS_KEYS["feature"], newv); updated[ADS_KEYS["feature"]]=newv
    elif action == "act":
        cur = _val(chat_id, ADS_KEYS["action"])
        newv = _cycle_action(cur)
        chat_cfg_set(chat_id, ADS_KEYS["action"], newv); updated[ADS_KEYS["action"]]=newv
    elif action == "thr":
        cur = float(_val(chat_id, ADS_KEYS["thr"]))
        step = 0.05 if val=="+" else -0.05
        newv = max(0.0, min(1.0, round(cur+step, 2)))
        chat_cfg_set(chat_id, ADS_KEYS["thr"], f"{newv:.2f}"); updated[ADS_KEYS["thr"]]=f"{newv:.2f}"
    elif action == "few":
        cur = int(_val(chat_id, ADS_KEYS["few"]))
        step = 1 if val=="+" else -1
        newv = max(1, min(50, cur+step))
        chat_cfg_set(chat_id, ADS_KEYS["few"], str(newv)); updated[ADS_KEYS["few"]]=str(newv)
    elif action == "gap":
        cur = int(_val(chat_id, ADS_KEYS["gap"]))
        step = 1 if val=="+" else -1
        newv = max(0, cur+step)
        chat_cfg_set(chat_id, ADS_KEYS["gap"], str(newv)); updated[ADS_KEYS["gap"]]=str(newv)
    elif action == "auc":
        cur = _val(chat_id, ADS_KEYS["auc"])
        newv = _cycle_auc(cur)
        chat_cfg_set(chat_id, ADS_KEYS["auc"], str(newv)); updated[ADS_KEYS["auc"]]=str(newv)
    elif action == "cfid" and val == "edit":
        # اعلام به لایه‌ی بالاتر که ForceReply لازم است
        updated["__await_text__"] = {"module":"ads", "field":ADS_KEYS["cfid"], "title":"chatflow_id (Ads)"}
    elif action == "rpx":
        cur = _val(chat_id, ADS_KEYS["rpx"])
        newv = "off" if cur.lower() in ("on","1","true","yes") else "on"
        chat_cfg_set(chat_id, ADS_KEYS["rpx"], newv); updated[ADS_KEYS["rpx"]] = newv
    elif action == "rpxlen" and val in ("+","-"):
        # Increase/decrease reply_exempt_maxlen in steps of 10 (bounds 20..400)
        try:
            cur = int(_val(chat_id, ADS_KEYS["rpxlen"]))
        except Exception:
            cur = 160
        step = 10 if val == "+" else -10
        newv = max(20, min(400, cur + step))
        chat_cfg_set(chat_id, ADS_KEYS["rpxlen"], str(newv)); updated[ADS_KEYS["rpxlen"]] = str(newv)
    
    elif action == "rpxlen" and val == "edit":
        # سازگاری عقب‌رو: همچنان امکان واردکردن دستی با ForceReply
        updated["__await_text__"] = {"module":"ads", "field":ADS_KEYS["rpxlen"], "title":"reply_exempt_maxlen"}

    elif action == "rpxc":
        # سوییچ اجازه‌ی تماس (Contact) در پاسخ‌های معاف
        cur = _val(chat_id, ADS_KEYS["rpxc"])
        newv = "off" if cur.lower() in ("on","1","true","yes") else "on"
        chat_cfg_set(chat_id, ADS_KEYS["rpxc"], newv); updated[ADS_KEYS["rpxc"]] = newv

    elif action == "rpxclen" and val in ("+","-"):
        # افزایش/کاهش طول مجاز برای حالت Contact با گام 10 و محدودۀ 50..800
        try:
            cur = int(_val(chat_id, ADS_KEYS["rpxclen"]))
        except Exception:
            cur = 360
        step = 10 if val == "+" else -10
        newv = max(50, min(800, cur + step))
        chat_cfg_set(chat_id, ADS_KEYS["rpxclen"], str(newv)); updated[ADS_KEYS["rpxclen"]] = str(newv)

    elif action == "rpxclen" and val == "edit":
        # سازگاری عقب‌رو: امکان واردکردن دستی با ForceReply
        updated["__await_text__"] = {"module":"ads", "field":ADS_KEYS["rpxclen"], "title":"reply_exempt_contact_maxlen"}
    return updated


def handle_chat_action(chat_id: int, action: str, val: str) -> Dict:
    updated = {}
    if action == "enable":
        cur = _val(chat_id, CHAT_KEYS["enable"])
        newv = "off" if cur.lower() in ("on","1","true","yes") else "on"
        chat_cfg_set(chat_id, CHAT_KEYS["enable"], newv); updated[CHAT_KEYS["enable"]]=newv
    elif action == "mode":
        cur = _val(chat_id, CHAT_KEYS["mode"])
        newv = _cycle_mode(cur)
        chat_cfg_set(chat_id, CHAT_KEYS["mode"], newv); updated[CHAT_KEYS["mode"]]=newv
    elif action == "admins":   # NEW
        cur = _val(chat_id, CHAT_KEYS["admins"])
        newv = "off" if str(cur).lower() in ("on","1","true","yes") else "on"
        chat_cfg_set(chat_id, CHAT_KEYS["admins"], newv); updated[CHAT_KEYS["admins"]] = newv
    elif action == "gap":
        cur = int(_val(chat_id, CHAT_KEYS["gap"]))
        step = 1 if val=="+" else -1
        newv = max(0, cur+step)
        chat_cfg_set(chat_id, CHAT_KEYS["gap"], str(newv)); updated[CHAT_KEYS["gap"]]=str(newv)
    elif action == "cauc":
        cur = _val(chat_id, CHAT_KEYS["aauc"])
        newv = _cycle_auc(cur)  # off -> 30 -> 60 -> 120 -> off
        chat_cfg_set(chat_id, CHAT_KEYS["aauc"], str(newv)); updated[CHAT_KEYS["aauc"]] = str(newv)
    elif action == "cfid" and val == "edit":
        updated["__await_text__"] = {"module":"chat", "field":CHAT_KEYS["cfid"], "title":"chatflow_id (Chat AI)"}
    return updated
