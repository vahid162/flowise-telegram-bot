# /tokens/jobs.py
from telegram.ext import Application, ContextTypes
from datetime import datetime, timezone, time as dtime
from .models import pg_conn, grant_weekly_if_needed
from .core import iso_week_monday_utc

async def _weekly_grant_job(context: ContextTypes.DEFAULT_TYPE):
    """
    گرانت هفتگی idempotent:
    - برای همهٔ کاربران شناخته‌شده در wallets اجرا می‌شود.
    - اگر رکورد weekly_grants وجود داشته باشد، NO-OP می‌شود.
    """
    now = datetime.now(timezone.utc)
    week_start = iso_week_monday_utc(now).date()
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id, user_id FROM wallets")  # کاربران شناخته‌شده
            rows = cur.fetchall()
    # برای کاهش فشار DB، گرانت را خارج از همان اتصال انجام می‌دهیم
    with pg_conn() as conn:
        for tenant_id, user_id in rows:
            grant_weekly_if_needed(conn, tenant_id, user_id, now)

def schedule_weekly_grants(app: Application):
    """
    زمان‌بندی: هر دوشنبه 00:00:15 UTC (کمی تأخیر برای اطمینان).
    """
    # JobQueue در PTB v22 موجود است
    app.job_queue.run_daily(
        _weekly_grant_job,
        time=dtime(hour=0, minute=0, second=15, tzinfo=timezone.utc),
        name="weekly_grant_job"
    )
