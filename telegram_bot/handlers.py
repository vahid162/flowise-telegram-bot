# /telegram_bot/handlers.py

import os
import logging
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import (
    load_history, 
    save_history, 
    clear_history, 
    save_feedback,
    log_conversation
)

FLOWISE_API_URL = os.getenv("FLOWISE_API_URL")
FLOWISE_API_KEY = os.getenv("FLOWISE_API_KEY")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("شروع گفتگوی جدید 🚮", callback_data="clear_chat")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "سلام! من یک ربات گفتگو هستم. برای شروع یک مکالمه جدید روی دکمه زیر کلیک کنید.",
        reply_markup=reply_markup
    )
    logging.info(f"Start command by user: {update.effective_user.id}")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "clear_chat":
        if clear_history(chat_id):
            reply_text = "تاریخچه پاک شد. می‌توانید گفتگوی جدید را شروع کنید."
        else:
            reply_text = "خطایی در پاک کردن تاریخچه رخ داد."
        await query.edit_message_text(text=reply_text)
    
    elif data.startswith("feedback:"):
        _, feedback_type, message_id_str = data.split(":")
        message_id = int(message_id_str)
        save_feedback(chat_id, message_id, feedback_type, query.message.text)
        new_text = query.message.text + "\n\n---"
        if feedback_type == "like":
            new_text += "\n✅ از بازخورد شما متشکریم!"
        else:
            new_text += "\n☑️ از بازخورد شما متشکریم. سعی می‌کنیم بهتر شویم."
        await query.edit_message_text(text=new_text, reply_markup=None)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    user_message = update.message.text
    logging.info(f"Received message: '{user_message}' from user: {chat_id}")
    await context.bot.send_chat_action(chat_id=chat_id, action='typing')
    
    # ۱. بارگیری تاریخچه از دیتابیس خودمان
    chat_history = load_history(chat_id)

    # پی‌لود ساده، فقط با تاریخچه. هیچ sessionId ارسال نمی‌شود.
    payload = {
        "question": user_message,
        "history": chat_history,
    }
    headers = {"Content-Type": "application/json"}
    if FLOWISE_API_KEY:
        headers["Authorization"] = f"Bearer {FLOWISE_API_KEY}"

    reply = ""
    try:
        resp = requests.post(FLOWISE_API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        res_data = resp.json()
        reply = res_data.get("text") or res_data.get("output") or str(res_data)
    except requests.exceptions.RequestException as e:
        reply = "متاسفانه در ارتباط با سرویس هوش مصنوعی مشکلی پیش آمده."
        logging.error(f"Flowise API request failed for user {chat_id}: {e}")
    
    log_conversation(chat_id, user_message, reply)
    
    # ۴. به‌روزرسانی تاریخچه در دیتابیس خودمان
    chat_history.append({"type": "human", "message": user_message})
    chat_history.append({"type": "ai", "message": reply})
    save_history(chat_id, chat_history)

    # ارسال پاسخ و سپس افزودن دکمه‌های بازخورد
    sent_message = await update.message.reply_text(reply)
    message_id = sent_message.message_id
    keyboard = [[
        InlineKeyboardButton("👍", callback_data=f"feedback:like:{message_id}"),
        InlineKeyboardButton("👎", callback_data=f"feedback:dislike:{message_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
    logging.info(f"Sent reply to user {chat_id}: '{reply}'")
