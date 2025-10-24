# messages_service.py
# لایهٔ سادهٔ پیام: DB-first برای زبان، خواندن از فایل JSON، و فالبک امن
from __future__ import annotations
from pathlib import Path
from typing import Optional, Dict
import json
import gettext
import os


# از هلسپرهای فعلی پروژه استفاده می‌کنیم (DB-first واقعی)
from shared_utils import get_config, chat_cfg_get  # این‌ها همین الان در پروژه موجودند

# کش سادهٔ پیام‌ها در حافظه
_MESSAGES: Dict[str, Dict[str, str]] = {}

def _norm_lang(lang: Optional[str]) -> str:
    """
    نُرمال‌سازی تگ زبان به BCP47 ساده (fa, en, ar, tr, ru).
    بعداً اگر en-US خواستی، همچنان base (en) کار می‌کند.  :contentReference[oaicite:2]{index=2}
    """
    if not lang:
        return "fa"
    lang = lang.strip().lower().replace("_", "-")
    return lang.split("-")[0]  # base language (fa, en, ...)

def _load_lang(lang: str) -> Dict[str, str]:
    lang = _norm_lang(lang)
    if lang in _MESSAGES:
        return _MESSAGES[lang]
    p = Path(__file__).resolve().parent / "messages" / f"{lang}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        data = {}
    _MESSAGES[lang] = data
    return data

def pick_lang(chat_id: Optional[int] = None, user_hint: Optional[str] = None) -> str:
    """
    سیاست انتخاب زبان (DB-first):
      1) chat_config.lang (اگر chat_id داریم)
      2) bot_config.default_lang
      3) hint کاربر (اختیاری)
      4) fa
    """
    lang = None
    try:
        if chat_id:
            lang = chat_cfg_get(chat_id, "lang")
    except Exception:
        lang = None
    if not lang:
        try:
            lang = get_config("default_lang")
        except Exception:
            lang = None
    if not lang and user_hint:
        lang = user_hint
    return _norm_lang(lang or "fa")

def t(key: str, *, chat_id: Optional[int] = None, user_lang_hint: Optional[str] = None, **vars) -> str:
    """
    گرفتن متن بر اساس کلید. فالبک: lang → fa → خودِ کلید.
    Placeholderها با format(**vars) جایگذاری می‌شوند.
    """
    lang = pick_lang(chat_id, user_hint=user_lang_hint)
    data = _load_lang(lang)
    txt = data.get(key)
    if not txt:
        txt = _load_lang("fa").get(key, key)  # فالبک به فارسی، در نهایت خود کلید
    try:
        if vars:
            txt = txt.format(**vars)
    except Exception:
        # اگر متغیر کم/زیاد بود، کرش نکن—همان متن پایه را برگردان
        pass
    return txt
    

# --- جمع/مفرد: لایهٔ اختیاری gettext با فالبک به JSON ---
_LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_GT_CACHE: Dict[str, gettext.NullTranslations] = {}

def _gt(lang: str) -> gettext.NullTranslations:
    """لود کش‌شدهٔ ترجمهٔ gettext (دومِـین 'bot'). فالبک: NullTranslations."""
    lang = _norm_lang(lang)
    tr = _GT_CACHE.get(lang)
    if tr is None:
        try:
            tr = gettext.translation("bot", localedir=str(_LOCALES_DIR), languages=[lang], fallback=True)
        except Exception:
            tr = gettext.NullTranslations()
        _GT_CACHE[lang] = tr
    return tr

def tn(singular_key: str, plural_key: str, n: int, *, chat_id: Optional[int] = None,
       user_lang_hint: Optional[str] = None, **vars) -> str:
    """
    پیام جمع/مفرد. کلیدها را به‌عنوان msgid/msgid_plural استفاده می‌کنیم.
    اول از gettext (اگر .mo موجود باشد)، وگرنه به JSON فالبک می‌کنیم.
    """
    lang = pick_lang(chat_id, user_hint=user_lang_hint)
    tr = _gt(lang)

    # اگر برای این زبان فایل .mo داشته باشیم، نتیجهٔ ngettext رشتهٔ ترجمه است؛
    # اگر نداشته باشیم، خودش msgid یا msgid_plural را برمی‌گرداند.
    txt = tr.ngettext(singular_key, plural_key, n)

    # اگر واقعاً ترجمه‌ای نبود (یعنی همان کلید برگشت)، به JSON فالبک کن:
    # 🔧 FIX: مقدار n را هم پاس بده تا {n} در پیام‌های JSON درست جایگذاری شود.
    if txt in (singular_key, plural_key):
        chosen_key = singular_key if n == 1 else plural_key
        return t(chosen_key, chat_id=chat_id, user_lang_hint=user_lang_hint, n=n, **vars)


    # اگر ترجمه بود، اجازهٔ قالب‌گذاری (مثلاً {n}) بده
    try:
        if vars:
            txt = txt.format(n=n, **vars)
        else:
            # حداقل {n} را پُر کنیم اگر در ترجمه استفاده شده
            txt = txt.format(n=n)
    except Exception:
        pass
    return txt

