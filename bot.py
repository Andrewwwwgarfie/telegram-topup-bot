"""MLBB / Magic Chess auto top-up Telegram bot.

User commands (dot-prefixed, work in groups and DMs):
  .mlb <uid>(<zone>)<code>     buy MLBB item(s), e.g.  .mlb 910819251(12610)wp2
  .mc  <uid>(<zone>)<code>     buy Magic Chess item(s)
  .check <uid>(<zone>)         region + account check
  .bal                         your coin balance
  .usecoin                     last 7 days coin usage per region
  .price                       MLBB price list
  .mcprice                     Magic Chess price list
  .help                        usage guide

Admin commands:
  .addcoin <tg_id> <amount>    add coins (negative to deduct); works as reply too
  .setrate <br|ph> <rate>      coin rate = api price x rate
  .setprice <code> <coins>     fixed coin price for a product
  .setcode <old> <new>         rename a product code
  .updateproducts              re-fetch products from the Smile API
  .smilebal                    Smile One account balance
  .ban / .unban <tg_id>        block or unblock a user
  .users                       list users
  .history [tg_id]             recent transactions
"""

import asyncio
import html
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import config
import db
import smile

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")

COIN = "🪙"
LINE = "-" * 30
_user_locks = defaultdict(asyncio.Lock)


def fmt(n):
    return f"{n:,.2f}"


def is_admin(tg_id):
    return tg_id in config.ADMIN_IDS


def err_text(e):
    """Readable error text — timeouts and the like have an empty str()."""
    return str(e) or type(e).__name__


def friendly_reason(msg):
    """Turn a raw API failure message into something readable on a receipt."""
    if not msg:
        return "unknown error"
    low = str(msg).lower()
    if any(k in low for k in ("limit", "maximum", "exceed", "purchase limit",
                              "can only", "reached", "up to")):
        return "purchase limit reached for this account"
    if "timeout" in low or "readtimeout" in low:
        return "network timeout"
    if msg.strip() in ("500", "502", "503"):
        return "Smile One server error (try again)"
    return str(msg)


async def get_balances(regions):
    """Fetch several region balances concurrently: {region: float|None}."""
    results = await asyncio.gather(
        *(smile.region_balance(r) for r in regions), return_exceptions=True
    )
    return {
        r: (v if not isinstance(v, Exception) else None)
        for r, v in zip(regions, results)
    }


async def reply(update, text, pre=False):
    # <code> = monospace without the copy-block chrome (bigger, full-width text)
    if pre:
        text = f"<code>{html.escape(text)}</code>"
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def edit_or_send(update, note, text, pre=False):
    """Turn a status note into the final message (fallback: new message)."""
    if pre:
        text = f"<code>{html.escape(text)}</code>"
    try:
        await note.edit_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception:
        try:
            await note.delete()
        except Exception:
            pass
        await update.message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )




# ------------------------------------------------------------- parsing

TARGET_RE = re.compile(r"^(\d{4,12})\s*[(\s]\s*(\d{2,8})\s*\)?\s*(.*)$")


def parse_target(arg):
    """'910819251(12610)wp2' or '910819251 12610 wp2' -> (uid, zone, 'wp2')"""
    m = TARGET_RE.match(arg.strip())
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3).strip().lower()


def parse_items(spec, game, region):
    """'wp2+86' -> [(product, qty), ...] within one game+region; None on unknown code.

    Users can drop the region/game affixes: for ph, '11' matches '11ph';
    for gogo, '86' matches 'g86' (and 'g86ph' for gogo ph).
    """
    aliases = {}
    for p in db.products_for_game_region(game, region):
        variants = {p["code"]}
        if region == "ph" and p["code"].endswith("ph"):
            variants.add(p["code"][:-2])
        if game == "gogo":
            for v in list(variants):
                if v.startswith("g"):
                    variants.add(v[1:])
        for v in variants:
            if v:
                aliases.setdefault(v, p)
    codes = sorted(aliases, key=len, reverse=True)
    items = []
    for part in re.split(r"[+,]", spec.replace(" ", "")):
        if not part:
            continue
        matched = None
        for code in codes:
            if part == code:
                matched = (code, 1)
                break
            if part.startswith(code):
                rest = part[len(code):].lstrip("x*")
                if rest.isdigit() and 1 <= int(rest) <= 20:
                    matched = (code, int(rest))
                    break
        if not matched:
            return None
        items.append((aliases[matched[0]], matched[1]))
    return items or None


# ------------------------------------------------------------- purchase

async def cmd_buy(update, args, game, region):
    user = update.effective_user
    tg_id = user.id
    row = db.get_user(tg_id)
    if row and row["banned"]:
        return await reply(update, "🚫 Your account is banned.")

    parsed = parse_target(args)
    if not parsed:
        return await reply(
            update,
            "❌ Format မှားနေပါတယ်။\nUsage: <code>.mlb 910819251 12610 wp</code>",
        )
    uid, zone, spec = parsed
    if not spec:
        return await reply(update, "❌ Package code ထည့်ပေးပါ။ ဥပမာ: <code>wp</code>, <code>86</code>, <code>wp2</code>")

    items = parse_items(spec, game, region)
    if not items:
        flag = "🇧🇷 BR" if region == "br" else "🇵🇭 PH"
        return await reply(
            update,
            f"❌ Package code <code>{html.escape(spec)}</code> ကို {flag} မှာ မတွေ့ပါ။ "
            f"<code>.price</code> နဲ့ စျေးနှုန်းစာရင်း ကြည့်ပါ။",
        )

    total = 0.0
    for product, qty in items:
        price = db.effective_price(product)
        if price is None and not config.PERSONAL_MODE:
            return await reply(
                update, f"❌ <code>{product['code']}</code> အတွက် စျေးနှုန်း မသတ်မှတ်ရသေးပါ။"
            )
        total += (price or 0) * qty

    regions = []
    for product, _ in items:
        if product["region"] not in regions:
            regions.append(product["region"])

    async with _user_locks[tg_id]:
        balance = 0.0
        # verify the player and read the starting balance at the same time,
        # and post the status note concurrently — all independent work
        role_task = asyncio.ensure_future(smile.check_role(uid, zone))
        bal_task = (asyncio.ensure_future(get_balances(regions))
                    if config.PERSONAL_MODE else None)
        note_task = asyncio.ensure_future(
            update.message.reply_text("⏳ Processing order ...")
        )

        if not config.PERSONAL_MODE:
            row = db.get_user(tg_id)
            balance = row["balance"] if row else 0.0
            if balance < total:
                role_task.cancel(); note_task.cancel()
                return await reply(
                    update,
                    f"❌ Coin မလုံလောက်ပါ။\nလိုအပ်သည် : {fmt(total)} {COIN}\n"
                    f"လက်ကျန်   : {fmt(balance)} {COIN}",
                )

        try:
            role = await role_task
        except Exception as e:
            log.exception("check-role failed")
            note_task.cancel()
            return await reply(update, f"❌ Account check failed: {html.escape(err_text(e))}")
        result = role.get("result") or {}
        player = result.get("username")
        note = await note_task
        initial = (await bal_task) if bal_task else {}
        if not role.get("success") or result.get("code") != 200 or not player:
            try:
                await note.delete()
            except Exception:
                pass
            return await reply(
                update,
                f"❌ Game ID <code>{uid} ({zone})</code> ကို ရှာမတွေ့ပါ။ ID/Server ပြန်စစ်ပေးပါ။",
            )

        success, fail = 0, 0
        spent = 0.0
        order_lines = []
        ok_units = []      # one entry per successful diamond/item unit
        fail_units = []
        fail_notes = []       # human-readable reasons for the receipt
        for product, qty in items:
            price = db.effective_price(product) or 0.0
            unit_ok = 0
            last_err = None
            for _ in range(qty):
                try:
                    res = await smile.purchase(game, uid, zone, product["smile_product_id"])
                except Exception as e:
                    log.exception("purchase failed")
                    res = {"success": False, "message": err_text(e)}
                if res.get("success"):
                    unit_ok += 1
                    success += 1
                    spent += price
                    ok_units.append({"product": product, "price": price, "serial": "-"})
                else:
                    fail += 1
                    last_err = str(res.get("message") or "")[:120]
                    fail_units.append((product, last_err))
            unit_fail = qty - unit_ok
            if unit_ok == qty:
                mark = "✅"
            elif unit_ok:
                mark = "⚠️"
            else:
                mark = "❌"
            # show how many of the requested quantity actually went through
            if qty > 1:
                order_lines.append(f"{product['title']} {mark} {unit_ok}/{qty}")
            else:
                order_lines.append(f"{product['title']} {mark}")
            if unit_fail:
                reason = friendly_reason(last_err)
                fail_notes.append(f"{product['title']}: {unit_fail} failed — {reason}")

        # pull real smile.one Order IDs; fetch the final balance at the same time
        async def fetch_order_ids():
            if not (ok_units and config.SMILE_COOKIE):
                return
            need_by_pid = {}
            for u in ok_units:
                need_by_pid[u["product"]["smile_product_id"]] = \
                    need_by_pid.get(u["product"]["smile_product_id"], 0) + 1
            found = {}
            for attempt in range(2):
                try:
                    orders = await smile.recent_orders(limit=30)
                except Exception:
                    orders = []
                for pid, need in need_by_pid.items():
                    found[pid] = [
                        str(o["increment_id"]) for o in orders
                        if str(o.get("goods_id")) == str(pid)
                        and str(o.get("user_id")) == str(uid)
                        and str(o.get("server_id")) == str(zone)
                        and str(o.get("status")) in ("1", "success", "Success")
                        and o.get("increment_id")
                    ][:need]
                if all(len(found.get(p, [])) >= n for p, n in need_by_pid.items()):
                    break
                if attempt == 0:
                    await asyncio.sleep(1.2)
            cursor = {pid: 0 for pid in need_by_pid}
            for u in ok_units:
                pid = u["product"]["smile_product_id"]
                ids = found.get(pid, [])
                if cursor[pid] < len(ids):
                    u["serial"] = ids[cursor[pid]]
                    cursor[pid] += 1

        assets = {}
        if config.PERSONAL_MODE:
            _, assets = await asyncio.gather(fetch_order_ids(), get_balances(regions))
        else:
            await fetch_order_ids()

        # persist transactions now that serials are known
        for u in ok_units:
            p = u["product"]
            db.record_tx(
                tg_id=tg_id, kind="purchase", game=game, region=p["region"],
                code=p["code"], title=p["title"], qty=1, coins=u["price"],
                serial=u["serial"], game_uid=uid, zone_id=zone,
                player=player, status="success",
            )
        for p, msg in fail_units:
            db.record_tx(
                tg_id=tg_id, kind="purchase", game=game, region=p["region"],
                code=p["code"], title=p["title"], qty=1, coins=0,
                serial=msg, game_uid=uid, zone_id=zone,
                player=player, status="fail",
            )
        serials = [u["serial"] for u in ok_units]

        if not config.PERSONAL_MODE and spent:
            db.add_balance(tg_id, -spent)

        name = user.username or user.first_name or str(tg_id)
        game_name = "MLBB" if game == "mlbb" else "Magic Chess"
        now = datetime.now().strftime("%I:%M:%S%p %d.%m.%Y")

        lines = [f"==== {game_name} Purchase Receipt ====", ""]
        lines.append(f"UID    : {uid} ({zone})")
        lines.append(f"Name   : {player}")
        lines.append(f"Order  : {order_lines[0]}")
        for extra in order_lines[1:]:
            lines.append(f"         {extra}")
        shown = [s for s in serials if s and s != "-"]
        if shown:
            lines.append(f"Serial : {shown[0]}")
            for s in shown[1:]:
                lines.append(f"         {s}")
        if config.PERSONAL_MODE:
            flags = {"br": "🇧🇷 BR", "ph": "🇵🇭 PH"}
            real_spent = sum(
                initial[r] - assets[r]
                for r in regions
                if initial.get(r) is not None and assets.get(r) is not None
            )
            lines.append(f"Spent  : {fmt(real_spent)} {COIN}")
            lines.append(LINE)
            lines.append(f"Date   : {now}")
            lines.append(f"========= {name} =========")
            if len(regions) == 1:
                r = regions[0]
                ini, ast = initial.get(r), assets.get(r)
                lines.append(f"Initial: {fmt(ini) if ini is not None else 'N/A'} {COIN}")
                if ini is not None and ast is not None:
                    lines.append(f"Spent  : {fmt(ini - ast)} {COIN}")
                lines.append(f"Assets : {fmt(ast) if ast is not None else 'N/A'} {COIN}")
            else:
                for r in regions:
                    ini, ast = initial.get(r), assets.get(r)
                    lines.append(f"{flags[r]}")
                    lines.append(f"Initial: {fmt(ini) if ini is not None else 'N/A'} {COIN}")
                    if ini is not None and ast is not None:
                        lines.append(f"Spent  : {fmt(ini - ast)} {COIN}")
                    lines.append(f"Assets : {fmt(ast) if ast is not None else 'N/A'} {COIN}")
        else:
            new_balance = db.get_user(tg_id)["balance"]
            lines.append(f"Spent  : {fmt(spent)} {COIN}")
            lines.append(LINE)
            lines.append(f"Date   : {now}")
            lines.append(f"========= {name} =========")
            lines.append(f"Initial: {fmt(balance)} {COIN}")
            lines.append(f"Spent  : {fmt(spent)} {COIN}")
            lines.append(f"Assets : {fmt(new_balance)} {COIN}")
        if fail_notes:
            lines.append("")
            for note_line in fail_notes:
                lines.append(f"⚠️ {note_line}")
        lines.append("")
        lines.append(f"Success {success} / Fail {fail}")

        # <code> keeps the monospace look without the copy-block chrome
        receipt = f"<code>{html.escape(chr(10).join(lines))}</code>"
        try:
            await note.edit_text(receipt, parse_mode=ParseMode.HTML)
        except Exception:
            try:
                await note.delete()
            except Exception:
                pass
            await update.message.reply_text(receipt, parse_mode=ParseMode.HTML)


# ------------------------------------------------------------- info commands

async def cmd_check(update, args):
    arg = args.replace("(", " ").replace(")", " ")
    parts = arg.split()
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        return await reply(update, "Usage: <code>.check 910819251 12610</code>")
    uid, zone = parts[0], parts[1]
    note_task = asyncio.ensure_future(
        update.message.reply_text("🔍 Checking MLBB... Please wait.")
    )
    try:
        data = await smile.region_check(uid, zone)
    except Exception as e:
        data = {"success": False, "_err": str(e) or type(e).__name__}
    note = await note_task
    if data.get("_err"):
        return await edit_or_send(
            update, note, f"❌ Check failed: {html.escape(data['_err'])}"
        )
    if not data.get("success"):
        return await edit_or_send(update, note, "❌ Account ကို ရှာမတွေ့ပါ။")
    lines = [
        "==== MLBB Account Check ====",
        "",
        f"UID    : {uid} ({zone})",
        f"Name   : {data.get('username') or 'N/A'}",
        f"Region : {data.get('region') or 'N/A'} ({data.get('region_code') or '-'})",
    ]
    if data.get("created_date"):
        lines.append(f"Created: {data['created_date']}")
    events = data.get("double_diamond_events") or []
    if events:
        lines.append("")
        lines.append("Double Diamond Events:")
        for ev in events:
            lines.append(f"  {ev.get('title')}: {ev.get('status')}")
    await edit_or_send(update, note, "\n".join(lines), pre=True)


async def cmd_bal(update):
    s, f = db.success_fail_counts(update.effective_user.id)
    if config.PERSONAL_MODE:
        bals = await get_balances(("br", "ph"))
        br, ph = bals["br"], bals["ph"]
        return await reply(
            update,
            f"💰 Smile One Balance\n"
            f"🇧🇷 BR : <b>{fmt(br) if br is not None else 'N/A'}</b> {COIN}\n"
            f"🇵🇭 PH : <b>{fmt(ph) if ph is not None else 'N/A'}</b> {COIN}\n"
            f"📦 Orders : Success {s} / Fail {f}",
        )
    row = db.get_user(update.effective_user.id)
    balance = row["balance"] if row else 0.0
    await reply(
        update,
        f"👤 {html.escape(update.effective_user.first_name or '')}\n"
        f"💰 Balance : <b>{fmt(balance)}</b> {COIN}\n"
        f"📦 Orders  : Success {s} / Fail {f}",
    )


async def cmd_usecoin(update):
    tg_id = update.effective_user.id
    usage = db.usage_last_7_days(tg_id)
    today = datetime.now().date()
    lines = ["==== Coin Usage (Last 7 Days) ====", ""]
    total = 0.0
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        day_usage = usage.get(key, {})
        br = day_usage.get("br", 0.0)
        ph = day_usage.get("ph", 0.0)
        total += br + ph
        lines.append(f"📅 {day.strftime('%d.%m.%Y')}")
        lines.append(f"   🇧🇷 BR Used : {fmt(br)} {COIN}")
        lines.append(f"   🇵🇭 PH Used : {fmt(ph)} {COIN}")
    lines.append(LINE)
    lines.append(f"Total Used : {fmt(total)} {COIN}")
    await reply(update, "\n".join(lines), pre=True)


async def cmd_price(update, args, game):
    products = db.products_for_game(game)
    priced = [(p, db.effective_price(p)) for p in products]
    priced = [(p, pr) for p, pr in priced if pr is not None]
    if not priced:
        return await reply(
            update,
            "❌ Product list မရှိသေးပါ။ Admin က <code>.updateproducts</code> အရင် run ပေးပါ။",
        )
    game_name = "MLBB" if game == "mlbb" else "Magic Chess"
    headers = {
        ("mlbb", "br"): f"{game_name} Dias price 🇧🇷 BR (.mlb):",
        ("mlbb", "ph"): f"{game_name} Dias price 🇵🇭 PH (.mlp):",
        ("gogo", "br"): f"{game_name} Dias price 🇧🇷 BR (.mc):",
        ("gogo", "ph"): f"{game_name} Dias price 🇵🇭 PH (.mcp):",
    }

    def display_code(p):
        code = p["code"]
        if p["region"] == "ph" and code.endswith("ph"):
            code = code[:-2]
        if game == "gogo" and code.startswith("g"):
            code = code[1:]
        return code or p["code"]

    def simple_price(v):
        s = f"{v:,.2f}".rstrip("0").rstrip(".")
        return s or "0"

    lines = []
    for region in ("br", "ph"):
        rows = [(p, pr) for p, pr in priced if p["region"] == region]
        if not rows:
            continue
        if lines:
            lines.append("")
        lines.append(headers[(game, region)])
        for p, pr in rows:
            lines.append(f"{display_code(p)} = {simple_price(pr)} {COIN}")
    await reply(update, "\n".join(lines))


HELP_TEXT = """📖 Bot အသုံးပြုနည်း

1️⃣ MLBB Diamond ဝယ်ရန်
   🇧🇷 Brazil      : .mlb GameID ServerID Code
   🇵🇭 Philippines : .mlp GameID ServerID Code
   ဥပမာ : .mlb 910819251 12610 wp
          .mlp 334649758 9664 11
   အများဝယ်ရန် : .mlb 910819251 12610 wp2
   ပေါင်းဝယ်ရန် : .mlb 910819251 12610 wp+86

2️⃣ Magic Chess ဝယ်ရန်
   🇧🇷 .mc / 🇵🇭 .mcp GameID ServerID Code

3️⃣ .usecoin — လွန်ခဲ့သော ၇ ရက်စာ Coin သုံးစွဲမှုမှတ်တမ်း
   (🇧🇷 BR / 🇵🇭 PH Region အလိုက် တစ်ရက်ချင်းစီ ပြပေးပါမည်)

4️⃣ .price — MLBB ရောင်းစျေးစာရင်း
   .mcprice — Magic Chess စျေးစာရင်း

5️⃣ .check GameID ServerID — Region နှင့် Account စစ်ရန်

6️⃣ .bal — Smile One Coin လက်ကျန် ကြည့်ရန် (🇧🇷 BR / 🇵🇭 PH)

7️⃣ .recharge Code — Activation Code ဖြင့် Smile One ငွေဖြည့်ရန်"""


# ------------------------------------------------------------- admin

def _target_id(update, parts, idx):
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user.id, idx
    if len(parts) > idx and parts[idx].lstrip("-").isdigit():
        return int(parts[idx]), idx + 1
    return None, idx


async def cmd_addcoin(update, parts):
    tg_id, idx = _target_id(update, parts, 1)
    if tg_id is None or len(parts) <= idx:
        return await reply(update, "Usage: <code>.addcoin tg_id amount</code> (or reply to a user)")
    try:
        amount = float(parts[idx])
    except ValueError:
        return await reply(update, "❌ Amount must be a number.")
    db.ensure_user(tg_id)
    db.add_balance(tg_id, amount)
    db.record_tx(
        tg_id=tg_id, kind="topup" if amount >= 0 else "deduct",
        coins=abs(amount), status="success",
    )
    balance = db.get_user(tg_id)["balance"]
    await reply(
        update,
        f"✅ <code>{tg_id}</code> သို့ {fmt(amount)} {COIN} ထည့်ပြီးပါပြီ။\n"
        f"လက်ကျန် : <b>{fmt(balance)}</b> {COIN}",
    )


async def cmd_updateproducts(update):
    note = await update.message.reply_text("⏳ Fetching products ...")
    report = []
    for game in ("mlbb", "gogo"):
        for region in ("br", "ph"):
            try:
                items = await smile.get_products(game, region)
                n = db.upsert_products(game, region, items)
                report.append(f"{game} {region}: {n} products")
            except Exception as e:
                report.append(f"{game} {region}: failed ({e})")
    try:
        await note.delete()
    except Exception:
        pass
    await reply(update, "📦 Product update\n" + "\n".join(report), pre=True)


async def cmd_recharge(update, parts):
    if len(parts) != 2:
        return await reply(update, "Usage: <code>.recharge ACTIVATION_CODE</code>")
    code = parts[1]
    if not config.SMILE_COOKIE:
        return await reply(
            update,
            "❌ <b>SMILE_COOKIE</b> is not set.\n\n"
            "Add your smile.one login cookies to <code>.env</code>:\n"
            "1. Log in at smile.one in your browser\n"
            "2. DevTools → Network → any request → copy the whole <b>Cookie</b> header\n"
            "3. Add <code>SMILE_COOKIE=...</code> to .env and restart the bot",
        )
    note = await update.message.reply_text("⏳ Redeeming code ...")

    before = await get_balances(("br", "ph"))
    try:
        res = await smile.redeem_code(code)
    except Exception as e:
        log.exception("redeem failed")
        res = {"ok": False, "error": str(e), "message": None}
    after = await get_balances(("br", "ph"))

    try:
        await note.delete()
    except Exception:
        pass

    if res.get("error") == "cookie_expired":
        return await reply(
            update,
            "❌ smile.one cookie expired — update <code>SMILE_COOKIE</code> in .env "
            "and restart the bot.",
        )

    deltas = {
        r: after[r] - before[r]
        for r in ("br", "ph")
        if before.get(r) is not None and after.get(r) is not None
    }
    credited = {r: d for r, d in deltas.items() if d > 0}
    flags = {"br": "🇧🇷 BR", "ph": "🇵🇭 PH"}
    lines = ["==== Smile One Recharge ====", "", f"Code   : {code}"]
    if credited:
        for r, d in credited.items():
            lines.append(f"{flags[r]} : {fmt(before[r])} → {fmt(after[r])}  (+{fmt(d)})")
        lines.append("")
        lines.append("✅ Recharge successful!")
    else:
        for r in ("br", "ph"):
            if after.get(r) is not None:
                lines.append(f"{flags[r]} : {fmt(after[r])} {COIN} (unchanged)")
        lines.append("")
        lines.append("⚠️ Balance did not increase — the code was probably "
                     "not redeemed.")
        if res.get("message"):
            lines.append(f"smile.one says: {res['message']}")
        elif res.get("error"):
            lines.append(f"Error: {res['error']}")
    await reply(update, "\n".join(lines), pre=True)


async def cmd_smilebal(update):
    try:
        data = await smile.balance()
    except Exception as e:
        return await reply(update, f"❌ Failed: {html.escape(err_text(e))}")
    result = data.get("result") or {}
    balances = result.get("balances") or {}
    lines = ["==== Smile One Balance ===="]
    if result.get("username"):
        lines.append(f"Account : {result['username']}")
    lines.append(f"🇧🇷 BR : {balances.get('br')}")
    lines.append(f"🇵🇭 PH : {balances.get('ph')}")
    if not data.get("success"):
        lines.append(f"⚠️ {data.get('result') or data.get('message')}")
    await reply(update, "\n".join(lines), pre=True)


async def cmd_users(update):
    rows = db.all_users()
    if not rows:
        return await reply(update, "No users yet.")
    lines = [f"{'ID':<12} {'Balance':>12}  Name"]
    for r in rows[:60]:
        name = r["username"] or r["first_name"] or ""
        flag = " 🚫" if r["banned"] else ""
        lines.append(f"{r['tg_id']:<12} {fmt(r['balance']):>12}  {name}{flag}")
    lines.append(f"\nTotal: {len(rows)} users")
    await reply(update, "\n".join(lines), pre=True)


async def cmd_history(update, parts):
    tg_id, _ = _target_id(update, parts, 1)
    if tg_id is None:
        tg_id = update.effective_user.id
    rows = db.user_history(tg_id)
    if not rows:
        return await reply(update, "No transactions.")
    lines = [f"==== History {tg_id} ===="]
    for r in rows:
        if r["kind"] == "purchase":
            mark = "✅" if r["status"] == "success" else "❌"
            lines.append(
                f"{r['created_at']} {mark} {r['title']} -> {r['game_uid']}({r['zone_id']}) "
                f"{fmt(r['coins'])} {COIN}"
            )
        else:
            sign = "+" if r["kind"] == "topup" else "-"
            lines.append(f"{r['created_at']} 💰 {r['kind']} {sign}{fmt(r['coins'])} {COIN}")
    await reply(update, "\n".join(lines), pre=True)


# ------------------------------------------------------------- router

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    user = update.effective_user
    if config.PERSONAL_MODE and not is_admin(user.id):
        return
    db.ensure_user(user.id, user.username, user.first_name)

    if text.startswith("/start") or text.startswith("/help") or text == ".help":
        return await reply(update, HELP_TEXT)
    if not text.startswith("."):
        return

    parts = text.split()
    cmd = parts[0].lower()
    args = text[len(parts[0]):].strip()

    try:
        if cmd == ".mlb":
            await cmd_buy(update, args, "mlbb", "br")
        elif cmd in (".mlp", ".mlph"):
            await cmd_buy(update, args, "mlbb", "ph")
        elif cmd in (".mc", ".gogo", ".chess"):
            await cmd_buy(update, args, "gogo", "br")
        elif cmd in (".mcp", ".mcph"):
            await cmd_buy(update, args, "gogo", "ph")
        elif cmd == ".check":
            await cmd_check(update, args)
        elif cmd in (".bal", ".balance", ".mycoin"):
            await cmd_bal(update)
        elif cmd == ".usecoin":
            await cmd_usecoin(update)
        elif cmd in (".price", ".mlbprice"):
            await cmd_price(update, args, "mlbb")
        elif cmd in (".mcprice", ".gogoprice", ".chessprice"):
            await cmd_price(update, args, "gogo")
        # ---- admin ----
        elif cmd in (".addcoin", ".setrate", ".setprice", ".setcode",
                     ".updateproducts", ".smilebal", ".ban", ".unban",
                     ".users", ".history", ".addproduct", ".recharge"):
            if not is_admin(user.id):
                return await reply(update, "🚫 Admin only command.")
            if cmd == ".addcoin":
                await cmd_addcoin(update, parts)
            elif cmd == ".setrate":
                if len(parts) == 3 and parts[1].lower() in ("br", "ph"):
                    try:
                        rate = float(parts[2])
                    except ValueError:
                        return await reply(update, "❌ Rate must be a number.")
                    db.set_setting(f"rate_{parts[1].lower()}", rate)
                    await reply(update, f"✅ {parts[1].upper()} rate = {rate:g} (coin = api price × rate)")
                else:
                    await reply(update, "Usage: <code>.setrate br 6.5</code>")
            elif cmd == ".setprice":
                if len(parts) == 3:
                    try:
                        coins = float(parts[2])
                    except ValueError:
                        return await reply(update, "❌ Price must be a number.")
                    if db.set_coin_price(parts[1], coins):
                        await reply(update, f"✅ <code>{html.escape(parts[1])}</code> = {fmt(coins)} {COIN}")
                    else:
                        await reply(update, "❌ Code not found.")
                else:
                    await reply(update, "Usage: <code>.setprice wp 76</code>")
            elif cmd == ".setcode":
                if len(parts) == 3 and db.set_code(parts[1], parts[2]):
                    await reply(update, f"✅ <code>{html.escape(parts[1])}</code> → <code>{html.escape(parts[2])}</code>")
                else:
                    await reply(update, "❌ Usage: <code>.setcode old new</code> (new must be unused)")
            elif cmd == ".recharge":
                await cmd_recharge(update, parts)
            elif cmd == ".addproduct":
                # .addproduct mlbb br 13 wp 76 Weekly Diamond Pass
                if len(parts) >= 7 and parts[1] in ("mlbb", "gogo") \
                        and parts[2] in ("br", "ph"):
                    try:
                        coins = float(parts[5])
                    except ValueError:
                        return await reply(update, "❌ Coin price must be a number.")
                    title = " ".join(parts[6:])
                    if db.add_product(parts[1], parts[2], parts[3], parts[4], coins, title):
                        await reply(
                            update,
                            f"✅ Added <code>{html.escape(parts[4].lower())}</code> — "
                            f"{html.escape(title)} ({parts[1]} {parts[2].upper()}, "
                            f"pid {html.escape(parts[3])}) = {fmt(coins)} {COIN}",
                        )
                    else:
                        await reply(update, "❌ That code is already used by another product.")
                else:
                    await reply(
                        update,
                        "Usage:\n<code>.addproduct mlbb br 13 wp 76 Weekly Diamond Pass</code>\n"
                        "(game region smile_product_id code coin_price title)",
                    )
            elif cmd == ".updateproducts":
                await cmd_updateproducts(update)
            elif cmd == ".smilebal":
                await cmd_smilebal(update)
            elif cmd in (".ban", ".unban"):
                tg_id, _ = _target_id(update, parts, 1)
                if tg_id is None:
                    return await reply(update, f"Usage: <code>{cmd} tg_id</code>")
                db.ensure_user(tg_id)
                db.set_banned(tg_id, cmd == ".ban")
                await reply(update, f"✅ <code>{tg_id}</code> {'banned 🚫' if cmd == '.ban' else 'unbanned ✅'}")
            elif cmd == ".users":
                await cmd_users(update)
            elif cmd == ".history":
                await cmd_history(update, parts)
    except Exception:
        log.exception("handler error for %s", cmd)
        await reply(update, "❌ Unexpected error. Please try again.")


async def _post_init(app):
    try:
        await smile.warmup()
        log.info("API connections warmed up")
    except Exception:
        pass


def main():
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")
    app = Application.builder().token(config.BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(MessageHandler(filters.TEXT, on_message))
    log.info("Bot starting ...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
