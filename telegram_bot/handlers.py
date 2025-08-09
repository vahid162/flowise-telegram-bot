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

# --- کمک‌کننده: نگاشت تاریخچه به فرمت مورد انتظار Flowise ---
# Flowise انتظار دارد history به صورت آرایه‌ای از آبجکت‌های:
#   {"role":"userMessage","content":"..."}  برای کاربر
#   {"role":"apiMessage","content":"..."}   برای پاسخ مدل
# باشد. (بر اساس مستند Prediction) :contentReference[oaicite:2]{index=2}
def _map_history_for_flowise(history: list) -> list:
    mapped = []
    for item in history or []:
        # ما در DB خودمان با کلیدهای "type": human/ai و "message" ذخیره می‌کنیم
        t = (item or {}).get("type")
        msg = (item or {}).get("message") or ""
        if t == "human":
            mapped.append({"role": "userMessage", "content": msg})
        elif t == "ai":
            mapped.append({"role": "apiMessage", "content": msg})
        # اگر نوع ناشناخته بود، نادیده بگیر
    return mapped

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

    # ۱) تاریخچه داخلی خودمان (برای نمایش و session timeout)
    local_history = load_history(chat_id)

    # ۲) نگاشت تاریخچه به فرمت Flowise (در صورت نیاز به ارسال)
    flowise_history = _map_history_for_flowise(local_history)

    # ۳) ساخت payload سازگار با Flowise:
    #    - sessionId پایدار بر اساس chat_id (برای کار صحیح Buffer Memory)
    #    - history با فرمت صحیح (اختیاری اما مفید؛ اگر Session تازه باشد، کمک می‌کند)
    payload = {
        "question": user_message,
        "overrideConfig": {
            "sessionId": str(chat_id)  # کلید اصلی کار حافظه در Flowise
        },
        "history": flowise_history     # مطابق مستند Prediction. :contentReference[oaicite:3]{index=3}
    }

    headers = {"Content-Type": "application/json"}
    if FLOWISE_API_KEY:
        headers["Authorization"] = f"Bearer {FLOWISE_API_KEY}"

    reply = ""
    try:
        resp = requests.post(FLOWISE_API_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        # فلوایز ممکن است فیلدهای متفاوتی برگرداند؛ ترتیب اولویت زیر امن است
        reply = data.get("text") or data.get("output") or data.get("answer") or data.get("message") or str(data)
    except requests.exceptions.RequestException as e:
        reply = "متاسفانه در ارتباط با سرویس هوش مصنوعی مشکلی پیش آمده."
        logging.error(f"Flowise API request failed for user {chat_id}: {e}")

    # ۴) لاگ کردن مکالمه در دیتابیس پروژه
    log_conversation(chat_id, user_message, reply)

    # ۵) ذخیره تاریخچه داخلی (برای timeout و نمایش)
    local_history.append({"type": "human", "message": user_message})
    local_history.append({"type": "ai", "message": reply})
    save_history(chat_id, local_history)

    # ۶) ارسال پاسخ + دکمه بازخورد
    sent_message = await update.message.reply_text(reply)
    message_id = sent_message.message_id
    keyboard = [[
        InlineKeyboardButton("👍", callback_data=f"feedback:like:{message_id}"),
        InlineKeyboardButton("👎", callback_data=f"feedback:dislike:{message_id}")
    ]]
    await context.bot.edit_message_reply_markup(
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    logging.info(f"Sent reply to user {chat_id}: '{reply}'")
