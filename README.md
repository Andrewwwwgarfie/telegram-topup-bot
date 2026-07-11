# MLBB Auto Top-Up Telegram Bot

Telegram bot for MLBB / Magic Chess top-ups through the Smile One proxy API
(`rkr.shalsmileapi.site`), with purchase receipts, 7-day usage history and price lists.

Two modes (`PERSONAL_MODE` in `.env`):

- `PERSONAL_MODE=true` (current): only `ADMIN_IDS` can use the bot. There is no
  internal coin wallet — receipts and `.bal` show the **live Smile One balance**
  (BR/PH), fetched before and after each purchase, so "Spent" is the exact real cost.
- `PERSONAL_MODE=false` (shop mode): customers hold a coin balance you top up with
  `.addcoin`; purchases deduct coins at your prices (`.setrate` / `.setprice`).

## Setup

```bash
cd "telegram recharge bot"
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

- `BOT_TOKEN` — create a bot with [@BotFather](https://t.me/BotFather) and paste the token.
- `ADMIN_IDS` — your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot)).

Run manually:

```bash
venv/bin/python bot.py
```

## Permanent free cloud hosting

The production bot is deployed as a Telegram webhook on Cloudflare Workers,
with persistent data in Cloudflare D1. The deployment files are in
`cloudflare/`; credentials are stored as encrypted Worker secrets and are never
committed to this repository. This serverless version does not depend on a Mac
or an always-running process.

Or as an always-on macOS service (auto-starts on login, restarts on crash,
keeps the Mac from idle-sleeping via `caffeinate -i`):

```bash
launchctl load -w ~/Library/LaunchAgents/com.lilhsu.mlbbbot.plist   # start
launchctl unload ~/Library/LaunchAgents/com.lilhsu.mlbbbot.plist    # stop
tail -f bot.log                                                     # logs
```

## First-time setup (as admin, in Telegram)

1. Make sure the Smile One cookies are valid at https://rkr.shalsmileapi.site/cookies
   (currently the API reports "Cookie expired or invalid").
2. `.updateproducts` — pulls the MLBB + Magic Chess product lists (BR and PH) and
   auto-assigns short codes (`wp`, `86`, `172`, … PH items get a `ph` suffix).
3. Set coin prices, either:
   - a global rate per region: `.setrate br 6.5` (coin price = Smile price × rate), or
   - per-product: `.setprice wp 76`
4. Give a user coins: `.addcoin 123456789 5000` (or reply to their message with
   `.addcoin 5000`).

## User commands

| Command | What it does |
|---|---|
| `.mlb 910819251(12610)wp` | Buy a Weekly Pass for that account |
| `.mlb 910819251(12610)wp2` | Buy 2 Weekly Passes |
| `.mlb 910819251(12610)wp+86` | Buy a Weekly Pass + 86 diamonds in one order |
| `.mc <uid>(<zone>)<code>` | Magic Chess: Go Go purchase |
| `.check 910819251(12610)` | Account name, region + double-diamond status |
| `.bal` | Coin balance and order counts |
| `.usecoin` | Last 7 days coin usage, split 🇧🇷 BR / 🇵🇭 PH per day |
| `.price` | MLBB price list |
| `.mcprice` | Magic Chess price list |
| `.help` | Usage guide (Burmese) |

Purchases verify the player name first (no charge if the account doesn't exist),
buy each item sequentially, only charge coins for successful orders, and reply
with a receipt showing serials, Initial/Spent/Assets and Success/Fail counts.

## Admin commands

`.addcoin`, `.setrate`, `.setprice`, `.setcode old new`, `.updateproducts`,
`.smilebal`, `.ban` / `.unban`, `.users`, `.history [tg_id]`

## Data

Everything is stored in `bot.db` (SQLite): users/balances, products+codes,
and a full transaction ledger (used by `.usecoin` and `.history`).
