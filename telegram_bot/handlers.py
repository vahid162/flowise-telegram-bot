# /telegram_bot/handlers.py
# -*- coding: utf-8 -*-

import os
import json
import logging
import requests
from typing import Any, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from database import (
    load_history,
    save_history,
    clear_history,
    save_feedback,
    log_conversation,
    has_any_history,  # حالا این تابع را مستقیم داریم
)

# --- تنظیمات Flowise ---
FLOWISE_API_URL = os.getenv("FLOWISE_API_URL")
FLOWISE_API_KEY = os.getenv("FLOWISE_API_KEY")

# --- تنظیمات سشن ---
# اگر SESSION_TIMEOUT>0 باشد، پیام «پایان جلسه» فعال است
SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", "0"))

# کلید داخلی برای نگهداری وضعیت در حافظه‌ی پردازه
_SESSION_FLAG_KEY = "_had_history_once"


def _build_feedback_markup(message_id: int) -> InlineKeyboardMarkup:
    """کیبورد بازخورد."""
    keyboard = [[
        InlineKeyboardButton("👍", callback_data=f"feedback:like:{message_id}"),
        InlineKeyboardButton("👎", callback_data=f"feedback:dislike:{message_id}")
    ]]
    return InlineKeyboardMarkup(keyboard)


def _make_flowise_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if FLOWISE_API_KEY:
        headers["Authorization"] = f"Bearer {FLOWISE_API_KEY}"
    return headers


def _parse_flowise_response(resp_json: Dict[str, Any]) -> str:
    for key in ("text", "output", "answer", "result"):
        val = resp_json.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return json.dumps(resp_json, ensure_ascii=False)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("شروع گفتگوی جدید 🚮", callback_data="clear_chat")]]
    await update.message.reply_text(
        "سلام! من یک ربات گفتگو هستم. برای شروع یک مکالمه جدید روی دکمه زیر کلیک کنید.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    logging.info(f"Start command by user: {update.effective_user.id}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "clear_chat":
        ok = clear_history(chat_id)
        context.user_data[_SESSION_FLAG_KEY] = False
        msg = "تاریخچه پاک شد. می‌توانید گفتگوی جدید را شروع کنید." if ok else "خطایی در پاک کردن تاریخچه رخ داد."
        await query.edit_message_text(text=msg)

    elif data.startswith("feedback:"):
        try:
            _, feedback_type, message_id_str = data.split(":")
            message_id = int(message_id_str)
        except Exception:
            await query.answer("خطا در ثبت بازخورد.", show_alert=True)
            return

        save_feedback(chat_id, message_id, feedback_type, query.message.text)
        new_text = query.message.text + "\n\n---"
        new_text += "\n✅ از بازخورد شما متشکریم!" if feedback_type == "like" else "\n☑️ از بازخورد شما متشکریم. سعی می‌کنیم بهتر شویم."
        await query.edit_message_text(text=new_text, reply_markup=None)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_message = (update.message.text or "").strip()
    logging.info(f"Received message: '{user_message}' from user: {chat_id}")

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    # 1) تاریخچه از دیتابیس
    history = load_history(chat_id)  # اگر منقضی باشد، پاک می‌شود و [] برمی‌گردد

    # آیا قبلاً این کاربر سابقه داشته؟
    had_history_once = bool(context.user_data.get(_SESSION_FLAG_KEY, False))
    had_history_db = has_any_history(chat_id)
    had_any_history_before = had_history_once or had_history_db

    # 2) اگر تاریخچه خالی شد ولی قبلاً سابقه بوده => پیام پایان جلسه
    if SESSION_TIMEOUT > 0 and not history and had_any_history_before:
        try:
            await update.message.reply_text("🕓 جلسه قبلی شما به پایان رسیده. یک مکالمه جدید شروع شده.")
        except Exception as e:
            logging.warning(f"Could not send 'session expired' notice: {e}")
        # بعد از اعلام، فلگ حافظه محلی را ریست کنیم
        context.user_data[_SESSION_FLAG_KEY] = False

    # 3) درخواست به Flowise
    payload = {"question": user_message, "history": history}
    reply_text = "متاسفانه در ارتباط با سرویس هوش مصنوعی مشکلی پیش آمده."
    try:
        resp = requests.post(FLOWISE_API_URL, json=payload, headers=_make_flowise_headers(), timeout=30)
        resp.raise_for_status()
        if resp.headers.get("content-type", "").lower().startswith("application/json"):
            reply_text = _parse_flowise_response(resp.json())
        else:
            reply_text = resp.text or reply_text
    except requests.exceptions.Timeout:
        reply_text = "⏳ درخواست طول کشید. لطفاً دوباره تلاش کنید."
        logging.error(f"Flowise API timeout for user {chat_id}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Flowise API request failed for user {chat_id}: {e}")

    # 4) لاگ مکالمه در دیتابیس پروژه
    try:
        log_conversation(chat_id, user_message, reply_text)
    except Exception as e:
        logging.warning(f"log_conversation failed: {e}")

    # 5) ذخیره تاریخچه جدید
    try:
        history.append({"type": "human", "message": user_message})
        history.append({"type": "ai", "message": reply_text})
        save_history(chat_id, history)
        context.user_data[_SESSION_FLAG_KEY] = True
    except Exception as e:
        logging.error(f"Error saving/updating history for chat {chat_id}: {e}")

    # 6) ارسال پاسخ + بازخورد
    sent = await update.message.reply_text(reply_text)
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=sent.message_id,
            reply_markup=_build_feedback_markup(sent.message_id),
        )
    except Exception as e:
        logging.warning(f"Could not attach feedback buttons: {e}")

    logging.info(f"Sent reply to user {chat_id}: '{reply_text}'")
