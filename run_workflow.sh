#!/usr/bin/env bash
set -u

PORT="${PORT:-7860}"
DB_FILE="${DB_PATH:-consulting_bot.db}"
CYCLE_SECONDS=20100 # 335 minutes (5.5 hours)
START_TIME=$(date +%s)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")

echo "========================================================"
echo "🚀 راه‌اندازی ربات با مکانیزم ترکیبی Hot-Reload (۳ ثانیه) و Watchdog"
echo "🔌 پورت فعال: $PORT | ⏱ زمان سیکل: ۳۳۵ دقیقه (۵.۵ ساعت)"
echo "========================================================"

# راه‌اندازی اختیاری تانل کلودفلر روی پورت 7860
if ! command -v cloudflared >/dev/null 2>&1; then
  curl -sL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cloudflared && chmod +x /tmp/cloudflared || true
  export PATH="/tmp:$PATH"
fi

if command -v cloudflared >/dev/null 2>&1; then
  if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
    echo "🌐 اتصال به Cloudflare Tunnel اختصاصی..."
    nohup cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" >/tmp/cloudflared.log 2>&1 &
  else
    echo "🌐 ایجاد لینک Quick Tunnel کلودفلر روی پورت $PORT..."
    nohup cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >/tmp/cloudflared.log 2>&1 &
  fi
fi

free_port_and_stop_bot() {
  if [ -n "${BOT_PID:-}" ] && kill -0 "$BOT_PID" 2>/dev/null; then
    kill -TERM "$BOT_PID" 2>/dev/null || true
    sleep 0.4
    kill -9 "$BOT_PID" 2>/dev/null || true
  fi
  fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
  pkill -9 -f "python.*bot.py" >/dev/null 2>&1 || true
}

start_bot() {
  free_port_and_stop_bot
  echo "▶️ [$(date '+%H:%M:%S')] بالا آوردن bot.py روی پورت $PORT..."
  python3 bot.py &
  BOT_PID=$!
  echo "✅ bot.py با شناسه PID=$BOT_PID فعال شد."
}

BOT_PID=""
LAST_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "")
start_bot

while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TIME))

  # پایان سیکل ۵.۵ ساعته (۳۳۵ دقیقه) جهت اجرای گام Self Re-Trigger
  if [ "$ELAPSED" -ge "$CYCLE_SECONDS" ]; then
    echo "⏰ پایان سیکل ۳۳۵ دقیقه‌ای (۵.۵ ساعت). آماده‌سازی برای انتقال بدون وقفه به سیکل بعدی..."
    free_port_and_stop_bot
    break
  fi

  # ۱. مکانیزم Watchdog (ضد کرش): بررسی زنده بودن ربات و بالا آوردن مجدد فوری
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    echo "⚠️ [Watchdog] پردازش bot.py متوقف شده است! ری‌استارت خودکار در کسری از ثانیه..."
    start_bot
  fi

  # ۲. مکانیزم Live Hot-Reload (بررسی کل مخزن هر ۳ ثانیه)
  if git fetch origin "$BRANCH" --quiet 2>/dev/null; then
    REMOTE_COMMIT=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "$LAST_COMMIT")
    if [ -n "$REMOTE_COMMIT" ] && [ "$REMOTE_COMMIT" != "$LAST_COMMIT" ]; then
      echo "🔄 [Hot-Reload] تغییر جدید در مخزن شناسایی شد ($LAST_COMMIT -> $REMOTE_COMMIT)!"

      # بکاپ ایمن از دیتابیس فعلی در حافظه موقت قبل از آپدیت کدها
      if [ -f "$DB_FILE" ]; then
        sqlite3 "$DB_FILE" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
        cp -f "$DB_FILE" "/tmp/hotreload_backup_${DB_FILE}"
      fi

      # بررسی اینکه آیا خود کاربر فایل دیتابیس جدیدی را مستقیماً در گیت‌هاب آپلود کرده است یا خیر
      DB_CHANGED_IN_GIT=$(git diff --name-only "$LAST_COMMIT" "$REMOTE_COMMIT" -- "$DB_FILE" 2>/dev/null || echo "")

      # دانلود آنی تغییرات مخزن
      git reset --hard "origin/$BRANCH" --quiet
      git clean -fd --exclude="$DB_FILE" --exclude="${DB_FILE}-wal" --exclude="${DB_FILE}-shm" --quiet

      # اگر کاربر فایل دیتابیس را در گیت تغییر نداده بود، دیتابیس در حالِ کارِ فعلی (شامل بکاپ‌های ارسالی در تلگرام) را حفظ کن
      if [ -z "$DB_CHANGED_IN_GIT" ] && [ -f "/tmp/hotreload_backup_${DB_FILE}" ]; then
        cp -f "/tmp/hotreload_backup_${DB_FILE}" "$DB_FILE"
      fi

      # اگر requirements.txt تغییر کرده بود، پکیج‌های جدید را نصب کن
      REQ_CHANGED=$(git diff --name-only "$LAST_COMMIT" "$REMOTE_COMMIT" -- "requirements.txt" 2>/dev/null || echo "")
      if [ -n "$REQ_CHANGED" ] && [ -f "requirements.txt" ]; then
        pip install -q -r requirements.txt || true
      fi

      LAST_COMMIT="$REMOTE_COMMIT"
      chmod +x run_workflow.sh 2>/dev/null || true
      echo "⚡ [Hot-Reload] آزادسازی پورت $PORT و بالا آوردن bot.py با کد جدید..."
      start_bot
    fi
  fi

  sleep 3
done
