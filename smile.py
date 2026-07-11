"""Async client for the Smile One proxy API and the region-check API."""

import asyncio

import httpx

import config

# keepalive_expiry is deliberately short: idle bot connections older than this
# are closed instead of reused, which avoids hanging on a server-dropped socket.
_client = httpx.AsyncClient(
    timeout=httpx.Timeout(25.0, connect=10.0),
    follow_redirects=True,
    limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=20),
)

# errors where the request provably never reached the server (safe to retry
# for any method, including purchases)
_PRE_SEND = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# additionally retryable for idempotent reads (the request may have been sent)
_READ = _PRE_SEND + (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError)


async def _request(method, url, retry_on=_READ, **kw):
    """One retry on a fresh connection when a pooled one is stale.

    `retry_on` defaults to the read set; pass `_PRE_SEND` for non-idempotent
    POSTs (purchase) so a request that may already have hit the server is not
    resent.
    """
    for attempt in range(2):
        try:
            return await _client.request(method, url, **kw)
        except retry_on:
            if attempt == 1:
                raise
            await asyncio.sleep(0.2)


async def warmup():
    """Open connections to both API hosts so the first user command is fast."""
    async def ping(url):
        try:
            await _client.get(url, timeout=10)
        except Exception:
            pass
    await asyncio.gather(
        ping(config.SMILE_API_BASE + "/balance/br"),
        ping(config.CHECK_API_BASE + "/check"),
    )

PRODUCT_ENDPOINTS = {
    "mlbb": "/products",
    "gogo": "/gogoproducts",
}
PURCHASE_ENDPOINTS = {
    "mlbb": "/purchase",
    "gogo": "/purchasechess",
}


async def _get(url, params=None):
    r = await _request("GET", url, params=params)
    r.raise_for_status()
    return r.json()


async def get_products(game, region):
    url = config.SMILE_API_BASE + PRODUCT_ENDPOINTS[game]
    data = await _get(url, {"region_slug": region})
    result = data.get("result") or []
    return result if isinstance(result, list) else []


async def check_role(game_id, zone_id):
    url = config.SMILE_API_BASE + "/check-role"
    return await _get(url, {"game_id": game_id, "zone_id": zone_id})


async def purchase(game, game_id, zone_id, smile_product_id):
    url = config.SMILE_API_BASE + PURCHASE_ENDPOINTS[game]
    payload = {
        "game_id": game_id,
        "zone_id": zone_id,
        "smile_product_id": smile_product_id,
    }
    # only retry when the connection never established — never resend a POST
    # that might already have charged the account
    r = await _request("POST", url, data=payload, retry_on=_PRE_SEND)
    try:
        return r.json()
    except ValueError:
        return {"success": False, "message": f"HTTP {r.status_code}", "result": None}


async def balance():
    return await _get(config.SMILE_API_BASE + "/balance")


async def region_balance(region):
    """Live Smile One balance for one region ('br'/'ph'), or None."""
    data = await _get(config.SMILE_API_BASE + f"/balance/{region}")
    result = data.get("result") or {}
    try:
        return float(result.get("balance"))
    except (TypeError, ValueError):
        return None


async def region_check(game_id, server_id):
    url = config.CHECK_API_BASE + "/check"
    return await _get(url, {"game_id": game_id, "server_id": server_id})


SMILE_SITE = "https://www.smile.one"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _site_headers():
    return {
        "Cookie": config.SMILE_COOKIE,
        "User-Agent": _UA,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{SMILE_SITE}/customer/order",
    }


async def recent_orders(limit=20):
    """Latest orders from the smile.one dashboard (needs SMILE_COOKIE).

    Each row includes increment_id (the real Order ID), goods_id
    (= smile_product_id), user_id (game UID), server_id and status.
    """
    if not config.SMILE_COOKIE:
        return []
    params = {
        "type": "orderlist", "p": 1, "pageSize": limit, "status": "",
        "startdate": "", "enddate": "", "order_type": "", "key": "", "user_id": "",
    }
    r = await _request(
        "GET", f"{SMILE_SITE}/customer/activationcode/codelist",
        params=params, headers=_site_headers(),
    )
    try:
        return r.json().get("list") or []
    except ValueError:
        return []


async def match_order_ids(game_uid, zone_id, smile_product_id, need):
    """Return up to `need` recent increment_ids for a just-placed order."""
    if need <= 0 or not config.SMILE_COOKIE:
        return []
    try:
        orders = await recent_orders(limit=max(20, need + 5))
    except Exception:
        return []
    ids = []
    for row in orders:  # newest first
        if (str(row.get("goods_id")) == str(smile_product_id)
                and str(row.get("user_id")) == str(game_uid)
                and str(row.get("server_id")) == str(zone_id)
                and str(row.get("status")) in ("1", "success", "Success")):
            inc = row.get("increment_id")
            if inc:
                ids.append(str(inc))
            if len(ids) >= need:
                break
    return ids


CHECKCARD_MSG = {
    201: "Invalid or non-existent code",
    202: "Code already used",
    203: "Code expired",
}


async def redeem_code(code):
    """Redeem a Smile One activation card via /smilecard/pay/payajax.

    smile.one flow: checkcard (validate) → payajax (credit). `sec` is just
    the code, upper-cased. Returns {"ok", "error", "message"}.
    """
    if not config.SMILE_COOKIE:
        return {"ok": False, "error": "no_cookie", "message": None}
    sec = code.strip().upper()
    headers = {
        "Cookie": config.SMILE_COOKIE,
        "User-Agent": _UA,
        "Referer": f"{SMILE_SITE}/customer/activationcode",
        "X-Requested-With": "XMLHttpRequest",
    }

    def _code(resp):
        try:
            return int(resp.json().get("code", 0))
        except (ValueError, TypeError, AttributeError):
            return 0

    # checkcard is read-only validation — safe to retry fully
    check = await _request(
        "POST", f"{SMILE_SITE}/smilecard/pay/checkcard",
        data={"sec": sec}, headers=headers,
    )
    if "login" in str(check.url) or check.status_code in (301, 302, 401, 403):
        return {"ok": False, "error": "cookie_expired", "message": None}
    cc = _code(check)
    if cc != 200:
        return {"ok": False, "error": None, "message": CHECKCARD_MSG.get(cc, f"Code rejected (code {cc})")}

    # payajax credits the account — retry only if the connection never opened
    pay = await _request(
        "POST", f"{SMILE_SITE}/smilecard/pay/payajax",
        data={"sec": sec}, headers=headers, retry_on=_PRE_SEND,
    )
    pc = _code(pay)
    if pc == 200:
        return {"ok": True, "error": None, "message": "Recharge successful"}
    return {"ok": False, "error": None, "message": f"Redeem failed (code {pc})"}


def find_serial(obj):
    """Recursively look for an order serial / id in a purchase response."""
    keys = ("serial", "sn", "order_id", "orderid", "order_no", "trade_no", "orderId")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys and v:
                return str(v)
        for v in obj.values():
            found = find_serial(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_serial(v)
            if found:
                return found
    return None
