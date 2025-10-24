# /tokens/models.py
import os
import psycopg2, psycopg2.extras
from contextlib import contextmanager
from datetime import datetime, timezone
from .core import iso_week_monday_utc

PG_HOST = os.getenv("POSTGRES_BOT_HOST", "telegram_bot_db")
PG_DB   = os.getenv("POSTGRES_BOT_DB",   "telegram_bot_db")
PG_USER = os.getenv("POSTGRES_BOT_USER", "postgres")
PG_PW   = os.getenv("POSTGRES_BOT_PASSWORD", "")

@contextmanager
def pg_conn():
    conn = psycopg2.connect(host=PG_HOST, dbname=PG_DB, user=PG_USER, password=PG_PW)
    try:
        yield conn
    finally:
        conn.close()

def ensure_group_settings(conn, tenant_id: int):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO group_settings(tenant_id) VALUES(%s) ON CONFLICT (tenant_id) DO NOTHING", (tenant_id,))

def get_wallet(conn, tenant_id: int, user_id: int):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT balance FROM wallets WHERE tenant_id=%s AND user_id=%s", (tenant_id, user_id))
        row = cur.fetchone()
        return (row["balance"] if row else 0)

def grant_weekly_if_needed(conn, tenant_id: int, user_id: int, now: datetime):
    """
    ایدمپوتنت: اگر برای هفتهٔ جاری (Mon UTC) گرانت نشده باشد:
    1) رکورد weekly_grants ایجاد می‌کند (ON CONFLICT DO NOTHING)
    2) اگر درج موفق بود: به کیف پول +1 اضافه و در ledger ثبت می‌کند
    """
    week_start = iso_week_monday_utc(now).date()
    with conn:
        with conn.cursor() as cur:
            # init wallet if missing
            cur.execute("""
                INSERT INTO wallets(tenant_id, user_id, balance)
                VALUES (%s,%s,0)
                ON CONFLICT (tenant_id, user_id) DO NOTHING
            """, (tenant_id, user_id))

            # enforce max_carry from group_settings
            cur.execute("SELECT max_carry FROM group_settings WHERE tenant_id=%s", (tenant_id,))
            row = cur.fetchone()
            max_carry = (row[0] if row and row[0] is not None else 1)

            # try insert grant
            cur.execute("""
                INSERT INTO weekly_grants(tenant_id,user_id,week_start_date)
                VALUES (%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING week_start_date
            """, (tenant_id, user_id, week_start))
            inserted = cur.fetchone()

            if inserted:
                # read current balance
                cur.execute("SELECT balance FROM wallets WHERE tenant_id=%s AND user_id=%s FOR UPDATE",
                            (tenant_id, user_id))
                bal = cur.fetchone()[0]
                # cap carry: اگر بالانس >= max_carry، دیگر اضافه نکن
                if bal >= max_carry:
                    return bal
                # add +1
                cur.execute("""
                    UPDATE wallets SET balance = balance + 1, updated_at=NOW()
                    WHERE tenant_id=%s AND user_id=%s
                """, (tenant_id, user_id))
                # ledger
                cur.execute("""
                    INSERT INTO ledger(tenant_id, user_id, type, amount, ref_id, note)
                    VALUES (%s,%s,'grant', 1, NULL, 'weekly grant')
                """, (tenant_id, user_id))
            # return latest balance
            cur.execute("SELECT balance FROM wallets WHERE tenant_id=%s AND user_id=%s",
                        (tenant_id, user_id))
            return cur.fetchone()[0]

def spend_one_for_ad(conn, tenant_id: int, user_id: int):
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM wallets WHERE tenant_id=%s AND user_id=%s FOR UPDATE",
                        (tenant_id, user_id))
            row = cur.fetchone()
            bal = row[0] if row else 0
            if bal <= 0:
                return False, 0
            cur.execute("""
                UPDATE wallets SET balance = balance - 1, updated_at=NOW()
                WHERE tenant_id=%s AND user_id=%s
            """, (tenant_id, user_id))
            cur.execute("""
                INSERT INTO ledger(tenant_id, user_id, type, amount, ref_id, note)
                VALUES (%s,%s,'spend_ad', -1, NULL, 'ad spend')
            """, (tenant_id, user_id))
            cur.execute("SELECT balance FROM wallets WHERE tenant_id=%s AND user_id=%s", (tenant_id, user_id))
            return True, cur.fetchone()[0]
