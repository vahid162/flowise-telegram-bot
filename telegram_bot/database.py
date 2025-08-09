# /telegram_bot/database.py

import os
import logging
import json
import psycopg2
from datetime import datetime, timezone

# --- تنظیمات اتصال به دیتابیس ربات ---
DB_NAME = os.getenv("POSTGRES_BOT_DB")
DB_USER = os.getenv("POSTGRES_BOT_USER")
DB_PASS = os.getenv("POSTGRES_BOT_PASSWORD")
DB_HOST = "bot_db"
DB_PORT = "5432"

SESSION_TIMEOUT = int(os.getenv("SESSION_TIMEOUT", 300)) # 5 دقیقه

# --- تنظیمات اتصال به دیتابیس پروژه ---
PROJECT_DB_NAME = os.getenv("POSTGRES_PROJECT_DB")
PROJECT_DB_USER = os.getenv("POSTGRES_PROJECT_USER")
PROJECT_DB_PASS = os.getenv("POSTGRES_PROJECT_PASSWORD")
PROJECT_DB_HOST = "flowise_project_db"

def setup_tables():
    """تمام جداول مورد نیاز برنامه را ایجاد می‌کند."""
    try:
        # اتصال به دیتابیس ربات
        conn_bot = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
        cur_bot = conn_bot.cursor()
        
        cur_bot.execute("DROP TABLE IF EXISTS user_sessions;") # حذف جدول غیرضروری
        
        cur_bot.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                chat_id BIGINT PRIMARY KEY,
                history JSONB NOT NULL,
                last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        
        cur_bot.execute("""
            CREATE TABLE IF NOT EXISTS message_feedback (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                feedback VARCHAR(10) NOT NULL,
                message_text TEXT,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE (chat_id, message_id)
            );
        """)
        conn_bot.commit()
        cur_bot.close()
        conn_bot.close()
        logging.info("Bot database tables are ready.")

        # اتصال به دیتابیس پروژه
        conn_project = psycopg2.connect(dbname=PROJECT_DB_NAME, user=PROJECT_DB_USER, password=PROJECT_DB_PASS, host=PROJECT_DB_HOST)
        cur_project = conn_project.cursor()
        cur_project.execute("""
            CREATE TABLE IF NOT EXISTS chat_logs (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT,
                human_message TEXT,
                ai_message TEXT,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        conn_project.commit()
        cur_project.close()
        conn_project.close()
        logging.info("Project database tables are ready.")

    except psycopg2.OperationalError as e:
        logging.critical(f"Could not connect to databases: {e}")
        exit(1)

def load_history(chat_id: int) -> list:
    """تاریخچه را فقط در صورتی که منقضی نشده باشد، بارگیری می‌کند."""
    history = []
    try:
        conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
        cur = conn.cursor()
        cur.execute("""
            SELECT history FROM chat_history
            WHERE chat_id = %s AND last_updated > NOW() - INTERVAL '%s seconds';
        """, (chat_id, SESSION_TIMEOUT))
        
        result = cur.fetchone()
        if result:
            history = result[0]
        else:
            clear_history(chat_id)
        cur.close()
        conn.close()
    except Exception as e:
        logging.error(f"Error loading history for chat {chat_id}: {e}")
    return history

def save_history(chat_id: int, history: list):
    """تاریخچه را ذخیره کرده و last_updated را بروزرسانی می‌کند."""
    try:
        conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO chat_history (chat_id, history, last_updated)
            VALUES (%s, %s, NOW())
            ON CONFLICT (chat_id)
            DO UPDATE SET history = EXCLUDED.history, last_updated = NOW();
        """, (chat_id, json.dumps(history)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logging.error(f"Error saving history for chat {chat_id}: {e}")

def clear_history(chat_id: int) -> bool:
    """تاریخچه کاربر را حذف می‌کند."""
    try:
        conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
        cur = conn.cursor()
        cur.execute("DELETE FROM chat_history WHERE chat_id = %s;", (chat_id,))
        conn.commit()
        cur.close()
        conn.close()
        logging.info(f"History cleared for user {chat_id}")
        return True
    except Exception as e:
        logging.error(f"Error clearing history for user {chat_id}: {e}")
        return False

def save_feedback(chat_id: int, message_id: int, feedback: str, message_text: str):
    """بازخورد کاربر را در دیتابیس ربات ذخیره می‌کند."""
    try:
        conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO message_feedback (chat_id, message_id, feedback, message_text)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (chat_id, message_id)
            DO UPDATE SET feedback = EXCLUDED.feedback, timestamp = NOW();
        """, (chat_id, message_id, feedback, message_text))
        conn.commit()
        cur.close()
        conn.close()
        logging.info(f"Feedback '{feedback}' saved for message {message_id} from user {chat_id}")
    except Exception as e:
        logging.error(f"Error saving feedback: {e}")

def log_conversation(chat_id: int, human_message: str, ai_message: str):
    """مکالمه را در دیتابیس پروژه لاگ می‌کند."""
    try:
        conn = psycopg2.connect(dbname=PROJECT_DB_NAME, user=PROJECT_DB_USER, password=PROJECT_DB_PASS, host=PROJECT_DB_HOST)
        cur = conn.cursor()
        query = "INSERT INTO chat_logs (chat_id, human_message, ai_message) VALUES (%s, %s, %s);"
        values = (chat_id, human_message, ai_message)
        cur.execute(query, values)
        conn.commit()
        cur.close()
        conn.close()
        logging.info(f"Successfully logged conversation for user {chat_id}")
    except Exception as e:
        logging.error(f"Failed to log conversation for user {chat_id}: {e}")
