#!/usr/bin/env bash
set -u

PORT="${PORT:-7860}"
DB_FILE="${DB_PATH:-consulting_bot.db}"
CYCLE_SECONDS=20100 # 335 minutes (5.5 hours)
START_TIME=$(date +%s)
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
CONFLICT_DETECTED=0

echo "========================================================"
echo "🚀 راه‌اندازی ربات با مکانیزم هوشمند Anti-Duplicate (کد ۴۲)، Single-Instance Lock و Watchdog"
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

flush_and_backup_db() {
  if [ -f "$DB_FILE" ]; then
    python3 -c "
import sqlite3
try:
    conn = sqlite3.connect('$DB_FILE', timeout=10.0)
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE);')
    conn.close()
except Exception:
    pass
" 2>/dev/null || true
    cp -f "$DB_FILE" "/tmp/hotreload_backup_${DB_FILE}" 2>/dev/null || true
  fi
}

sync_db_to_git() {
  # اگر این نمونه به خاطر تداخل (Conflict 42) بسته شده، دیتابیس قدیمی خود را روی گیت Push نکند!
  if [ "$CONFLICT_DETECTED" -eq 1 ]; then
    echo "ℹ️ نمونه تکراری متوقف شد؛ از بازنویسی دیتابیس روی گیت خودداری شد."
    return
  fi
  flush_and_backup_db
  if [ -f "$DB_FILE" ] && command -v git >/dev/null 2>&1; then
    git config user.name "Bot Auto-Sync" 2>/dev/null || true
    git config user.email "bot@users.noreply.github.com" 2>/dev/null || true
    git add "$DB_FILE" 2>/dev/null || true
    if ! git diff --cached --quiet 2>/dev/null; then
      git fetch origin "$BRANCH" --quiet 2>/dev/null || true
      git commit -m "chore(db): auto-sync live database state [skip ci]" --quiet 2>/dev/null || true
      git push origin "HEAD:$BRANCH" --quiet 2>/dev/null || true
    fi
  fi
}

free_port_and_stop_bot() {
  if [ -n "${BOT_PID:-}" ] && kill -0 "$BOT_PID" 2>/dev/null; then
    kill -TERM "$BOT_PID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      if ! kill -0 "$BOT_PID" 2>/dev/null; then
        break
      fi
      sleep 0.3
    done
    kill -9 "$BOT_PID" 2>/dev/null || true
    wait "$BOT_PID" 2>/dev/null || true
  fi
  fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
  pkill -9 -f "python.*bot.py" >/dev/null 2>&1 || true
  rm -f /tmp/consulting_bot_single_instance.lock 2>/dev/null || true
  sleep 0.5
}

start_bot() {
  free_port_and_stop_bot
  echo "▶️ [$(date '+%H:%M:%S')] بالا آوردن bot.py روی پورت $PORT..."
  python3 bot.py &
  BOT_PID=$!
  echo "✅ bot.py با شناسه PID=$BOT_PID فعال شد."
}

trap 'free_port_and_stop_bot; sync_db_to_git' EXIT INT TERM

BOT_PID=""
LAST_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "")
start_bot

LAST_GIT_CHECK=$(date +%s)

while true; do
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TIME))

  # پایان سیکل ۵.۵ ساعته (۳۳۵ دقیقه) جهت اجرای گام Self Re-Trigger
  if [ "$ELAPSED" -ge "$CYCLE_SECONDS" ]; then
    echo "⏰ پایان سیکل ۳۳۵ دقیقه‌ای (۵.۵ ساعت). ذخیره دیتابیس و آماده‌سازی برای انتقال بدون وقفه به سیکل بعدی..."
    free_port_and_stop_bot
    sync_db_to_git
    break
  fi

  # ۱. مکانیزم Watchdog هوشمند: بررسی وضعیت پردازش bot.py
  if ! kill -0 "$BOT_PID" 2>/dev/null; then
    wait "$BOT_PID" 2>/dev/null
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -eq 42 ]; then
      echo "🛑 [Anti-Duplicate] نمونه جدیدتری از ربات روی سرور دیگر فعال است (کد خروج ۴۲). توقف کامل این ورکفلوی تکراری..."
      CONFLICT_DETECTED=1
      rm -f /tmp/consulting_bot_single_instance.lock 2>/dev/null || true
      exit 0
    fi
    echo "⚠️ [Watchdog] پردازش bot.py با کد $EXIT_CODE متوقف شد! ری‌استارت خودکار..."
    start_bot
  fi

  # ۲. مکانیزم Live Hot-Reload (بررسی مخزن هر ۱۰ ثانیه)
  if [ $((NOW - LAST_GIT_CHECK)) -ge 10 ]; then
    LAST_GIT_CHECK=$NOW
    if git fetch origin "$BRANCH" --quiet 2>/dev/null; then
      REMOTE_COMMIT=$(git rev-parse "origin/$BRANCH" 2>/dev/null || echo "$LAST_COMMIT")
      if [ -n "$REMOTE_COMMIT" ] && [ "$REMOTE_COMMIT" != "$LAST_COMMIT" ]; then
        COMMIT_AUTHOR=$(git log -1 --format='%an' "$REMOTE_COMMIT" 2>/dev/null || echo "")
        # اگر کامیت صرفاً سینک خودکار دیتابیس از یک رانر دیگر بود، نیازی به ری‌استارت و بازنویسی دیتابیس زنده نیست
        CODE_CHANGED=$(git diff --name-only "$LAST_COMMIT" "$REMOTE_COMMIT" -- '*.py' '*.sh' 'requirements.txt' 2>/dev/null || echo "")
        if [ "$COMMIT_AUTHOR" = "Bot Auto-Sync" ] && [ -z "$CODE_CHANGED" ]; then
          LAST_COMMIT="$REMOTE_COMMIT"
        else
          echo "🔄 [Hot-Reload] تغییر کد جدید در مخزن شناسایی شد ($LAST_COMMIT -> $REMOTE_COMMIT)!"
          REQ_CHANGED=$(git diff --name-only "$LAST_COMMIT" "$REMOTE_COMMIT" -- "requirements.txt" 2>/dev/null || echo "")

          # توقف ایمن ربات و بکاپ گرفتن از دیتابیس فعلیِ در حال کار قبل از آپدیت کدها
          free_port_and_stop_bot
          flush_and_backup_db

          # دانلود آنی تغییرات مخزن
          git reset --hard "origin/$BRANCH" --quiet
          git clean -fd --exclude="$DB_FILE" --exclude="${DB_FILE}-wal" --exclude="${DB_FILE}-shm" --quiet

          # همیشه دیتابیس زنده فعلی را حفظ کن (مگر اینکه دیتابیس فعلی وجود نداشته باشد)
          if [ -f "/tmp/hotreload_backup_${DB_FILE}" ]; then
            cp -f "/tmp/hotreload_backup_${DB_FILE}" "$DB_FILE"
          fi

          if [ -n "$REQ_CHANGED" ] && [ -f "requirements.txt" ]; then
            pip install -q -r requirements.txt || true
          fi

          LAST_COMMIT="$REMOTE_COMMIT"
          chmod +x run_workflow.sh 2>/dev/null || true
          echo "⚡ [Hot-Reload] آزادسازی پورت $PORT و بالا آوردن bot.py با کد جدید..."
          start_bot
        fi
      fi
    fi
  fi

  sleep 2
done
