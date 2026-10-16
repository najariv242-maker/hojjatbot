import os

# Telegram Bot Token from @BotFather
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# Telegram numeric ID for the admin/consultant
raw_admin_id = os.getenv("ADMIN_ID", "1176839639")
try:
    ADMIN_ID: int = int(raw_admin_id)
except ValueError:
    ADMIN_ID: int = 1176839639

# Telegram username for the admin/consultant
ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "Hojjat_hshmi")

# Database file path
DB_PATH: str = os.getenv("DB_PATH", "consulting_bot.db")

# Port for Render health-check web server
PORT: int = int(os.getenv("PORT", "10000"))

# Render public URL (automatically provided by Render or set manually)
RENDER_EXTERNAL_URL: str = os.getenv("RENDER_EXTERNAL_URL", os.getenv("PING_URL", ""))
