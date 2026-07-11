"""SQLite storage for users, products, prices and transactions."""

import re
import sqlite3
import threading
from datetime import datetime, timedelta

import config

_lock = threading.Lock()
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    tg_id      INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    balance    REAL    DEFAULT 0,
    banned     INTEGER DEFAULT 0,
    created_at TEXT    DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS products(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    game             TEXT NOT NULL,              -- mlbb | gogo
    region           TEXT NOT NULL,              -- br | ph
    smile_product_id TEXT NOT NULL,
    title            TEXT NOT NULL,
    api_price        REAL,
    coin_price       REAL,                       -- admin override, NULL = api_price * rate
    code             TEXT UNIQUE,
    active           INTEGER DEFAULT 1,
    UNIQUE(game, region, smile_product_id)
);
CREATE TABLE IF NOT EXISTS transactions(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id      INTEGER NOT NULL,
    kind       TEXT NOT NULL,                    -- purchase | topup | deduct
    game       TEXT,
    region     TEXT,
    code       TEXT,
    title      TEXT,
    qty        INTEGER DEFAULT 1,
    coins      REAL NOT NULL,
    serial     TEXT,
    game_uid   TEXT,
    zone_id    TEXT,
    player     TEXT,
    status     TEXT,                             -- success | fail
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

with _lock:
    _conn.executescript(SCHEMA)
    _conn.commit()


def _exec(sql, params=()):
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _query(sql, params=()):
    with _lock:
        return _conn.execute(sql, params).fetchall()


# ---------------------------------------------------------------- settings

def get_setting(key, default=None):
    rows = _query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key, value):
    _exec(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_rate(region):
    try:
        return float(get_setting(f"rate_{region}", "1"))
    except ValueError:
        return 1.0


# ---------------------------------------------------------------- users

def ensure_user(tg_id, username=None, first_name=None):
    _exec(
        "INSERT INTO users(tg_id, username, first_name) VALUES(?,?,?) "
        "ON CONFLICT(tg_id) DO UPDATE SET username=COALESCE(?,username), "
        "first_name=COALESCE(?,first_name)",
        (tg_id, username, first_name, username, first_name),
    )


def get_user(tg_id):
    rows = _query("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    return rows[0] if rows else None


def add_balance(tg_id, amount):
    _exec("UPDATE users SET balance = balance + ? WHERE tg_id=?", (amount, tg_id))


def set_banned(tg_id, banned):
    _exec("UPDATE users SET banned=? WHERE tg_id=?", (1 if banned else 0, tg_id))


def all_users():
    return _query("SELECT * FROM users ORDER BY created_at")


# ---------------------------------------------------------------- products

# Smile One PH truncates MLBB titles to just the bonus ("+1" = 10+1 = 11 diamonds)
PH_BONUS_TOTAL = {
    "1": "11", "2": "22", "5": "56", "10": "112", "20": "223",
    "33": "336", "66": "570", "156": "1163", "383": "2398", "1007": "6042",
}


def _slug(title):
    t = title.lower()
    m = re.search(r"diamond\s*[×x]\s*(\d+)\s*\+\s*(\d+)", t)
    if m:  # "Diamond×78+8" -> 86 (total diamonds, the usual reseller code)
        return str(int(m.group(1)) + int(m.group(2)))
    m = re.fullmatch(r"\+(\d+)", t.strip())
    if m:
        return PH_BONUS_TOTAL.get(m.group(1), m.group(1))
    if "passe semanal" in t or "weekly" in t:
        return "wp"
    if "pacote semanal" in t:
        return "elite"
    if "pacote mensal" in t:
        return "epic"
    if "crepúsculo" in t or "crepusculo" in t or "twilight" in t:
        return "tw"
    m = re.match(r"^\s*\+?(\d+)", t)
    if m:
        return m.group(1)
    return re.sub(r"[^a-z0-9]", "", t)[:8] or "item"


def _normalize_title(title):
    """Translate Smile One titles (Portuguese / truncated) to clean English."""
    m = re.search(r"diamond\s*[×x]\s*(\d+)\s*\+\s*(\d+)", title.lower())
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return f"{a + b} Diamonds ({a}+{b})"
    m = re.fullmatch(r"\+(\d+)", title.strip())
    if m and m.group(1) in PH_BONUS_TOTAL:
        total = PH_BONUS_TOTAL[m.group(1)]
        return f"{total} Diamonds ({int(total) - int(m.group(1))}+{m.group(1)})"
    t = title.lower()
    if "pacote de valor" in t:
        return "Limited-Time Value Pack"
    if "passe semanal" in t or "weekly diamond pass" in t:
        return "Weekly Diamond Pass"
    if "crepúsculo" in t or "crepusculo" in t or "twilight" in t:
        return "Twilight Pass"
    if "pacote semanal elite" in t or "weekly elite bundle" in t:
        return "Weekly Elite Bundle (1x per week)"
    if "pacote mensal" in t or "monthly epic bundle" in t:
        return "Monthly Epic Bundle (1x per month)"
    if "lukas" in t:
        return "Lukas's Battle Bounty (Lv.3+, once only)"
    if "batalhe por descontos" in t or "battle for discounts" in t:
        return "Battle for Discounts (Lv.5+, per 14 days)"
    return title


def _parse_price(val):
    """'R$ 4,00' / '₱ 9.50' / 'R$ 1.234,56' / 12.5 -> float"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = re.sub(r"[^\d.,]", "", str(val))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):   # 1.234,56 -> comma is decimal
            s = s.replace(".", "").replace(",", ".")
        else:                             # 1,900.00 -> dot is decimal
            s = s.replace(",", "")
    elif "," in s:
        tail = s.rpartition(",")[2]
        if len(tail) == 3:                # 1,900 -> thousands separator
            s = s.replace(",", "")
        else:                             # 4,00 -> decimal comma
            s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _unique_code(base):
    code, n = base, 2
    while True:
        rows = _query("SELECT 1 FROM products WHERE code=?", (code,))
        if not rows:
            return code
        code = f"{base}{n}"
        n += 1


def upsert_products(game, region, items):
    """items: list of dicts with id/title/price from the Smile API."""
    count = 0
    for it in items:
        pid = str(it.get("smile_product_id") or it.get("product_id") or it.get("pid") or "")
        title = str(it.get("smile_title") or it.get("title") or it.get("name") or "").strip()
        price = _parse_price(it.get("smile_price", it.get("price")))
        if not pid or not title:
            continue
        title = _normalize_title(title)
        existing = _query(
            "SELECT id FROM products WHERE game=? AND region=? AND smile_product_id=?",
            (game, region, pid),
        )
        if existing:
            _exec(
                "UPDATE products SET title=?, api_price=?, active=1 WHERE id=?",
                (title, price, existing[0]["id"]),
            )
        else:
            base = _slug(title)
            if game == "gogo":
                base = "g" + base
            if region == "ph":
                base = base + "ph"
            code = _unique_code(base)
            _exec(
                "INSERT INTO products(game,region,smile_product_id,title,api_price,code) "
                "VALUES(?,?,?,?,?,?)",
                (game, region, pid, title, price, code),
            )
        count += 1
    return count


def add_product(game, region, pid, code, coins, title):
    """Manually register a product. Returns False if the code is taken."""
    code = code.lower()
    existing = _query(
        "SELECT id, code FROM products WHERE game=? AND region=? AND smile_product_id=?",
        (game, region, pid),
    )
    clash = _query("SELECT id FROM products WHERE code=?", (code,))
    if clash and not (existing and clash[0]["id"] == existing[0]["id"]):
        return False
    if existing:
        _exec(
            "UPDATE products SET title=?, coin_price=?, code=?, active=1 WHERE id=?",
            (title, coins, code, existing[0]["id"]),
        )
    else:
        _exec(
            "INSERT INTO products(game,region,smile_product_id,title,coin_price,code) "
            "VALUES(?,?,?,?,?,?)",
            (game, region, pid, title, coins, code),
        )
    return True


def product_by_code(code):
    rows = _query("SELECT * FROM products WHERE code=? AND active=1", (code.lower(),))
    return rows[0] if rows else None


def products_for_game_region(game, region):
    return _query(
        "SELECT * FROM products WHERE game=? AND region=? AND active=1 "
        "ORDER BY COALESCE(api_price, coin_price)",
        (game, region),
    )


def products_for_game(game):
    return _query(
        "SELECT * FROM products WHERE game=? AND active=1 ORDER BY region, "
        "COALESCE(api_price, coin_price)",
        (game,),
    )


def all_codes(game):
    return [r["code"] for r in _query(
        "SELECT code FROM products WHERE game=? AND active=1", (game,))]


def set_coin_price(code, coins):
    cur = _exec("UPDATE products SET coin_price=? WHERE code=?", (coins, code.lower()))
    return cur.rowcount


def set_code(old, new):
    if product_by_code(new):
        return 0
    cur = _exec("UPDATE products SET code=? WHERE code=?", (new.lower(), old.lower()))
    return cur.rowcount


def effective_price(product):
    if product["coin_price"] is not None:
        return float(product["coin_price"])
    if product["api_price"] is not None:
        return float(product["api_price"]) * get_rate(product["region"])
    return None


# ---------------------------------------------------------------- transactions

def record_tx(**kw):
    cols = ("tg_id", "kind", "game", "region", "code", "title", "qty",
            "coins", "serial", "game_uid", "zone_id", "player", "status")
    vals = [kw.get(c) for c in cols]
    _exec(
        f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        vals,
    )


def usage_last_7_days(tg_id):
    """Per-day, per-region coin spend for the past 7 days (successful purchases)."""
    since = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d 00:00:00")
    rows = _query(
        "SELECT date(created_at) AS d, region, SUM(coins) AS total "
        "FROM transactions WHERE tg_id=? AND kind='purchase' AND status='success' "
        "AND created_at >= ? GROUP BY d, region",
        (tg_id, since),
    )
    usage = {}
    for r in rows:
        usage.setdefault(r["d"], {})[r["region"] or "?"] = float(r["total"] or 0)
    return usage


def success_fail_counts(tg_id):
    rows = _query(
        "SELECT status, COUNT(*) AS n FROM transactions "
        "WHERE tg_id=? AND kind='purchase' GROUP BY status",
        (tg_id,),
    )
    d = {r["status"]: r["n"] for r in rows}
    return d.get("success", 0), d.get("fail", 0)


def user_history(tg_id, limit=15):
    return _query(
        "SELECT * FROM transactions WHERE tg_id=? ORDER BY id DESC LIMIT ?",
        (tg_id, limit),
    )
