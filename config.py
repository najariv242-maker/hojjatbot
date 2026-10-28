import os

# Telegram Bot Token from @BotFather
# از or استفاده شده تا اگر در GitHub Actions متغیر BOT_TOKEN خالی ("") رد شد، از توکن داخل کد استفاده کند
BOT_TOKEN: str = (os.getenv("BOT_TOKEN") or "").strip() or "8872293247:AAGFYxZV1Efj3pSRgQmPF8DzEKcubix1v9U"

# Telegram numeric ID for the admin/consultant
raw_admin_id = (os.getenv("ADMIN_ID") or "").strip() or "1176839639"
try:
    ADMIN_ID: int = int(raw_admin_id)
except ValueError:
    ADMIN_ID: int = 1176839639

# Telegram username for the admin/consultant
ADMIN_USERNAME: str = (os.getenv("ADMIN_USERNAME") or "").strip() or "Hojjat_hshmi"

# Database file path
DB_PATH: str = (os.getenv("DB_PATH") or "").strip() or "consulting_bot.db"

# Port for health-check web server (7860 default for Workflow / Hot-Reload)
raw_port = (os.getenv("PORT") or "").strip() or "7860"
try:
    PORT: int = int(raw_port)
except ValueError:
    PORT: int = 7860

# Optional external ping URL
RENDER_EXTERNAL_URL: str = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PING_URL") or "").strip()
