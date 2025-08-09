# /telegram-bot/Dockerfile

# استفاده از تصویر پایه پایتون نسخه 3.12 - نسخه Slim برای کاهش حجم
FROM python:3.12-slim

# مشخص کردن اطلاعات نگهدارنده (Maintainer)
LABEL maintainer="YourName <your@email.com>"

# تنظیم دایرکتوری کاری داخل کانتینر
WORKDIR /app

# کپی کردن فایل نیازمندی‌ها و نصب آن‌ها
# این لایه جداگانه باعث می‌شود تا زمانی که نیازمندی‌ها تغییر نکرده، داکر از کش استفاده کند و نصب مجدد انجام ندهد
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کردن تمام فایل‌های پروژه به داخل کانتینر
COPY . .

# اجرای ربات زمانی که کانتینر اجرا می‌شود
CMD ["python", "-u", "bot.py"]
