# /tokens/handlers.py
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from datetime import datetime, timezone
from .models import pg_conn, grant_weekly_if_needed, get_wallet

async def _cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    tenant_id = chat.id
    user_id = user.id
    now = datetime.now(timezone.utc)
    with pg_conn() as conn:
        # ensure group_settings exists (defaults)
        from .models import ensure_group_settings
        ensure_group_settings(conn, tenant_id)
        # lazy weekly grant (idempotent)
        balance = grant_weekly_if_needed(conn, tenant_id, user_id, now)
        # reply (i18n را بعداً دقیق می‌کنیم)
        await context.bot.send_message(chat_id=chat.id, reply_to_message_id=getattr(update.message,'message_id',None),
                                       text=f"موجودی ژتون شما: {balance}")

def register_token_handlers(app: Application):
    app.add_handler(CommandHandler(["wallet","bal","balance"], _cmd_wallet))
