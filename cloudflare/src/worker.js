const COIN = "🪙";
const LINE = "------------------------------";

const html = (s) => String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const fmt = (n) => Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const admins = (env) => new Set(String(env.ADMIN_IDS || "").split(/[ ,]+/).filter(Boolean).map(Number));
const personal = (env) => /^(1|true|yes)$/i.test(env.PERSONAL_MODE || "false");

async function tg(env, method, data) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(data),
  });
  const out = await r.json();
  if (!out.ok) throw new Error(out.description || `Telegram ${r.status}`);
  return out.result;
}

const send = (env, chatId, text, extra = {}) => tg(env, "sendMessage", {
  chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true, ...extra,
});
const edit = (env, chatId, messageId, text) => tg(env, "editMessageText", {
  chat_id: chatId, message_id: messageId, text, parse_mode: "HTML", disable_web_page_preview: true,
});

async function apiJSON(url, options = {}) {
  const r = await fetch(url, { ...options, signal: AbortSignal.timeout(25000) });
  const text = await r.text();
  let out;
  try { out = JSON.parse(text); } catch { throw new Error(`HTTP ${r.status}`); }
  if (!r.ok) throw new Error(out.message || `HTTP ${r.status}`);
  return out;
}

function smileURL(env, path, params) {
  const u = new URL(String(env.SMILE_API_BASE).replace(/\/$/, "") + path);
  for (const [k, v] of Object.entries(params || {})) u.searchParams.set(k, v);
  return u;
}
const getProducts = (env, game, region) => apiJSON(smileURL(env, game === "mlbb" ? "/products" : "/gogoproducts", { region_slug: region })).then(x => Array.isArray(x.result) ? x.result : []);
const checkRole = (env, uid, zone) => apiJSON(smileURL(env, "/check-role", { game_id: uid, zone_id: zone }));
const regionBalance = async (env, region) => {
  try {
    const x = await apiJSON(smileURL(env, `/balance/${region}`));
    const n = Number(x.result?.balance);
    return Number.isFinite(n) ? n : null;
  } catch { return null; }
};
const balances = async (env, regions) => Object.fromEntries(await Promise.all(regions.map(async r => [r, await regionBalance(env, r)])));

async function purchase(env, game, uid, zone, pid) {
  const body = new URLSearchParams({ game_id: uid, zone_id: zone, smile_product_id: String(pid) });
  return apiJSON(smileURL(env, game === "mlbb" ? "/purchase" : "/purchasechess"), {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body,
  });
}

async function recentOrders(env, limit = 30) {
  if (!env.SMILE_COOKIE) return [];
  const u = new URL("https://www.smile.one/customer/activationcode/codelist");
  Object.entries({ type: "orderlist", p: "1", pageSize: String(limit), status: "", startdate: "", enddate: "", order_type: "", key: "", user_id: "" })
    .forEach(([k, v]) => u.searchParams.set(k, v));
  const r = await fetch(u, { headers: { Cookie: env.SMILE_COOKIE, "User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest" } });
  try { return (await r.json()).list || []; } catch { return []; }
}

function parseTarget(arg) {
  const m = arg.trim().match(/^(\d{4,12})\s*[(\s]\s*(\d{2,8})\s*\)?\s*(.*)$/);
  return m ? [m[1], m[2], m[3].trim().toLowerCase()] : null;
}

async function effectivePrice(env, p) {
  if (p.coin_price != null) return Number(p.coin_price);
  if (p.api_price == null) return null;
  const row = await env.DB.prepare("SELECT value FROM settings WHERE key=?").bind(`rate_${p.region}`).first();
  return Number(p.api_price) * Number(row?.value || 1);
}

async function parseItems(env, spec, game, region) {
  const rows = (await env.DB.prepare("SELECT * FROM products WHERE game=? AND region=? AND active=1").bind(game, region).all()).results;
  const aliases = new Map();
  for (const p of rows) {
    const variants = new Set([p.code]);
    if (region === "ph" && p.code.endsWith("ph")) variants.add(p.code.slice(0, -2));
    if (game === "gogo") for (const v of [...variants]) if (v.startsWith("g")) variants.add(v.slice(1));
    for (const v of variants) if (v && !aliases.has(v)) aliases.set(v, p);
  }
  const codes = [...aliases.keys()].sort((a, b) => b.length - a.length);
  const out = [];
  for (const part of spec.replaceAll(" ", "").split(/[+,]/).filter(Boolean)) {
    let found = null;
    for (const code of codes) {
      if (part === code) { found = [code, 1]; break; }
      if (part.startsWith(code)) {
        const rest = part.slice(code.length).replace(/^[x*]/, "");
        if (/^\d+$/.test(rest) && Number(rest) >= 1 && Number(rest) <= 20) { found = [code, Number(rest)]; break; }
      }
    }
    if (!found) return null;
    out.push([aliases.get(found[0]), found[1]]);
  }
  return out.length ? out : null;
}

function friendly(msg) {
  const s = String(msg || "unknown error"), low = s.toLowerCase();
  if (["limit", "maximum", "exceed", "purchase limit", "can only", "reached", "up to"].some(k => low.includes(k))) return "purchase limit reached for this account";
  if (low.includes("timeout")) return "network timeout";
  if (["500", "502", "503"].includes(s.trim())) return "Smile One server error (try again)";
  return s;
}

async function ensureUser(env, user) {
  await env.DB.prepare("INSERT INTO users(tg_id,username,first_name) VALUES(?,?,?) ON CONFLICT(tg_id) DO UPDATE SET username=COALESCE(excluded.username,username),first_name=COALESCE(excluded.first_name,first_name)")
    .bind(user.id, user.username || null, user.first_name || null).run();
}

async function buy(env, msg, args, game, region) {
  const chat = msg.chat.id, user = msg.from, parsed = parseTarget(args);
  if (!parsed) return send(env, chat, "❌ Format မှားနေပါတယ်။\nUsage: <code>.mlb 910819251 12610 wp</code>");
  const [uid, zone, spec] = parsed;
  if (!spec) return send(env, chat, "❌ Package code ထည့်ပေးပါ။");
  const items = await parseItems(env, spec, game, region);
  if (!items) return send(env, chat, `❌ Package code <code>${html(spec)}</code> ကို ${region === "br" ? "🇧🇷 BR" : "🇵🇭 PH"} မှာ မတွေ့ပါ။`);
  const lock = await env.DB.prepare("INSERT OR IGNORE INTO user_locks(tg_id,created_at) VALUES(?,datetime('now'))").bind(user.id).run();
  if (!lock.meta.changes) return send(env, chat, "⏳ Your previous order is still processing.");
  let note;
  try {
    let total = 0;
    for (const [p, qty] of items) {
      p.price = await effectivePrice(env, p);
      if (p.price == null && !personal(env)) return send(env, chat, `❌ <code>${html(p.code)}</code> အတွက် စျေးနှုန်း မသတ်မှတ်ရသေးပါ။`);
      total += (p.price || 0) * qty;
    }
    if (!personal(env)) {
      const u = await env.DB.prepare("SELECT balance FROM users WHERE tg_id=?").bind(user.id).first();
      if (Number(u?.balance || 0) < total) return send(env, chat, `❌ Coin မလုံလောက်ပါ။\nလိုအပ်သည် : ${fmt(total)} ${COIN}\nလက်ကျန် : ${fmt(u?.balance)} ${COIN}`);
    }
    note = await send(env, chat, "⏳ Processing order ...");
    const regions = [...new Set(items.map(([p]) => p.region))];
    const [role, initial] = await Promise.all([checkRole(env, uid, zone), personal(env) ? balances(env, regions) : {}]);
    const player = role.result?.username;
    if (!role.success || role.result?.code !== 200 || !player) return edit(env, chat, note.message_id, `❌ Game ID <code>${uid} (${zone})</code> ကို ရှာမတွေ့ပါ။`);

    const ok = [], failed = [], orderLines = [], failNotes = [];
    for (const [p, qty] of items) {
      let unitOk = 0, last = "";
      for (let i = 0; i < qty; i++) {
        try {
          const res = await purchase(env, game, uid, zone, p.smile_product_id);
          if (res.success) { unitOk++; ok.push({ p, serial: "-" }); }
          else { last = String(res.message || "").slice(0, 120); failed.push([p, last]); }
        } catch (e) { last = e.message || String(e); failed.push([p, last]); }
      }
      const unitFail = qty - unitOk;
      orderLines.push(`${p.title} ×${qty}${unitFail ? ` (${unitOk} ok, ${unitFail} fail)` : ""}`);
      if (unitFail) failNotes.push(`${p.title}: ${friendly(last)}`);
    }

    if (ok.length && env.SMILE_COOKIE) {
      const orders = await recentOrders(env, 30), cursors = {};
      for (const u of ok) {
        const pid = String(u.p.smile_product_id), matches = orders.filter(o => String(o.goods_id) === pid && String(o.user_id) === uid && String(o.server_id) === zone && ["1", "success", "Success"].includes(String(o.status)) && o.increment_id);
        const i = cursors[pid] || 0;
        if (matches[i]) u.serial = String(matches[i].increment_id);
        cursors[pid] = i + 1;
      }
    }
    const finalBal = personal(env) ? await balances(env, regions) : {};
    const statements = [];
    for (const u of ok) statements.push(env.DB.prepare("INSERT INTO transactions(tg_id,kind,game,region,code,title,qty,coins,serial,game_uid,zone_id,player,status) VALUES(?,'purchase',?,?,?,?,1,?,?,?,?,?,'success')").bind(user.id, game, u.p.region, u.p.code, u.p.title, u.p.price || 0, u.serial, uid, zone, player));
    for (const [p, reason] of failed) statements.push(env.DB.prepare("INSERT INTO transactions(tg_id,kind,game,region,code,title,qty,coins,serial,game_uid,zone_id,player,status) VALUES(?,'purchase',?,?,?,?,1,0,?,?,?,?,'fail')").bind(user.id, game, p.region, p.code, p.title, reason, uid, zone, player));
    const spent = ok.reduce((n, u) => n + Number(u.p.price || 0), 0);
    if (!personal(env) && spent) statements.push(env.DB.prepare("UPDATE users SET balance=balance-? WHERE tg_id=?").bind(spent, user.id));
    if (statements.length) await env.DB.batch(statements);

    const gameName = game === "mlbb" ? "MLBB" : "Magic Chess";
    const date = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Bangkok", dateStyle: "short", timeStyle: "medium" }).format(new Date());
    const lines = [`==== ${gameName} Purchase Receipt ====`, "", `UID    : ${uid} (${zone})`, `Name   : ${player}`, `Order  : ${orderLines[0] || "-"}`];
    orderLines.slice(1).forEach(x => lines.push(`         ${x}`));
    const serials = ok.map(x => x.serial).filter(x => x !== "-");
    if (serials.length) { lines.push(`Serial : ${serials[0]}`); serials.slice(1).forEach(x => lines.push(`         ${x}`)); }
    if (personal(env)) {
      const realSpent = regions.reduce((n, r) => n + (initial[r] != null && finalBal[r] != null ? initial[r] - finalBal[r] : 0), 0);
      lines.push(`Spent  : ${fmt(realSpent)} ${COIN}`, LINE, `Date   : ${date}`, `========= ${user.username || user.first_name || user.id} =========`);
      for (const r of regions) {
        if (regions.length > 1) lines.push(r === "br" ? "🇧🇷 BR" : "🇵🇭 PH");
        lines.push(`Initial: ${initial[r] == null ? "N/A" : fmt(initial[r])} ${COIN}`);
        if (initial[r] != null && finalBal[r] != null) lines.push(`Spent  : ${fmt(initial[r] - finalBal[r])} ${COIN}`);
        lines.push(`Assets : ${finalBal[r] == null ? "N/A" : fmt(finalBal[r])} ${COIN}`);
      }
    } else {
      const u = await env.DB.prepare("SELECT balance FROM users WHERE tg_id=?").bind(user.id).first();
      lines.push(`Spent  : ${fmt(spent)} ${COIN}`, LINE, `Date   : ${date}`, `Assets : ${fmt(u?.balance)} ${COIN}`);
    }
    failNotes.forEach(x => lines.push(`⚠️ ${x}`));
    lines.push("", `Success ${ok.length} / Fail ${failed.length}`);
    return edit(env, chat, note.message_id, `<code>${html(lines.join("\n"))}</code>`);
  } catch (e) {
    const text = `❌ Unexpected error: ${html(e.message || String(e))}`;
    return note ? edit(env, chat, note.message_id, text).catch(() => send(env, chat, text)) : send(env, chat, text);
  } finally {
    await env.DB.prepare("DELETE FROM user_locks WHERE tg_id=?").bind(user.id).run();
  }
}

const HELP = `📖 Bot အသုံးပြုနည်း

1️⃣ MLBB Diamond ဝယ်ရန်
   🇧🇷 Brazil      : .mlb GameID ServerID Code
   🇵🇭 Philippines : .mlp GameID ServerID Code
   ဥပမာ : .mlb 910819251 12610 wp
   အများဝယ်ရန် : .mlb 910819251 12610 wp2
   ပေါင်းဝယ်ရန် : .mlb 910819251 12610 wp+86

2️⃣ Magic Chess: 🇧🇷 .mc / 🇵🇭 .mcp
3️⃣ .usecoin — ၇ ရက်စာ Coin သုံးစွဲမှု
4️⃣ .price / .mcprice — စျေးနှုန်းစာရင်း
5️⃣ .check GameID ServerID — Account စစ်ရန်
6️⃣ .bal — လက်ကျန် ကြည့်ရန်
7️⃣ .recharge Code — Smile One ငွေဖြည့်ရန်`;

async function showPrice(env, msg, game) {
  const rows = (await env.DB.prepare("SELECT * FROM products WHERE game=? AND active=1 ORDER BY region,COALESCE(api_price,coin_price)").bind(game).all()).results;
  const lines = [];
  for (const region of ["br", "ph"]) {
    const subset = rows.filter(p => p.region === region);
    if (!subset.length) continue;
    if (lines.length) lines.push("");
    const command = game === "mlbb" ? (region === "br" ? ".mlb" : ".mlp") : (region === "br" ? ".mc" : ".mcp");
    lines.push(`${game === "mlbb" ? "MLBB" : "Magic Chess"} Dias price ${region === "br" ? "🇧🇷 BR" : "🇵🇭 PH"} (${command}):`);
    for (const p of subset) {
      const price = await effectivePrice(env, p); if (price == null) continue;
      let code = p.code;
      if (region === "ph" && code.endsWith("ph")) code = code.slice(0, -2);
      if (game === "gogo" && code.startsWith("g")) code = code.slice(1);
      lines.push(`${code} = ${String(price.toFixed(2)).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1")} ${COIN}`);
    }
  }
  return send(env, msg.chat.id, lines.length ? lines.join("\n") : "❌ Product list မရှိသေးပါ။");
}

async function updateProducts(env, msg) {
  const note = await send(env, msg.chat.id, "⏳ Fetching products ..."), report = [];
  for (const game of ["mlbb", "gogo"]) for (const region of ["br", "ph"]) {
    try {
      const items = await getProducts(env, game, region); let n = 0;
      for (const it of items) {
        const pid = String(it.smile_product_id || it.product_id || it.pid || "");
        let title = String(it.smile_title || it.title || it.name || "").trim();
        if (!pid || !title) continue;
        const raw = it.smile_price ?? it.price, normalized = String(raw ?? "").replace(/[^\d.,]/g, "").replace(/\.(?=.*[,])/, "").replace(",", ".");
        const price = Number(normalized); let existing = await env.DB.prepare("SELECT id,code FROM products WHERE game=? AND region=? AND smile_product_id=?").bind(game, region, pid).first();
        if (existing) await env.DB.prepare("UPDATE products SET title=?,api_price=?,active=1 WHERE id=?").bind(title, Number.isFinite(price) ? price : null, existing.id).run();
        else {
          let base = title.toLowerCase().includes("weekly") || title.toLowerCase().includes("passe semanal") ? "wp" : (title.match(/\d+/)?.[0] || title.toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 8) || "item");
          if (game === "gogo") base = "g" + base; if (region === "ph") base += "ph";
          let code = base, i = 2; while (await env.DB.prepare("SELECT 1 FROM products WHERE code=?").bind(code).first()) code = base + i++;
          await env.DB.prepare("INSERT INTO products(game,region,smile_product_id,title,api_price,code) VALUES(?,?,?,?,?,?)").bind(game, region, pid, title, Number.isFinite(price) ? price : null, code).run();
        }
        n++;
      }
      report.push(`${game} ${region}: ${n} products`);
    } catch (e) { report.push(`${game} ${region}: failed (${e.message})`); }
  }
  return edit(env, msg.chat.id, note.message_id, `<code>${html("📦 Product update\n" + report.join("\n"))}</code>`);
}

async function redeem(env, msg, code) {
  if (!env.SMILE_COOKIE) return send(env, msg.chat.id, "❌ SMILE_COOKIE is not set.");
  const before = await balances(env, ["br", "ph"]), headers = { Cookie: env.SMILE_COOKIE, "User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest", Referer: "https://www.smile.one/customer/activationcode" };
  const body = new URLSearchParams({ sec: code.trim().toUpperCase() });
  const check = await fetch("https://www.smile.one/smilecard/pay/checkcard", { method: "POST", headers, body });
  let cc = 0; try { cc = Number((await check.json()).code); } catch {}
  if (cc !== 200) return send(env, msg.chat.id, `❌ Code rejected (code ${cc})`);
  const pay = await fetch("https://www.smile.one/smilecard/pay/payajax", { method: "POST", headers, body });
  let pc = 0; try { pc = Number((await pay.json()).code); } catch {}
  const after = await balances(env, ["br", "ph"]), lines = ["==== Smile One Recharge ====", "", `Code   : ${code}`];
  for (const r of ["br", "ph"]) if (after[r] != null) lines.push(`${r === "br" ? "🇧🇷 BR" : "🇵🇭 PH"} : ${fmt(before[r])} → ${fmt(after[r])}`);
  lines.push("", pc === 200 ? "✅ Recharge successful!" : `❌ Redeem failed (code ${pc})`);
  return send(env, msg.chat.id, `<code>${html(lines.join("\n"))}</code>`);
}

async function handle(env, msg) {
  if (!msg?.text || !msg.from) return;
  if (personal(env) && !admins(env).has(msg.from.id)) return;
  await ensureUser(env, msg.from);
  const account = await env.DB.prepare("SELECT banned FROM users WHERE tg_id=?").bind(msg.from.id).first();
  if (account?.banned) return send(env, msg.chat.id, "🚫 Your account is banned.");
  const text = msg.text.trim(), chat = msg.chat.id;
  if (text.startsWith("/start") || text.startsWith("/help") || text === ".help") return send(env, chat, HELP);
  if (!text.startsWith(".")) return;
  const parts = text.split(/\s+/), cmd = parts[0].toLowerCase(), args = text.slice(parts[0].length).trim(), admin = admins(env).has(msg.from.id);
  if (cmd === ".mlb") return buy(env, msg, args, "mlbb", "br");
  if ([".mlp", ".mlph"].includes(cmd)) return buy(env, msg, args, "mlbb", "ph");
  if ([".mc", ".gogo", ".chess"].includes(cmd)) return buy(env, msg, args, "gogo", "br");
  if ([".mcp", ".mcph"].includes(cmd)) return buy(env, msg, args, "gogo", "ph");
  if (cmd === ".price" || cmd === ".mlbprice") return showPrice(env, msg, "mlbb");
  if ([".mcprice", ".gogoprice", ".chessprice"].includes(cmd)) return showPrice(env, msg, "gogo");
  if (cmd === ".check") {
    const p = args.replace(/[()]/g, " ").trim().split(/\s+/); if (p.length < 2 || !p.slice(0, 2).every(x => /^\d+$/.test(x))) return send(env, chat, "Usage: <code>.check 910819251 12610</code>");
    const note = await send(env, chat, "🔍 Checking MLBB... Please wait.");
    try {
      const u = new URL(String(env.CHECK_API_BASE).replace(/\/$/, "") + "/check"); u.searchParams.set("game_id", p[0]); u.searchParams.set("server_id", p[1]);
      const d = await apiJSON(u); if (!d.success) return edit(env, chat, note.message_id, "❌ Account ကို ရှာမတွေ့ပါ။");
      const lines = ["==== MLBB Account Check ====", "", `UID    : ${p[0]} (${p[1]})`, `Name   : ${d.username || "N/A"}`, `Region : ${d.region || "N/A"} (${d.region_code || "-"})`];
      return edit(env, chat, note.message_id, `<code>${html(lines.join("\n"))}</code>`);
    } catch (e) { return edit(env, chat, note.message_id, `❌ Check failed: ${html(e.message)}`); }
  }
  if ([".bal", ".balance", ".mycoin"].includes(cmd)) {
    const counts = (await env.DB.prepare("SELECT status,COUNT(*) n FROM transactions WHERE tg_id=? AND kind='purchase' GROUP BY status").bind(msg.from.id).all()).results;
    const n = Object.fromEntries(counts.map(x => [x.status, x.n]));
    if (personal(env)) { const b = await balances(env, ["br", "ph"]); return send(env, chat, `💰 Smile One Balance\n🇧🇷 BR : <b>${b.br == null ? "N/A" : fmt(b.br)}</b> ${COIN}\n🇵🇭 PH : <b>${b.ph == null ? "N/A" : fmt(b.ph)}</b> ${COIN}\n📦 Orders : Success ${n.success || 0} / Fail ${n.fail || 0}`); }
    const u = await env.DB.prepare("SELECT balance FROM users WHERE tg_id=?").bind(msg.from.id).first(); return send(env, chat, `💰 Balance : <b>${fmt(u?.balance)}</b> ${COIN}`);
  }
  if (cmd === ".usecoin") {
    const rows = (await env.DB.prepare("SELECT date(created_at) d,region,SUM(coins) total FROM transactions WHERE tg_id=? AND kind='purchase' AND status='success' AND created_at>=datetime('now','-6 days','start of day') GROUP BY d,region").bind(msg.from.id).all()).results;
    const map = {}; rows.forEach(r => (map[r.d] ||= {})[r.region] = Number(r.total)); const lines = ["==== Coin Usage (Last 7 Days) ====", ""]; let total = 0;
    for (let i = 6; i >= 0; i--) { const d = new Date(Date.now() - i * 86400000), key = d.toISOString().slice(0, 10), br = map[key]?.br || 0, ph = map[key]?.ph || 0; total += br + ph; lines.push(`📅 ${key}\n   🇧🇷 BR Used : ${fmt(br)} ${COIN}\n   🇵🇭 PH Used : ${fmt(ph)} ${COIN}`); }
    lines.push(LINE, `Total Used : ${fmt(total)} ${COIN}`); return send(env, chat, `<code>${html(lines.join("\n"))}</code>`);
  }
  if (!admin && [".addcoin", ".setrate", ".setprice", ".setcode", ".updateproducts", ".smilebal", ".ban", ".unban", ".users", ".history", ".addproduct", ".recharge"].includes(cmd)) return send(env, chat, "🚫 Admin only command.");
  if (cmd === ".updateproducts") return updateProducts(env, msg);
  if (cmd === ".setrate" && parts.length === 3 && ["br", "ph"].includes(parts[1].toLowerCase()) && Number.isFinite(Number(parts[2]))) { await env.DB.prepare("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").bind(`rate_${parts[1].toLowerCase()}`, parts[2]).run(); return send(env, chat, `✅ ${parts[1].toUpperCase()} rate = ${parts[2]}`); }
  if (cmd === ".setprice" && parts.length === 3 && Number.isFinite(Number(parts[2]))) { const r = await env.DB.prepare("UPDATE products SET coin_price=? WHERE code=?").bind(Number(parts[2]), parts[1].toLowerCase()).run(); return send(env, chat, r.meta.changes ? "✅ Price updated." : "❌ Code not found."); }
  if (cmd === ".setcode" && parts.length === 3) { const clash = await env.DB.prepare("SELECT 1 FROM products WHERE code=?").bind(parts[2].toLowerCase()).first(); if (clash) return send(env, chat, "❌ New code is already used."); const r = await env.DB.prepare("UPDATE products SET code=? WHERE code=?").bind(parts[2].toLowerCase(), parts[1].toLowerCase()).run(); return send(env, chat, r.meta.changes ? "✅ Code updated." : "❌ Code not found."); }
  if (cmd === ".addcoin") {
    let id, amount; if (msg.reply_to_message?.from) { id = msg.reply_to_message.from.id; amount = Number(parts[1]); } else { id = Number(parts[1]); amount = Number(parts[2]); }
    if (!Number.isFinite(id) || !Number.isFinite(amount)) return send(env, chat, "Usage: <code>.addcoin tg_id amount</code>");
    await env.DB.batch([env.DB.prepare("INSERT OR IGNORE INTO users(tg_id) VALUES(?)").bind(id), env.DB.prepare("UPDATE users SET balance=balance+? WHERE tg_id=?").bind(amount, id), env.DB.prepare("INSERT INTO transactions(tg_id,kind,coins,status) VALUES(?,?,?,'success')").bind(id, amount >= 0 ? "topup" : "deduct", Math.abs(amount))]);
    const u = await env.DB.prepare("SELECT balance FROM users WHERE tg_id=?").bind(id).first(); return send(env, chat, `✅ <code>${id}</code> : ${fmt(amount)} ${COIN}\nလက်ကျန် : <b>${fmt(u.balance)}</b> ${COIN}`);
  }
  if (cmd === ".ban" || cmd === ".unban") { const id = Number(msg.reply_to_message?.from?.id || parts[1]); if (!Number.isFinite(id)) return send(env, chat, `Usage: <code>${cmd} tg_id</code>`); await env.DB.batch([env.DB.prepare("INSERT OR IGNORE INTO users(tg_id) VALUES(?)").bind(id), env.DB.prepare("UPDATE users SET banned=? WHERE tg_id=?").bind(cmd === ".ban" ? 1 : 0, id)]); return send(env, chat, "✅ Updated."); }
  if (cmd === ".users") { const rows = (await env.DB.prepare("SELECT * FROM users ORDER BY created_at LIMIT 60").all()).results; const lines = rows.map(r => `${r.tg_id}  ${fmt(r.balance)}  ${r.username || r.first_name || ""}${r.banned ? " 🚫" : ""}`); return send(env, chat, `<code>${html(lines.join("\n") || "No users yet.")}</code>`); }
  if (cmd === ".history") { const id = Number(msg.reply_to_message?.from?.id || parts[1] || msg.from.id); const rows = (await env.DB.prepare("SELECT * FROM transactions WHERE tg_id=? ORDER BY id DESC LIMIT 15").bind(id).all()).results; const lines = [`==== History ${id} ====`].concat(rows.map(r => `${r.created_at} ${r.status === "success" ? "✅" : "❌"} ${r.kind} ${r.title || ""} ${fmt(r.coins)} ${COIN}`)); return send(env, chat, `<code>${html(lines.join("\n"))}</code>`); }
  if (cmd === ".smilebal") { const d = await apiJSON(smileURL(env, "/balance")); const b = d.result?.balances || {}; return send(env, chat, `<code>${html(`==== Smile One Balance ====\n🇧🇷 BR : ${b.br}\n🇵🇭 PH : ${b.ph}`)}</code>`); }
  if (cmd === ".addproduct") {
    if (parts.length < 7 || !["mlbb", "gogo"].includes(parts[1]) || !["br", "ph"].includes(parts[2]) || !Number.isFinite(Number(parts[5]))) return send(env, chat, "Usage: <code>.addproduct mlbb br 13 wp 76 Weekly Diamond Pass</code>");
    const clash = await env.DB.prepare("SELECT 1 FROM products WHERE code=?").bind(parts[4].toLowerCase()).first();
    if (clash) return send(env, chat, "❌ That code is already used.");
    await env.DB.prepare("INSERT INTO products(game,region,smile_product_id,code,coin_price,title) VALUES(?,?,?,?,?,?)").bind(parts[1], parts[2], parts[3], parts[4].toLowerCase(), Number(parts[5]), parts.slice(6).join(" ")).run();
    return send(env, chat, "✅ Product added.");
  }
  if (cmd === ".recharge") return parts.length === 2 ? redeem(env, msg, parts[1]) : send(env, chat, "Usage: <code>.recharge ACTIVATION_CODE</code>");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") return Response.json({ ok: true, service: "telegram-topup-bot" });
    if (request.method !== "POST" || url.pathname !== "/telegram") return new Response("Not found", { status: 404 });
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) return new Response("Forbidden", { status: 403 });
    const update = await request.json();
    const inserted = await env.DB.prepare("INSERT OR IGNORE INTO processed_updates(update_id,created_at) VALUES(?,datetime('now'))").bind(update.update_id).run();
    if (!inserted.meta.changes) return Response.json({ ok: true, duplicate: true });
    try { await handle(env, update.message); } catch (e) { console.error("handler", e?.stack || e); }
    return Response.json({ ok: true });
  },
};
