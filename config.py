import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x)
    for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split()
    if x.strip().lstrip("-").isdigit()
}
SMILE_API_BASE = os.getenv("SMILE_API_BASE", "https://rkr.shalsmileapi.site/api").rstrip("/")
CHECK_API_BASE = os.getenv("CHECK_API_BASE", "https://chk.shalsmileapi.site").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Personal mode: only ADMIN_IDS can use the bot, no internal coin wallet —
# balances shown in receipts come live from the Smile One account.
PERSONAL_MODE = os.getenv("PERSONAL_MODE", "false").strip().lower() in ("1", "true", "yes")

# Your smile.one login cookies (whole Cookie header string) — used by .recharge
# to redeem activation codes directly on smile.one.
SMILE_COOKIE = os.getenv("SMILE_COOKIE", "").strip()
