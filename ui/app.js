"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const ALERTS = ["+30%", "+50%", "+100%", "-50%"];
const REFRESH_MS = 15000;
const LIVE_MS = 2000;      // Market, ticker and pings screens while the market is open
const state = { view: "market", scope: "open", detailId: null, detailLive: false, chart: null, lastDetail: null,
  marketOpen: false, prevPx: {}, tTicker: null, tChart: null, tKey: null, tN: 0, tMarks: 0, tPrev: null, loadedAt: {} };

// Plain-English explanations, shown on hover anywhere a term has data-tip="<key>" and on the Help tab
const GLOSSARY = {
  whale: ["Whale", "A very large option trade (usually $350K or more) that our system flagged as unusual and worth watching."],
  fill: ["Whale's price", "The price the whale paid per contract. P&L is measured from here, as if you had bought at the same price and time."],
  bid: ["Bid", "The highest price a buyer is offering right now - the price you could sell at immediately. All P&L in FlowDesk uses the bid."],
  ask: ["Ask", "The lowest price a seller wants - what you would pay to buy right now."],
  spread: ["Spread", "Ask minus bid. A wide spread means you lose money just by buying and selling, so tighter is better."],
  pnl: ["P&L", "Profit and loss: bid now divided by the whale's price, minus 1. +50% means the option is worth 1.5x what the whale paid."],
  best: ["Best so far", "The highest bid since the whale bought: the best moment you could have sold."],
  call: ["Call", "An option that gains value when the stock goes UP."],
  put: ["Put", "An option that gains value when the stock goes DOWN."],
  strike: ["Strike", "The stock price the option is tied to. A $365 call pays off if the stock ends above $365."],
  expiry: ["Expiry", "The last day the option exists. After that it is worth only its in-the-money value, often zero."],
  dte: ["DTE / 0DTE", "Days to expiry. 0DTE options expire today and can move hundreds of percent in minutes - both ways."],
  premium: ["Premium", "Total dollars the whale spent: price x contracts x 100."],
  iv: ["IV (implied volatility)", "How big a move the market is pricing in. High IV means expensive options."],
  delta: ["Delta", "How much the option moves for each $1 the stock moves. 0.50 = about 50 cents per $1."],
  theta: ["Theta (time decay)", "How much value the option loses per day just from time passing."],
  model: ["Model target", "Our machine-learning model's prediction of the best % this contract's bid would reach after the whale's trade."],
  judges: ["AI judges", "Two AI models running on this PC (Hermes and Qwen) that read each trade and guess its upside. They are still learning and are less accurate than the model."],
  alerts: ["Alerts", "Discord alerts that fired the first time the bid crossed +30%, +50%, +100% or -50% from the whale's price."],
  latency: ["Ping delay", "How long after the whale's trade our ping went out. By then the price may already have moved."],
  tradeResult: ["As a trade", "What following this whale really earned: buy at the ask right after its trade, sell when the bid hits +30% or −50%, otherwise at 4 PM, after $0.65/contract fees each way. This is what the model is now trained and judged on."],
  blindtest: ["Blind test", "To trade a day, the Lab searches for the best strategy using ONLY the days before it, then trades that strategy on the day it never saw. Repeated for every day. It measures whether the search itself finds strategies that keep working - not whether one strategy looked good after the fact."],
  lucktest: ["Luck test", "The whole blind test is rerun many times on data where each day's results were shuffled between trades, so no real pattern is left. If the shuffled runs often do as well as the real one, the real result is just luck. p = the share of shuffled runs that did at least as well (below 0.05 = convincing)."],
  walkforward: ["Walk-forward test", "The honest way to backtest: to judge Wednesday, the model is trained only on Monday and Tuesday, then predicts Wednesday's picks blind. No peeking at the future."],
  rank: ["Rank agreement", "Does the model put the picks in the right order, from worst to best? 0 means random guessing, 1 means a perfect order. Anything steadily above 0.2 is useful for trading."],
  side: ["Bought or sold?", "Whether the whale was BUYING (paid the ask) or SELLING (hit the bid). Following only makes sense when the whale bought. From Trade Echo's trade sentiment, cross-checked with IBKR's own bid/ask at that minute."],
  spreadleg: ["Spread leg", "The whale traded another contract in the same second with a matching size: one multi-leg order (a vertical spread, calendar, collar...). Its real bet is the combination, not this contract alone. If this was the leg the whale SOLD, no ping is sent."],
  score: ["Flow score", "Trade Echo's 0-100 rating of how unusual and aggressive the trade was. We only consider trades above 30."],
  vwap: ["VWAP", "Volume-weighted average price: the average price everyone paid today, weighted by how many shares traded. Price above VWAP = buyers have been winning today; below = sellers. Big traders use it as a fair-value line."],
  volume: ["Volume vs. normal", "Shares traded so far today compared with a normal day at the same time of day (the last 20 days). 2.0× = twice the usual activity - something is going on. Volume is naturally high at the open and close, which this already accounts for."],
  voltrend: ["Volume picking up / slowing", "The last 5 minutes' trading pace vs. the 15 minutes before, adjusted for the time of day. Picking up = more traders are jumping in right now."],
  momentum: ["Momentum", "How far the price moved over the last 1, 5, 15 and 30 minutes. Several green moves in a row = strong upward momentum."],
  rsi: ["RSI", "Relative Strength Index (14 one-minute bars), 0-100. Above 70 = the price ran up fast and may be stretched; below 30 = it dropped fast. It is not a buy/sell signal by itself."],
  trend: ["Trend", "Uptrend = the 9-minute average is above the 21-minute average AND the price is above VWAP. Downtrend = both below. Mixed = they disagree."],
  pressure: ["Buyers vs. sellers", "Share of the last 15 minutes' volume that was buying. With live trade data: trades at the ask (buyers paying up) vs. at the bid (sellers hitting). Without it (your account today): an estimate from where each 1-minute bar closed within its range - closing near the high means buyers were in control."],
  orderbook: ["Order book (Level 2)", "Every buy and sell order waiting at each price, not just the best bid and ask. A heavy side shows where big orders are parked. It needs IBKR's Level 2 (depth) data subscriptions."],
  oi: ["Open interest", "How many contracts of this option are open (held by someone). The exchanges publish it once a day, overnight, so it doesn't change during the day. Up vs. the day before = new positions were opened."],
  voloi: ["Volume ÷ open interest", "Contracts traded today divided by the contracts that were open at the start of the day. Above 1 = more traded today than existed - a strong sign of NEW positions being opened, often by the whale."],
  flowlean: ["Whale lean", "Bullish = calls bought or puts sold (they profit if the stock rises). Bearish = puts bought or calls sold. From Trade Echo's trade sentiment, over every trade we logged on the ticker ($25K+, $50K+ on SPY and QQQ, plus the $350K+ whale watch)."],
};

// ---------- formatting ----------
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = v => v == null ? "—" : "$" + Number(v).toFixed(2);
const bigMoney = v => v == null ? "—" : !v ? "$0" : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}K`;
const pct = v => v == null ? "—" : (v >= 0 ? "+" : "−") + Math.abs(v * 100).toFixed(0) + "%";
const tone = v => v == null ? "" : v >= 0 ? "up" : "down";
const strike = v => "$" + Number(v).toString();
const pcWord = pc => pc === "CALL" ? "Call" : "Put";
function clock(m) {
  const [h, mi] = m.slice(11, 16).split(":").map(Number);
  return `${h % 12 || 12}:${String(mi).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}
function day(d) {
  return new Date(d.slice(0, 10) + "T12:00:00Z").toLocaleDateString("en-US",
    { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
}
const when = (m, ref) => m.slice(0, 10) === ref ? clock(m) : `${day(m)}, ${clock(m)}`;
const minutesBetween = (a, b) => Math.round((Date.parse(b.replace(" ", "T") + ":00Z") - Date.parse(a.replace(" ", "T") + ":00Z")) / 60000);
function expiryText(c) {
  if (c.state === "expired") return `Expired ${day(c.expiration)}`;
  if (c.dte <= 0) return "Expires today (0DTE)";
  if (c.dte === 1) return "Expires tomorrow";
  return `Expires ${day(c.expiration)} · ${c.dte} days left`;
}
const name = c => `${c.ticker} ${strike(c.strike)} ${pcWord(c.put_call)}`;

// ---------- data ----------
async function api(endpoint, params = {}) {
  const r = await fetch(`/api/${endpoint}?` + new URLSearchParams(params), { cache: "no-store" });
  const j = await r.json();
  if (!r.ok || (j && j.error)) throw new Error(j && j.error ? j.error : `HTTP ${r.status}`);
  return j;
}

// ---------- header ----------
async function loadHealth() {
  try {
    const s = await api("status");
    state.marketOpen = s.market_open;
    const ib = s.ibkr.ok ? "ok" : s.ibkr.idle ? "idle" : "bad";
    $("#health").innerHTML = `
      <span data-tip="US options trade 9:30 AM - 4:00 PM Eastern, Monday to Friday.">
        <span class="dot ${s.market_open ? "ok" : "idle"}"></span>${s.market_open ? "Market open" : "Market closed"}</span>
      <span data-tip="${s.flow.ok ? "The flow logger is pulling whale trades from Trade Echo." : "The flow logger hasn't checked for new trades recently - is the PC awake?"}">
        <span class="dot ${s.flow.ok ? "ok" : "bad"}"></span>Flow alerts</span>
      <span data-tip="${s.ibkr.ok ? "Live option prices are streaming from Interactive Brokers." : s.ibkr.idle ? "No live prices while the market is closed." : "No live prices for a while - check that TWS is open and logged in."}">
        <span class="dot ${ib}"></span>IBKR prices</span>
      ${s.model ? `<span data-tip="The model that decides what to ping. Retrained every night; this version was trained ${esc(s.model.trained)} ET.">Model: ${esc(kindName(s.model.kind))}</span>` : ""}
      <span>${esc(s.now_et)}</span>`;
  } catch (e) {
    $("#health").innerHTML = `<span class="down">Can't read data: ${esc(e.message)}</span>`;
  }
}

// ---------- live list ----------
function spark(values, fill, up) {
  if (!values.length) return '<div class="spark muted" style="display:flex;align-items:center">No prices recorded yet</div>';
  const w = 300, h = 54, all = [...values, fill];
  const lo = Math.min(...all), hi = Math.max(...all), span = hi - lo || 1;
  const x = i => values.length === 1 ? w : i * (w / (values.length - 1));
  const y = v => (h - 3 - (v - lo) / span * (h - 6)).toFixed(1);
  const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <line x1="0" x2="${w}" y1="${y(fill)}" y2="${y(fill)}" style="stroke:#8b949e;stroke-width:1;stroke-dasharray:3 3" vector-effect="non-scaling-stroke"/>
    <polyline points="${pts}" style="fill:none;stroke:${up ? "var(--green)" : "var(--red)"};stroke-width:2" vector-effect="non-scaling-stroke"/></svg>`;
}

function contextChips(ctx) {
  if (!ctx) return '<div class="chips"><span class="chip" data-tip="side">Bought/sold: not checked yet</span></div>';
  const side = { buy: ["hit", "Whale BOUGHT (at ask)"], sell: ["hit neg", "Whale SOLD (at bid)"], mid: ["", "Traded mid (unclear)"] }[ctx.side];
  let html = `<span class="chip ${side[0]}" data-tip="side">${side[1]}</span>`;
  if (ctx.structure) html += `<span class="chip ${ctx.our_leg === "sold" ? "hit neg" : ""}" data-tip="spreadleg">🧩 ${esc(ctx.structure)} · leg ${esc(ctx.our_leg)}</span>`;
  return `<div class="chips">${html}</div>`;
}

function cardHTML(c) {
  const stateTag = c.state === "live" ? '<span class="state live"><span class="dot"></span>LIVE</span>'
    : c.state === "expired" ? '<span class="state">Expired</span>' : '<span class="state">Market closed</span>';
  const label = c.state === "live" ? "Bid now" : "Last bid";
  const chips = ALERTS.map(a => `<span class="chip ${c.alerts.includes(a) ? "hit" + (a.startsWith("-") ? " neg" : "") : ""}">${a}</span>`).join("");
  return `<article class="card" data-id="${c.id}" tabindex="0">
    <div class="card-top">
      <span class="ticker">${esc(c.ticker)}</span>
      <span class="pc ${c.put_call === "CALL" ? "call" : "put"}" data-tip="${c.put_call === "CALL" ? "call" : "put"}">${strike(c.strike)} ${pcWord(c.put_call).toUpperCase()}</span>
      ${stateTag}
    </div>
    <div class="expiry">${expiryText(c)}</div>
    <div class="whale">Whale traded <b>${Number(c.size).toLocaleString()}</b> at <b>${money(c.fill)}</b> · ${day(c.trade_date)} ${clock(c.trade_date + " " + c.time)} · ${bigMoney(c.premium)}</div>
    ${contextChips(c.context)}
    <div class="pnl-row">
      <span class="pnl ${tone(c.pnl)}" data-tip="pnl">${pct(c.pnl)}</span>
      <span class="bidnow">${label}<br><b>${money(c.bid)}</b></span>
    </div>
    ${spark(c.spark, c.fill, (c.pnl ?? 0) >= 0)}
    <div class="card-foot">
      <span data-tip="best">Best <b class="${tone(c.best_pnl)}">${pct(c.best_pnl)}</b>${c.best_at ? " at " + when(c.best_at, c.trade_date) : ""}</span>
      <span data-tip="${c.play ? esc(c.play.about) : "model"}">Model expected <b>${pct(c.model)}</b>${c.play ? ` · 🎯 ${esc(c.play.name)}` : ""}</span>
    </div>
    <div class="chips" data-tip="alerts">${chips}</div>
  </article>`;
}

function statsHTML(cards) {
  const n = cards.length;
  const winning = cards.filter(c => (c.pnl ?? -1) > 0).length;
  const hit30 = cards.filter(c => (c.best_pnl ?? -1) >= 0.3).length;
  const top = cards.filter(c => c.best_pnl != null).sort((a, b) => b.best_pnl - a.best_pnl)[0];
  const scopeWord = { open: "still-open contracts", session: "pings in the last session", all: "pings so far" }[state.scope];
  return `
    <div class="stat"><div class="label">Tracking</div><div class="value">${n}</div><div class="sub">${scopeWord}</div></div>
    <div class="stat"><div class="label">In profit right now</div><div class="value ${winning ? "up" : ""}">${winning} of ${n}</div><div class="sub">bid above the whale's price</div></div>
    <div class="stat"><div class="label">Reached +30% at some point</div><div class="value">${hit30} of ${n}</div><div class="sub">a chance to take profit</div></div>
    <div class="stat"><div class="label">Biggest move</div><div class="value up">${top ? pct(top.best_pnl) : "—"}</div><div class="sub">${top ? esc(name(top)) : ""}</div></div>`;
}

async function loadList() {
  try {
    const { cards } = await api("pings", { scope: state.scope });
    $("#stats").innerHTML = statsHTML(cards);
    $("#cards").innerHTML = cards.length ? cards.map(cardHTML).join("")
      : `<div class="empty">${state.scope === "open" ? "No open contracts right now. New pings appear here as soon as they're sent." : "No pings yet."}</div>`;
    $("#updated").textContent = "Updated " + new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
  } catch (e) {
    $("#cards").innerHTML = `<div class="error">Couldn't load pings: ${esc(e.message)}</div>`;
  }
}

// ---------- market board ----------
const px = v => v == null ? "—" : "$" + Number(v).toFixed(2);
const pct2 = v => v == null ? "—" : (v >= 0 ? "+" : "−") + Math.abs(v * 100).toFixed(2) + "%";
const vol = v => v == null ? "—" : v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M"
  : v >= 1e3 ? Math.round(v / 1e3) + "K" : String(Math.round(v));
const arrow = v => v == null ? "" : v > 0 ? "▲" : v < 0 ? "▼" : "";
const flashCls = (prev, now) => prev == null || now == null || prev === now ? "" : now > prev ? "flash-up" : "flash-down";
const TREND = { up: ["up", "Uptrend"], down: ["down", "Downtrend"], mixed: ["", "Mixed"] };

function rsiText(r) {
  if (r == null) return "—";
  return Math.round(r) + (r >= 70 ? " · stretched up" : r <= 30 ? " · stretched down" : "");
}
function volTrendText(v) {
  if (v == null) return "";
  return v >= 1.25 ? '<span class="up">↑ picking up</span>' : v <= 0.8 ? '<span class="down">↓ slowing</span>' : "→ steady";
}
function stateTag(s) {
  return {
    live: '<span class="state live"><span class="dot"></span>LIVE</span>',
    delayed: '<span class="state" data-tip="IBKR\'s live bar updates aren\'t arriving, so this ticker refreshes every 5 minutes.">Every 5 min</span>',
    stale: '<span class="state down" data-tip="No new prices from IBKR for a while - is TWS running?">No new prices</span>',
    closed: '<span class="state">Closed</span>',
  }[s.state] || "";
}
function pressureMeter(p, short) {
  const v = p && p.buy_share;
  if (v == null) return `<div class="meter empty"></div><div class="meter-label"><span>Buyers vs sellers: not enough data yet</span></div>`;
  const est = p.method === "estimate" ? " (estimate)" : "";
  const bar = `<div class="meter" data-tip="pressure"><span style="width:${(v * 100).toFixed(0)}%"></span></div>`;
  const buy = `<span class="${v >= 0.5 ? "up" : ""}">${Math.round(v * 100)}% buying</span>`;
  const sell = `<span class="${v < 0.5 ? "down" : ""}">${Math.round((1 - v) * 100)}% selling</span>`;
  if (short) return `${bar}<div class="meter-label" data-tip="pressure">${buy}<span>last 15 min${est}</span>${sell}</div>`;
  return `${bar}<div class="meter-label" data-tip="pressure">${buy}${sell}</div>
    <div class="muted small" style="margin-top:4px">Last 15 minutes${est}${p.day_share != null ? ` · whole day: ${Math.round(p.day_share * 100)}% buying` : ""}</div>`;
}
function flowMeter(f) {
  if (!f || !(f.bull + f.bear)) return `<div class="meter empty"></div><div class="meter-label" data-tip="flowlean"><span>Whales: no bullish/bearish trades logged ${f && f.n ? "(only unclear ones)" : "yet"}</span></div>`;
  const share = f.bull / (f.bull + f.bear);
  return `<div class="meter flow" data-tip="flowlean"><span style="width:${(share * 100).toFixed(0)}%"></span></div>
    <div class="meter-label" data-tip="flowlean"><span class="up">Whales ${bigMoney(f.bull)} bullish</span>
      <span>${f.n} trades</span><span class="down">${bigMoney(f.bear)} bearish</span></div>`;
}

function tileHTML(s) {
  if (s.state === "no_data") return `<article class="tile" data-t="${esc(s.ticker)}"><div class="t-top"><span class="ticker">${esc(s.ticker)}</span></div>
    <div class="muted small">No IBKR prices saved for this ticker yet - they start at the next market open.</div></article>`;
  const prev = state.prevPx[s.ticker];
  const m = s.mom || {};
  const chip = (label, v) => `<span class="mchip ${tone(v)}">${label} ${arrow(v)}${v == null ? "—" : Math.abs(v * 100).toFixed(2) + "%"}</span>`;
  const tr = TREND[s.trend];
  return `<article class="tile" data-t="${esc(s.ticker)}" tabindex="0">
    <div class="t-top"><span class="ticker">${esc(s.ticker)}</span>
      ${tr ? `<span class="chip ${tr[0] === "up" ? "hit" : tr[0] === "down" ? "hit neg" : ""}" data-tip="trend">${tr[1]}</span>` : ""}
      ${stateTag(s)}</div>
    <div class="t-price"><span class="px ${flashCls(prev, s.price)}">${px(s.price)}</span>
      <span class="chg ${tone(s.change)}">${pct2(s.change)}${s.change_abs != null ? ` (${s.change_abs >= 0 ? "+" : "−"}$${Math.abs(s.change_abs).toFixed(2)})` : ""}</span></div>
    ${spark(s.spark || [], s.vwap ?? (s.spark || [0])[0], (s.vs_vwap ?? 0) >= 0)}
    <div class="t-row"><span data-tip="vwap">vs. VWAP ${px(s.vwap)}</span><b class="${tone(s.vs_vwap)}">${pct2(s.vs_vwap)}</b></div>
    <div class="mom" data-tip="momentum">${chip("1m", m["1m"])}${chip("5m", m["5m"])}${chip("15m", m["15m"])}</div>
    <div class="t-row"><span data-tip="volume">Volume ${vol(s.volume)}${s.rvol != null ? ` · <b>${s.rvol.toFixed(1)}×</b> normal` : ""}</span>
      <span data-tip="voltrend">${volTrendText(s.vol_trend)}</span></div>
    <div class="t-row"><span data-tip="rsi">RSI ${rsiText(s.rsi)}</span><span>H ${px(s.high)} · L ${px(s.low)}</span></div>
    ${pressureMeter(s.pressure, true)}
    ${flowMeter(s.flow)}
  </article>`;
}

function feedNoteHTML(M) {
  const f = M.feed || {}, parts = [];
  if (!M.market_open) parts.push(`<b>Market closed</b> - showing ${day(M.session)}'s session. Prices go live at 9:30 AM ET.`);
  if (f.quotes && f.quotes.status === "not_subscribed")
    parts.push("Prices come from IBKR's 1-minute bars as they form (every few seconds). Your IBKR account has no real-time " +
      "<b>stock</b> quotes for the API, so there's no live bid/ask for the stock and buying-vs-selling is an estimate - see Help.");
  else if (M.market_open && !f.bars)
    parts.push("The IBKR tracker hasn't started the live board yet (it starts a few minutes after the open; TWS must be running).");
  else if (M.market_open && f.bars && f.bars.status === "stale")
    parts.push("IBKR's live bar updates stopped - prices refresh every 5 minutes until they come back.");
  return parts.join(" ");
}

async function loadMarket() {
  try {
    const M = await api("market");
    state.marketOpen = M.market_open;
    $("#feedNote").innerHTML = feedNoteHTML(M);
    $("#tiles").innerHTML = M.tiles.map(tileHTML).join("");
    M.tiles.forEach(s => { state.prevPx[s.ticker] = s.price; });
    $("#mktUpdated").textContent = (M.market_open ? "Live · " : "") + "Updated " + M.now_et + " ET";
  } catch (e) {
    $("#tiles").innerHTML = `<div class="error">Couldn't load the market: ${esc(e.message)}</div>`;
  }
}

// ---------- one ticker ----------
function openTicker(t) {
  state.tTicker = t;
  if (state.tChart) { state.tChart.remove(); state.tChart = null; }
  state.tKey = null; state.tPrev = null;
  showView("ticker");
  $("#tHead").innerHTML = '<p class="muted">Loading…</p>';
  ["#tBook", "#tPanels", "#tContracts", "#tWhales"].forEach(s => { $(s).innerHTML = ""; });
  $("#tName").textContent = t;
  loadTicker();
}

async function loadTicker() {
  try {
    const d = await api("ticker", { t: state.tTicker });
    state.marketOpen = d.market_open;
    renderTicker(d);
  } catch (e) {
    $("#tHead").innerHTML = `<div class="error">Couldn't load ${esc(state.tTicker)}: ${esc(e.message)}</div>`;
  }
}

function tickerHeadHTML(d) {
  const p = state.tPrev;
  const stat = (label, value, sub, tip, cls = "") => `<div class="stat"><div class="label"${tip ? ` data-tip="${tip}"` : ""}>${label}</div>
    <div class="value ${cls}">${value}</div><div class="sub">${sub}</div></div>`;
  const tr = TREND[d.trend];
  return `<div class="d-head"><h1>${esc(d.ticker)}</h1>${stateTag(d)}
      ${tr ? `<span class="chip ${tr[0] === "up" ? "hit" : tr[0] === "down" ? "hit neg" : ""}" data-tip="trend">${tr[1]}</span>` : ""}
      <span class="muted">${d.market_open ? "" : day(d.session) + " session"}</span></div>
    <div class="big-row">
      ${stat("Price", `<span class="px ${flashCls(p, d.price)}">${px(d.price)}</span>`, d.bid != null ? `bid ${px(d.bid)} · ask ${px(d.ask)}` : `open ${px(d.open)}`)}
      ${stat("Today", pct2(d.change), d.prev_close != null ? `vs. yesterday's close ${px(d.prev_close)}` : "no previous close yet", "", tone(d.change))}
      ${stat("vs. VWAP", pct2(d.vs_vwap), `VWAP ${px(d.vwap)}`, "vwap", tone(d.vs_vwap))}
      ${stat("Volume", d.rvol != null ? d.rvol.toFixed(1) + "× normal" : vol(d.volume), `${vol(d.volume)} shares${d.vol_trend != null ? " · " + volTrendText(d.vol_trend) : ""}`, "volume")}
      ${stat("Day range", `${px(d.low)} – ${px(d.high)}`, `opened at ${px(d.open)}`)}
    </div>`;
}

function drawTicker(d) {
  const el = $("#tChart");
  const key = d.ticker + d.session;
  if (!d.bars.length || !window.LightweightCharts) {
    if (state.tChart) { state.tChart.remove(); state.tChart = null; }
    el.innerHTML = `<div class="no-chart">${window.LightweightCharts ? "No IBKR prices saved for this ticker yet." : "Chart library didn't load (no internet?)."}</div>`;
    state.tKey = null;
    return;
  }
  const toBar = b => ({ time: toTime(b[0]), open: b[1], high: b[2], low: b[3], close: b[4] });
  const toVol = b => ({ time: toTime(b[0]), value: b[5] || 0, color: b[4] >= b[1] ? "rgba(63,185,80,.35)" : "rgba(248,81,73,.35)" });
  const toVw = x => ({ time: toTime(x[0]), value: x[1] });
  const times = d.bars.map(b => toTime(b[0]));
  const snap = hhmm => { const t = toTime(d.session + " " + hhmm.slice(0, 5)); return times.find(x => x >= t) ?? times[times.length - 1]; };
  const markers = () => d.markers.filter(p => p.lean !== "unclear").map(p => ({
    time: snap(p.time), position: p.lean === "bull" ? "belowBar" : "aboveBar", color: p.lean === "bull" ? "#3fb950" : "#f85149",
    shape: p.lean === "bull" ? "arrowUp" : "arrowDown", text: `${bigMoney(p.premium)} ${p.put_call === "CALL" ? "C" : "P"}${Number(p.strike)}`,
  })).sort((a, b) => a.time - b.time);

  if (state.tChart && state.tKey === key && d.bars.length >= state.tN) {
    // live: only the forming bar and any new ones change, so the zoom and scroll stay put
    for (let i = Math.max(0, state.tN - 1); i < d.bars.length; i++) {
      state.tSeries.c.update(toBar(d.bars[i]));
      state.tSeries.v.update(toVol(d.bars[i]));
      if (d.vwap_line[i][1] != null) state.tSeries.w.update(toVw(d.vwap_line[i]));
    }
    state.tN = d.bars.length;
    if (d.markers.length !== state.tMarks) { state.tSeries.c.setMarkers(markers()); state.tMarks = d.markers.length; }
    return;
  }
  if (state.tChart) state.tChart.remove();
  el.innerHTML = "";
  const chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { type: "solid", color: "transparent" }, textColor: "#c9d1d9", fontFamily: "Segoe UI, system-ui, sans-serif" },
    grid: { vertLines: { color: "#1f2630" }, horzLines: { color: "#1f2630" } },
    rightPriceScale: { borderColor: "#2d3540" },
    timeScale: { borderColor: "#2d3540", timeVisible: true, secondsVisible: false,
      tickMarkFormatter: t => { const dt = new Date(t * 1000); return `${dt.getUTCHours() % 12 || 12}:${String(dt.getUTCMinutes()).padStart(2, "0")}`; } },
    localization: { timeFormatter: t => clock(new Date(t * 1000).toISOString().replace("T", " ")) },
    crosshair: { mode: 0 },
  });
  const c = chart.addCandlestickSeries({ upColor: "#3fb950", downColor: "#f85149", borderVisible: false,
    wickUpColor: "#3fb950", wickDownColor: "#f85149", priceLineVisible: true });
  c.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.24 } });
  const v = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false });
  v.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  const w = chart.addLineSeries({ color: "#d29922", lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  c.setData(d.bars.map(toBar));
  v.setData(d.bars.map(toVol));
  w.setData(d.vwap_line.filter(x => x[1] != null).map(toVw));
  if (d.prev_close != null) c.createPriceLine({ price: d.prev_close, color: "#8b949e", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "Prev close" });
  c.setMarkers(markers());
  const legend = $("#tLegend");
  const base = () => `${esc(d.ticker)} ${px(state.tLast)} · <span class="${tone(state.tChange)}">${pct2(state.tChange)}</span>`;
  chart.subscribeCrosshairMove(p => {
    const b = p && p.seriesData ? p.seriesData.get(c) : null;
    const vv = p && p.seriesData ? p.seriesData.get(v) : null;
    if (!b) { legend.innerHTML = base(); return; }
    legend.innerHTML = `${clock(new Date(b.time * 1000).toISOString().replace("T", " "))} · O ${px(b.open)} H ${px(b.high)} L ${px(b.low)} C ${px(b.close)}` +
      (vv ? ` · Vol ${vol(vv.value)}` : "");
  });
  state.tChart = chart; state.tSeries = { c, v, w }; state.tKey = key; state.tN = d.bars.length; state.tMarks = d.markers.length;
  chart.timeScale().fitContent();   // the whole session; scroll to zoom in
  legend.innerHTML = base();
}

function bookHTML(d) {
  const b = d.book || {};
  if (b.status === "ok" && ((b.bids || []).length || (b.asks || []).length)) {
    const bids = b.bids.slice(0, 10), asks = b.asks.slice(0, 10);
    const max = Math.max(1, ...bids.map(x => x[1] || 0), ...asks.map(x => x[1] || 0));
    const bs = bids.reduce((s, x) => s + (x[1] || 0), 0), as = asks.reduce((s, x) => s + (x[1] || 0), 0);
    const rows = Array.from({ length: Math.max(bids.length, asks.length) }, (_, i) => {
      const bi = bids[i], ai = asks[i];
      return `<div class="lv b" style="--w:${bi ? (bi[1] / max * 100).toFixed(0) : 0}%">${bi ? `<span>${vol(bi[1])}</span><b>${px(bi[0])}</b>` : ""}</div>
        <div class="lv a" style="--w:${ai ? (ai[1] / max * 100).toFixed(0) : 0}%">${ai ? `<b>${px(ai[0])}</b><span>${vol(ai[1])}</span>` : ""}</div>`;
    }).join("");
    return `<h3><span class="term" data-tip="orderbook">Order book</span> <span class="muted small">live</span></h3>
      <div class="meter"><span style="width:${(bs / (bs + as || 1) * 100).toFixed(0)}%"></span></div>
      <div class="meter-label"><span class="up">${vol(bs)} waiting to buy</span><span class="down">${vol(as)} waiting to sell</span></div>
      <div class="book" style="margin-top:10px"><div class="hd">Buyers (size · price)</div><div class="hd">Sellers (price · size)</div>${rows}</div>
      <h3 style="margin-top:16px">Buyers vs. sellers (trades)</h3>${pressureMeter(d.pressure)}`;
  }
  const why = b.status === "not_subscribed"
    ? "Your IBKR account doesn't have Level 2 (depth) data, so the orders waiting at each price can't be shown yet. Adding NASDAQ TotalView / NYSE OpenBook in IBKR's market-data subscriptions turns this on automatically."
    : b.status === "waiting" ? "Asking IBKR for the order book…"
    : d.market_open ? "The order book appears here a few seconds after you open a ticker (needs IBKR Level 2 data)."
    : "The order book shows the buy and sell orders waiting at each price while the market is open (needs IBKR Level 2 data).";
  return `<h3><span class="term" data-tip="orderbook">Order book</span></h3><p class="muted small">${why}</p>
    <h3 style="margin-top:16px"><span class="term" data-tip="pressure">Buyers vs. sellers</span></h3>${pressureMeter(d.pressure)}
    ${d.bid != null ? `<div class="kv" style="margin-top:12px"><span class="k" data-tip="bid">Bid × size</span><span class="v">${px(d.bid)} × ${vol(d.bid_size)}</span>
      <span class="k" data-tip="ask">Ask × size</span><span class="v">${px(d.ask)} × ${vol(d.ask_size)}</span></div>` : ""}`;
}

function tickerPanelsHTML(d) {
  const m = d.mom || {}, f = d.flow || {};
  const row = (k, v, cls = "", tip = "") => `<span class="k"${tip ? ` data-tip="${tip}"` : ""}>${k}</span><span class="v ${cls}">${v}</span>`;
  return `
    <div class="panel"><h3><span class="term" data-tip="momentum">Momentum</span></h3><div class="kv">
      ${row("Last 1 minute", pct2(m["1m"]), tone(m["1m"]))}${row("Last 5 minutes", pct2(m["5m"]), tone(m["5m"]))}
      ${row("Last 15 minutes", pct2(m["15m"]), tone(m["15m"]))}${row("Last 30 minutes", pct2(m["30m"]), tone(m["30m"]))}
      ${row("RSI (14 min)", rsiText(d.rsi), d.rsi >= 70 ? "up" : d.rsi <= 30 ? "down" : "", "rsi")}
      ${row("Trend", TREND[d.trend] ? TREND[d.trend][1] : "—", TREND[d.trend] ? TREND[d.trend][0] : "", "trend")}
    </div></div>
    <div class="panel"><h3><span class="term" data-tip="volume">Volume</span></h3><div class="kv">
      ${row("Shares traded today", vol(d.volume))}
      ${row("A normal full day", vol(d.normal_volume))}
      ${row("Pace vs. normal (this time of day)", d.rvol != null ? d.rvol.toFixed(2) + "×" : "—", d.rvol >= 1.5 ? "up" : "")}
      ${row("Last 5 min vs. the 15 before", volTrendText(d.vol_trend) || "—", "", "voltrend")}
    </div></div>
    <div class="panel"><h3><span class="term" data-tip="flowlean">Whale flow today</span></h3>${flowMeter(f)}<div class="kv" style="margin-top:10px">
      ${row("Bullish (calls bought, puts sold)", bigMoney(f.bull), "up")}${row("Bearish (puts bought, calls sold)", bigMoney(f.bear), "down")}
      ${row("Unclear (traded mid)", bigMoney(f.unclear))}${row("Trades logged · $350K+", `${f.n ?? 0} · ${f.big ?? 0}`)}
      ${row("Last 30 min: bullish / bearish", `<span class="up">${bigMoney(f.recent_bull)}</span> / <span class="down">${bigMoney(f.recent_bear)}</span>`)}
    </div></div>`;
}

function contractsHTML(d) {
  if (!d.contracts.length) return '<tr><td class="muted">No open contracts tracked on this ticker.</td></tr>';
  const side = c => c.side === "buy" ? '<span class="up">bought</span>' : c.side === "sell" ? '<span class="down">sold</span>' : c.side === "mid" ? "mid" : "";
  return `<thead><tr><th>Contract</th><th>Whale</th><th class="num">Bid</th><th class="num">Ask</th><th class="num">Volume today</th>
      <th class="num"><span class="term" data-tip="oi">Open interest</span></th><th class="num">vs. day before</th>
      <th class="num"><span class="term" data-tip="voloi">Vol ÷ OI</span></th><th class="num" data-tip="iv">IV</th></tr></thead><tbody>` +
    d.contracts.map(c => `<tr class="click" data-id="${c.pick_id}">
      <td>${c.live ? '<span class="dot ok"></span>' : ""}<b>${strike(c.strike)} ${pcWord(c.put_call)}</b> <span class="muted">${day(c.expiration)}</span></td>
      <td>${c.pinged ? "🔔 " : ""}${side(c)} ${bigMoney(c.premium)}${c.n > 1 ? ` <span class="muted">×${c.n}</span>` : ""}</td>
      <td class="num">${money(c.bid)}</td><td class="num">${money(c.ask)}</td>
      <td class="num">${c.volume != null ? Number(c.volume).toLocaleString() : "—"}</td>
      <td class="num">${c.oi != null ? Number(c.oi).toLocaleString() : "—"}</td>
      <td class="num ${tone(c.oi_change)}">${c.oi_change != null ? (c.oi_change >= 0 ? "+" : "−") + Math.abs(c.oi_change).toLocaleString() : "—"}</td>
      <td class="num ${c.vol_oi >= 1 ? "up" : ""}">${c.vol_oi != null ? c.vol_oi.toFixed(2) : "—"}</td>
      <td class="num">${c.iv != null ? (c.iv * 100).toFixed(0) + "%" : "—"}</td></tr>`).join("") + "</tbody>";
}

function whalesHTML(d) {
  if (!d.prints.length) return '<tr><td class="muted">No whale trades logged on this ticker that day.</td></tr>';
  const lean = p => p.lean === "bull" ? '<span class="up">Bullish</span>' : p.lean === "bear" ? '<span class="down">Bearish</span>' : '<span class="muted">Unclear</span>';
  const side = p => ({ buy: "Bought", sell: "Sold", mid: "Mid" })[p.side];
  return `<thead><tr><th>Time</th><th>Contract</th><th class="num">Size</th><th class="num">Price</th><th class="num">Premium</th>
      <th data-tip="side">Whale</th><th data-tip="flowlean">Lean</th><th>Type</th></tr></thead><tbody>` +
    d.prints.map(p => `<tr><td>${clock(d.session + " " + p.time.slice(0, 5))}</td>
      <td><b>${strike(p.strike)} ${pcWord(p.put_call)}</b> <span class="muted">${day(p.expiration)}</span></td>
      <td class="num">${Number(p.size).toLocaleString()}</td><td class="num">${money(p.price)}</td>
      <td class="num"><b>${bigMoney(p.premium)}</b></td><td>${side(p)}</td><td>${lean(p)}</td><td class="muted">${esc(p.activity)}</td></tr>`).join("") + "</tbody>";
}

function renderTicker(d) {
  if (d.state === "no_data") {
    $("#tHead").innerHTML = `<div class="d-head"><h1>${esc(d.ticker)}</h1></div><p class="muted">No IBKR prices saved for this ticker yet - they start at the next market open.</p>`;
    return;
  }
  $("#tHead").innerHTML = tickerHeadHTML(d);
  state.tPrev = d.price; state.tLast = d.price; state.tChange = d.change;
  drawTicker(d);
  $("#tBook").innerHTML = bookHTML(d);
  $("#tPanels").innerHTML = tickerPanelsHTML(d);
  $("#tContracts").innerHTML = contractsHTML(d);
  $("#tWhales").innerHTML = whalesHTML(d);
  $("#tWhalesTitle").textContent = `Whale trades on ${d.ticker} ${d.market_open ? "today" : "on " + day(d.session)} (newest first)`;
}

// ---------- detail ----------
const toTime = m => Date.UTC(+m.slice(0, 4), +m.slice(5, 7) - 1, +m.slice(8, 10), +m.slice(11, 13), +m.slice(14, 16)) / 1000;

function drawChart(d) {
  if (state.chart) { state.chart.remove(); state.chart = null; }
  const el = $("#chart");
  if (!d.bids.length || !window.LightweightCharts) {
    el.innerHTML = `<div class="no-chart">${window.LightweightCharts ? "No prices recorded for this contract yet." : "Chart library didn't load (no internet?)."}</div>`;
    return;
  }
  const chart = LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { type: "solid", color: "transparent" }, textColor: "#c9d1d9", fontFamily: "Segoe UI, system-ui, sans-serif" },
    grid: { vertLines: { color: "#1f2630" }, horzLines: { color: "#1f2630" } },
    rightPriceScale: { borderColor: "#2d3540" },
    timeScale: {
      borderColor: "#2d3540", timeVisible: true, secondsVisible: false,
      // times are Eastern wall-clock stored as UTC, so format them in UTC; a new day reads "Oct 2"
      tickMarkFormatter: (t, type) => {
        const dt = new Date(t * 1000);
        if (type <= 2) return dt.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
        return `${dt.getUTCHours() % 12 || 12}:${String(dt.getUTCMinutes()).padStart(2, "0")}`;
      },
    },
    localization: { timeFormatter: t => clock(new Date(t * 1000).toISOString().replace("T", " ")) },
    crosshair: { mode: 0 },
  });
  state.chart = chart;
  const ask = chart.addLineSeries({ color: "rgba(88,166,255,0.5)", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  ask.setData(d.asks.map(([m, v]) => ({ time: toTime(m), value: v })));
  const bid = chart.addLineSeries({ color: "#3fb950", lineWidth: 2, priceLineVisible: false });
  const bidData = d.bids.map(([m, v]) => ({ time: toTime(m), value: v }));
  bid.setData(bidData);

  const line = (price, color, title, style = 2, label = true) =>
    bid.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: label, title });
  line(d.fill, "#e6edf3", "Whale paid", 2);
  [[1.3, "+30%"], [1.5, "+50%"], [2, "+100%"]].forEach(([k, t]) => line(d.fill * k, "rgba(210,153,34,0.6)", t, 3, false));
  if (d.model != null) line(d.fill * (1 + d.model), "#a371f7", `Model target ${pct(d.model)}`, 2);

  // markers have to sit on a time that exists in the series
  const snap = m => { const t = toTime(m); const hit = bidData.find(p => p.time >= t); return (hit || bidData[bidData.length - 1]).time; };
  const marks = [{ time: snap(d.trade_date + " " + d.time), position: "belowBar", color: "#e6edf3", shape: "arrowUp", text: "Whale buys" }];
  if (d.pinged_at) marks.push({ time: snap(d.pinged_at), position: "belowBar", color: "#58a6ff", shape: "circle", text: "Ping sent" });
  if (d.best_at) marks.push({ time: snap(d.best_at), position: "aboveBar", color: "#3fb950", shape: "circle", text: `Best ${pct(d.best_pnl)}` });
  const byMinute = {};
  d.alerts_at.forEach(a => (byMinute[a.minute] = byMinute[a.minute] || []).push(a.level));
  Object.entries(byMinute).forEach(([m, levels]) => marks.push({
    time: snap(m), position: "aboveBar", color: levels.some(l => l.startsWith("-")) ? "#f85149" : "#d29922",
    shape: "arrowDown", text: "Alert " + levels.sort((a, b) => ALERTS.indexOf(a) - ALERTS.indexOf(b)).join(" "),
  }));
  bid.setMarkers(marks.sort((a, b) => a.time - b.time));

  const legend = $("#chart-legend");
  chart.subscribeCrosshairMove(p => {
    const v = p && p.seriesData ? p.seriesData.get(bid) : null;
    if (!v) { legend.innerHTML = `Bid ${money(d.bid)} · <span class="${tone(d.pnl)}">${pct(d.pnl)}</span>`; return; }
    const dt = new Date(v.time * 1000);
    const hh = dt.getUTCHours(), mm = String(dt.getUTCMinutes()).padStart(2, "0");
    legend.innerHTML = `${day(dt.toISOString())} ${hh % 12 || 12}:${mm} ${hh < 12 ? "AM" : "PM"} · Bid ${money(v.value)} · <span class="${tone(v.value / d.fill - 1)}">${pct(v.value / d.fill - 1)}</span>`;
  });
  legend.innerHTML = `Bid ${money(d.bid)} · <span class="${tone(d.pnl)}">${pct(d.pnl)}</span>`;
  // a little room on both sides so the first/last marker labels aren't cut off
  const n = bidData.length;
  chart.timeScale().setVisibleLogicalRange({ from: -Math.max(4, n * 0.06), to: n + Math.max(2, n * 0.02) });
}

const FLAG_WORDS = {
  sweep_or_block: "a sweep or block (bought fast across exchanges, or as one big order)",
  aggressive_execution: "aggressive (paid at or above the ask - an urgent buyer)",
  algo_edge: "flagged by Trade Echo's Algo Edge",
};

function storyHTML(d) {
  const start = d.trade_date + " " + d.time;
  const verb = { buy: "bought", sell: "sold" }[d.context && d.context.side] || "traded";
  const s = [`On <b>${day(d.trade_date)} at ${clock(start)}</b>, a whale ${verb} <b>${Number(d.size).toLocaleString()}</b> contracts of
    <b>${esc(name(d))}</b> at <b>${money(d.fill)}</b> each (${bigMoney(d.premium)} in total).`];
  if (d.flags.length) s.push(`The trade was ${d.flags.map(f => FLAG_WORDS[f] || esc(f.replace(/_/g, " "))).join(" and ")}.`);
  if (d.context) {
    const c = d.context, q = c.quote_pos;
    const where = q == null ? "" : q <= 0 ? " IBKR's quote that minute puts the fill at or below the bid."
      : q >= 1 ? " IBKR's quote that minute puts the fill at or above the ask."
      : ` IBKR's quote that minute puts the fill ${Math.round(q * 100)}% of the way from bid to ask.`;
    s.push({
      buy: `Trade Echo marks it as a <b class="up">buy at the ask</b> - an aggressive buyer.`,
      sell: `Trade Echo marks it as a <b class="down">sale at the bid</b> - the whale was SELLING, so following it means taking the other side.`,
      mid: `It traded <b>between the bid and the ask</b>, so whether the whale bought or sold is unclear.`,
    }[c.side] + where);
    if (c.structure && d.legs) s.push(`🧩 It was one leg of a <b>${esc(c.structure)}</b>: in the same second the whale also traded
      ${Number(d.legs.size).toLocaleString()} × ${strike(d.legs.strike)} ${pcWord(d.legs.put_call)} ${day(d.legs.expiration)}
      (${esc((d.legs.sentiment || "").toLowerCase())}). This contract was the leg <b>${esc(c.our_leg)}</b>.`);
  }
  if (d.whale_watch) {
    const w = d.whale_watch, secs = Math.round((Date.parse(w.seen.replace(" ", "T") + "Z") -
      Date.parse(d.trade_date + "T" + w.trade_time + "Z")) / 1000);
    s.push(`Our <span class="term" data-tip="latency">whale watch</span> spotted it at <b>${clock(w.seen)}</b>` +
      ` (${secs < 120 ? secs + " seconds" : Math.round(secs / 60) + " min"} after the trade)` +
      (d.listed_at ? `; Trade Echo's scored list had it at ${clock(d.listed_at)}.` : "."));
  }
  if (d.pinged_at) {
    const lag = minutesBetween(start, d.pinged_at);
    s.push(`Our ping went out at <b>${clock(d.pinged_at)}</b>, <span data-tip="latency" class="term">${lag} min after the whale</span>` +
      (d.ping_entry && d.ping_entry.ask ? `; at that moment the ask was <b>${money(d.ping_entry.ask)}</b> (${pct(d.ping_entry.ask / d.fill - 1)} vs. the whale).` : "."));
  }
  if (d.best != null) s.push(`The best price you could have sold at afterwards was <b class="up">${money(d.best)} (${pct(d.best_pnl)})</b> at ${when(d.best_at, d.trade_date)}.`);
  if (d.state === "live") s.push(`Right now the bid is <b>${money(d.bid)}</b> (<b class="${tone(d.pnl)}">${pct(d.pnl)}</b>).`);
  else if (d.state === "expired") s.push(`The contract expired on ${day(d.expiration)}. Its last recorded bid was <b>${money(d.bid)}</b> (<b class="${tone(d.pnl)}">${pct(d.pnl)}</b>).`);
  else if (d.bid != null) s.push(`At the last close the bid was <b>${money(d.bid)}</b> (<b class="${tone(d.pnl)}">${pct(d.pnl)}</b>). It keeps updating when the market opens.`);
  if (d.best_pnl != null && d.pnl != null && d.best_pnl - d.pnl > 0.5)
    s.push(`<span class="muted">Lesson: this one gave back a big gain - taking profit near the high would have mattered.</span>`);
  return s.map(x => `<p>${x}</p>`).join("");
}

function aiHTML(d) {
  const start = d.trade_date + " " + d.time;
  let verdict = "";
  if (d.model != null && d.best_pnl != null) {
    const target = d.fill * (1 + d.model);
    const reached = d.bids.find(([m, v]) => m > start && v >= target);
    verdict = reached
      ? `✅ The bid reached the model's target (${money(target)}) at ${when(reached[0], d.trade_date)}.`
      : `❌ The bid never reached the model's target (${money(target)}); the best was ${pct(d.best_pnl)}.`;
  }
  return `<div class="kv">
      <span class="k" data-tip="model">Model target</span><span class="v" style="color:var(--purple)">${pct(d.model)}${d.model != null ? " → " + money(d.fill * (1 + d.model)) : ""}</span>
      <span class="k" data-tip="judges">Hermes (AI judge)</span><span class="v">${pct(d.hermes)}</span>
      <span class="k" data-tip="judges">Qwen (AI judge)</span><span class="v">${pct(d.qwen)}</span>
      <span class="k" data-tip="best">What really happened</span><span class="v ${tone(d.best_pnl)}">${pct(d.best_pnl)} best</span>
    </div>${verdict ? `<div class="verdict">${verdict}</div>` : ""}`;
}

function detailsHTML(d) {
  const g = d.greeks, sp = d.spread, e = d.ping_entry;
  const row = (k, v, tip) => `<span class="k"${tip ? ` data-tip="${tip}"` : ""}>${k}</span><span class="v">${v}</span>`;
  return `<div class="kv">
    ${row("Contracts bought", Number(d.size).toLocaleString())}
    ${row("Premium (total spent)", bigMoney(d.premium), "premium")}
    ${row("Flow score", d.score ?? "—", "score")}
    ${row("Stock price at the trade", g.spot ? money(g.spot) : "—")}
    ${row("Implied volatility", g.iv ? (g.iv * 100).toFixed(0) + "%" : "—", "iv")}
    ${row("Delta", g.delta != null ? g.delta.toFixed(2) : "—", "delta")}
    ${row("Time decay per day", g.theta_contract != null ? money(Math.abs(g.theta_contract)) + " per contract" +
      (d.expiration === d.trade_date ? " (0DTE: all time value is gone by the close)" : "") : "—", "theta")}
    ${row("Bid/ask spread at the trade", sp && sp.spread_at_print != null ? `${money(sp.spread_at_print)} (${(sp.spread_pct_at_print * 100).toFixed(1)}%)` : "—", "spread")}
    ${row("Ask when our ping went out", e && e.ask ? money(e.ask) : "—", "ask")}
    ${row("Open interest at the ping", e && e.open_interest != null ? Number(e.open_interest).toLocaleString() : "—")}
  </div>`;
}

function renderDetail(d) {
  const range = state.chart ? state.chart.timeScale().getVisibleLogicalRange() : null;
  $("#detail").innerHTML = `
    <div class="d-head">
      <h1>${esc(name(d))}</h1>
      <span class="pc ${d.put_call === "CALL" ? "call" : "put"}" data-tip="${d.put_call === "CALL" ? "call" : "put"}">${pcWord(d.put_call).toUpperCase()}</span>
      <span class="chip" data-tip="expiry">${expiryText(d)}</span>
      ${d.state === "live" ? '<span class="state live"><span class="dot"></span>LIVE</span>' : ""}
    </div>
    <div class="big-row">
      <div class="stat"><div class="label" data-tip="fill">Whale paid</div><div class="value">${money(d.fill)}</div><div class="sub">${day(d.trade_date)}, ${clock(d.trade_date + " " + d.time)}</div></div>
      <div class="stat"><div class="label" data-tip="bid">${d.state === "live" ? "Bid now" : "Last bid"}</div><div class="value">${money(d.bid)}</div><div class="sub">${d.bid_at ? when(d.bid_at, d.trade_date) : ""}</div></div>
      <div class="stat"><div class="label" data-tip="pnl">P&amp;L</div><div class="value ${tone(d.pnl)}">${pct(d.pnl)}</div><div class="sub">from the whale's price</div></div>
      <div class="stat"><div class="label" data-tip="best">Best so far</div><div class="value ${tone(d.best_pnl)}">${pct(d.best_pnl)}</div><div class="sub">${d.best_at ? money(d.best) + " at " + when(d.best_at, d.trade_date) : ""}</div></div>
    </div>
    <div class="chart-box">
      <div class="chart-legend" id="chart-legend"></div>
      <div id="chart"></div>
      <div class="chart-key">
        <span><span class="key-line" style="border-color:#3fb950"></span>Bid (what you could sell at)</span>
        <span><span class="key-line" style="border-color:rgba(88,166,255,.6)"></span>Ask (what you'd pay)</span>
        <span><span class="key-line dash" style="border-color:#e6edf3"></span>Whale's price</span>
        <span><span class="key-line dash" style="border-color:#a371f7"></span>Model target</span>
        <span><span class="key-line dash" style="border-color:#d29922"></span>+30% / +50% / +100%</span>
        <span class="muted">Scroll to zoom · drag to move · hover for prices</span>
      </div>
    </div>
    <div class="panels">
      <div class="panel story"><h3>What happened</h3>${storyHTML(d)}</div>
      <div class="panel"><h3>What the AI expected</h3>${aiHTML(d)}</div>
      <div class="panel"><h3>Trade details</h3>${detailsHTML(d)}</div>
    </div>`;
  drawChart(d);
  if (range && state.lastDetail === d.id) state.chart && state.chart.timeScale().setVisibleLogicalRange(range);
  state.lastDetail = d.id;
}

async function loadDetail() {
  try {
    const d = await api("detail", { id: state.detailId });
    state.detailLive = d.state === "live";
    renderDetail(d);
  } catch (e) {
    $("#detail").innerHTML = `<div class="error">Couldn't load this contract: ${esc(e.message)}</div>`;
  }
}

// ---------- charts (Chart.js) ----------
const charts = {};
if (window.Chart) {
  Chart.defaults.color = "#c9d1d9";
  Chart.defaults.borderColor = "#222a35";
  Chart.defaults.font.family = "Segoe UI, system-ui, sans-serif";
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.plugins.legend.labels.boxWidth = 12;
}
function draw(id, config) {
  if (!window.Chart) { $("#" + id).parentElement.innerHTML = '<p class="muted">Chart library didn\'t load (no internet?).</p>'; return; }
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($("#" + id), config);
}
const pctAxis = (extra = {}) => ({ ticks: { callback: v => (v > 0 ? "+" : "") + Math.round(v * 100) + "%" }, ...extra });
function kindName(k) {
  if (k.startsWith("picker:")) return kindName(k.slice(7)) + " (strategy picker)";
  if (k.startsWith("profit:")) return kindName(k.slice(7)) + " (profit-trained)";
  const base = { boosting: "Boosted trees", linear: "Linear", forest: "Random forest", knn: "Nearest neighbours",
    similar_trades: "Similar past trades", judges: "Both AI judges alone", hermes: "Hermes alone", qwen: "Qwen alone" };
  const extra = { judges: " + both AI judges", hermes: " + Hermes", qwen: " + Qwen" };
  const [b, x] = k.split("+");
  return (base[b] || b.replace(/_/g, " ")) + (x ? extra[x] || " + " + x : "");
}
const runTime = utc => new Date(utc).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" });
const dayShort = d => day(d).replace(/^\w+, /, "");

// ---------- learning ----------
function inputTestHTML(t) {
  if (!t) return `<p class="muted small">The first comparison runs after the next nightly training.</p>`;
  const s = t.summary, rank = v => v == null ? "—" : v.toFixed(2);
  // a clear call needs two thirds of the methods to agree; anything closer is noise at this sample size
  const verdict = s.compared === 0 ? "Not enough data to compare yet."
    : s.better >= s.compared * 2 / 3 && (s.profit_with ?? -9) > (s.profit_without ?? -9)
      ? `<b class="up">They help so far:</b> better for ${s.better} of ${s.compared} methods, and tonight's winner earns more with them.`
      : s.worse >= s.compared * 2 / 3 ? `<b class="down">They don't help yet:</b> worse for ${s.worse} of ${s.compared} methods - the models may be fitting noise.`
      : `<b>No clear difference yet</b> (better for ${s.better}, worse for ${s.worse} of ${s.compared} methods).`;
  return `<p class="small" style="margin:0 0 10px">${verdict} Winner with them: <b>${esc(kindName(s.winner_with || ""))}</b>
      <span class="${tone(s.profit_with)}">${pct(s.profit_with)}</span> per trade · without: <b>${esc(kindName(s.winner_without || ""))}</b>
      <span class="${tone(s.profit_without)}">${pct(s.profit_without)}</span>.
      <span class="muted">${t.replay_days} blind days, ${t.n_graded} graded trades - with this little data a few trades can flip it.</span></p>
    <table class="table"><tr><th>Method</th><th class="num">With them</th><th class="num">Without</th><th class="num">Trades with / without</th>
      <th class="num" data-tip="rank">Rank with / without</th></tr>` +
    t.rows.map(r => `<tr><td>${esc(kindName(r.kind))}</td>
      <td class="num ${tone(r.with)}"><b>${pct(r.with)}</b></td><td class="num ${tone(r.without)}">${pct(r.without)}</td>
      <td class="num">${r.n_with ?? "—"} / ${r.n_without ?? "—"}</td><td class="num">${rank(r.rank_with)} / ${rank(r.rank_without)}</td></tr>`).join("") +
    `</table>`;
}

async function loadLearning() {
  try {
    const L = await api("learning");
    const x = L.latest;
    const byProfit = x && x.profit != null;
    $("#learnStats").innerHTML = !x ? "" : byProfit ? `
      <div class="stat"><div class="label">Picks it learned from</div><div class="value">${x.n}</div><div class="sub">graded on real IBKR prices</div></div>
      <div class="stat"><div class="label">Tonight's winner</div><div class="value" style="font-size:18px">${esc(kindName(x.kind))}</div><div class="sub">${x.profit_model ? "trained on real trade profit" : "trained on the spike"}</div></div>
      <div class="stat"><div class="label">Its picks, tested blind</div><div class="value ${tone(x.profit)}">${pct(x.profit)}</div><div class="sub">per trade after fees · ${x.n_pings} trades</div></div>
      <div class="stat"><div class="label">Following every whale</div><div class="value ${tone(x.base_profit)}">${pct(x.base_profit)}</div><div class="sub">per trade, same rules</div></div>` : `
      <div class="stat"><div class="label">Picks it learned from</div><div class="value">${x.n}</div><div class="sub">graded on real IBKR prices</div></div>
      <div class="stat"><div class="label" data-tip="rank">Rank agreement (tested blind)</div><div class="value">${x.rank != null ? x.rank.toFixed(2) : "—"}</div><div class="sub">0 = random, 1 = perfect</div></div>
      <div class="stat"><div class="label">The model's picks that won</div><div class="value up">${pct(x.hit).replace("+", "")}</div><div class="sub">of ${x.n_pings} picks it would ping</div></div>
      <div class="stat"><div class="label">All picks that won</div><div class="value">${pct(x.base).replace("+", "")}</div><div class="sub">${x.base ? (x.hit / x.base).toFixed(1) + "× better when the model chooses" : ""}</div></div>`;
    draw("historyChart", { type: "line", data: {
      labels: L.runs.map(r => runTime(r.run_utc)),
      datasets: [
        { label: "Model's picks: result per trade", data: L.runs.map(r => r.profit), borderColor: "#3fb950", backgroundColor: "#3fb950", tension: .25, spanGaps: true },
        { label: "Every whale: result per trade", data: L.runs.map(r => r.base_profit), borderColor: "#8b949e", backgroundColor: "#8b949e", borderDash: [5, 4], tension: .25, spanGaps: true },
        { label: "Model's picks that reached +30%", data: L.runs.map(r => r.hit), borderColor: "#58a6ff", backgroundColor: "#58a6ff", tension: .25, hidden: byProfit },
      ] }, options: { scales: { y: pctAxis() }, plugins: { tooltip: { callbacks: { label: c => `${c.dataset.label}: ${pct(c.raw)}` } } } } });
    // older contests (before profit judging) only have rank agreement
    const hasProfit = L.board.some(b => b.profit_avg != null);
    const metric = b => (hasProfit ? b.profit_avg : b.spearman) ?? null;
    const board = [...L.board].filter(b => metric(b) != null).sort((a, b) => metric(b) - metric(a)).slice(0, 12);
    draw("boardChart", { type: "bar", data: {
      labels: board.map(b => kindName(b.kind)),
      datasets: [{ label: "Result per trade (blind)", data: board.map(metric),
        backgroundColor: board.map(b => b.kind === L.winner ? "#3fb950" : metric(b) >= 0 ? "#2f7a43" : "#30598f") }] },
      options: { indexAxis: "y", plugins: { legend: { display: false }, tooltip: { callbacks: {
        label: c => { const b = board[c.dataIndex]; return hasProfit ? `${pct(b.profit_avg)} per trade over ${b.n_pings} trades (takes when its guess ≥ ${pct(b.threshold)})`
          : `rank agreement ${(b.spearman ?? 0).toFixed(2)}`; } } } },
        scales: { x: hasProfit ? pctAxis() : { min: 0 } } } });
    $("#strategyTable").innerHTML = L.strategies.length ? `<tr><th>Strategy</th><th>How it trades</th><th class="num">Trades</th>
      <th class="num">Avg per trade</th><th class="num">Won</th><th class="num">0DTE</th><th class="num">1-14 days</th><th class="num">Model's picks</th></tr>` +
      L.strategies.map(s => `<tr><td><b>${esc(s.name)}</b></td><td class="muted" style="white-space:normal;min-width:260px">${esc(s.about)}</td>
        <td class="num">${s.n}</td><td class="num ${tone(s.avg)}">${pct(s.avg)}</td><td class="num">${pct(s.win).replace("+", "")}</td>
        <td class="num ${tone(s.avg_0dte)}">${pct(s.avg_0dte)}</td><td class="num ${tone(s.avg_longer)}">${pct(s.avg_longer)}</td>
        <td class="num">${s.model_picks || "—"}</td></tr>`).join("")
      : `<tr><td class="muted">Strategy results appear after the next nightly run.</td></tr>`;
    $("#inputTest").innerHTML = inputTestHTML(L.input_test);
    draw("replayChart", { type: "bar", data: {
      labels: L.replay.map(d => dayShort(d.day)),
      datasets: [
        { label: "Model's picks", data: L.replay.map(d => d.hit_model), backgroundColor: "#3fb950" },
        { label: "All picks", data: L.replay.map(d => d.hit_all), backgroundColor: "#4b5563" },
      ] }, options: { scales: { y: pctAxis({ min: 0, max: 1 }) }, plugins: { tooltip: { callbacks: {
        label: c => { const d = L.replay[c.dataIndex]; const model = c.datasetIndex === 0;
          return `${c.dataset.label}: ${c.raw == null ? "none" : pct(c.raw).replace("+", "")} won (${model ? d.n_model : d.n_all} picks)`; } } } } } });
    draw("inputsChart", { type: "bar", data: {
      labels: L.inputs.map(i => i.name),
      datasets: [{ data: L.inputs.map(i => i.corr), backgroundColor: L.inputs.map(i => i.corr >= 0 ? "#3fb950" : "#f85149") }] },
      options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { min: -0.6, max: 0.6 } } } });
    initSim();
  } catch (e) {
    $("#learnStats").innerHTML = `<div class="error">Couldn't load: ${esc(e.message)}</div>`;
  }
}

// ---------- exit simulator ----------
const TARGETS = [null, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0];
const STOPS = [-0.10, -0.25, -0.50, -0.75, null];
let simReady = false, simTimer = null;
function simParams() {
  return { group: $("#simGroup").value, side: $("#simSide").value, entry: $("#simEntry").value,
    target: TARGETS[+$("#simTarget").value] ?? "off", stop: STOPS[+$("#simStop").value] ?? "off" };
}
function simLabels() {
  const t = TARGETS[+$("#simTarget").value], s = STOPS[+$("#simStop").value];
  $("#tgtLabel").textContent = t == null ? "off (hold to 4 PM)" : pct(t);
  $("#stopLabel").textContent = s == null ? "off" : pct(s);
}
function initSim() {
  if (simReady) return runSim(true);
  simReady = true;
  $("#simTarget").value = 3;
  ["simGroup", "simSide", "simEntry"].forEach(id => $("#" + id).addEventListener("change", () => runSim(true)));
  ["simTarget", "simStop"].forEach(id => $("#" + id).addEventListener("input", () => {
    simLabels(); clearTimeout(simTimer); simTimer = setTimeout(() => runSim(false), 120);
  }));
  runSim(true);
}
async function runSim(withGrid) {
  simLabels();
  const p = simParams();
  try {
    const r = await api("simulate", p);
    const s = r.stats;
    const why = { target: "Took profit", stop: "Stopped out", close: "Sold at 4 PM" };
    $("#simStats").innerHTML = s ? `
      <div class="stat"><div class="label">Trades</div><div class="value">${s.n}</div><div class="sub">${r.available - s.n ? (r.available - s.n) + " skipped (entry price unknown)" : "all have prices"}</div></div>
      <div class="stat"><div class="label">Won</div><div class="value">${pct(s.win_rate).replace("+", "")}</div><div class="sub">took profit ${s.exits.target} · stopped ${s.exits.stop} · 4 PM ${s.exits.close}</div></div>
      <div class="stat"><div class="label">Average per trade</div><div class="value ${tone(s.avg)}">${pct(s.avg)}</div><div class="sub">median ${pct(s.median)} · best ${pct(s.best)} · worst ${pct(s.worst)}</div></div>
      <div class="stat"><div class="label">$1,000 per trade →</div><div class="value ${tone(s.total_dollars)}">${s.total_dollars >= 0 ? "+" : "−"}$${Math.abs(Math.round(s.total_dollars)).toLocaleString()}</div><div class="sub">total over ${s.n} trades (before fees)</div></div>`
      : `<div class="empty">No trades match these choices${p.entry === "ping" ? " - your real ask is only recorded for live pings" : ""}.</div>`;
    const up = s && s.total_dollars >= 0;
    draw("equityChart", { type: "line", data: { labels: r.curve.map(c => c[0]), datasets: [{ label: "Account", data: r.curve.map(c => c[1]),
      borderColor: up ? "#3fb950" : "#f85149", backgroundColor: up ? "rgba(63,185,80,.12)" : "rgba(248,81,73,.12)", fill: true, pointRadius: 0, tension: .15 }] },
      options: { plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => `$${Math.round(c.raw).toLocaleString()}` } } },
        scales: { x: { ticks: { maxTicksLimit: 6, callback: function (v) { return dayShort(this.getLabelForValue(v)); } } },
          y: { ticks: { callback: v => "$" + Math.round(v).toLocaleString() } } } } });
    draw("histChart", { type: "bar", data: { labels: r.hist.labels, datasets: [{ data: r.hist.counts,
      backgroundColor: r.hist.labels.map((l, i) => i < 4 ? "#f85149" : "#3fb950") }] },
      options: { plugins: { legend: { display: false } }, scales: { y: { title: { display: true, text: "trades" } } } } });
    $("#simTrades").innerHTML = `<tr><th>When</th><th>Contract</th><th>Whale</th><th class="num">You paid</th><th class="num">Result</th><th>How it ended</th></tr>` +
      r.trades.map(t => `<tr class="click" data-id="${t.id}"><td>${dayShort(t.day)} ${clock(t.start)}</td><td>${esc(t.contract)}</td>
        <td>${{ buy: "bought", sell: "sold", mid: "mid", unknown: "—" }[t.side]}</td><td class="num">${money(t.entry)}</td>
        <td class="num ${tone(t.ret)}">${pct(t.ret)}</td><td>${why[t.why]}</td></tr>`).join("");
    if (withGrid) loadGrid(p);
    else markGrid();
  } catch (e) {
    $("#simStats").innerHTML = `<div class="error">Simulator error: ${esc(e.message)}</div>`;
  }
}
async function loadGrid(p) {
  const g = await api("grid", { group: p.group, side: p.side, entry: p.entry });
  const cell = (t, s) => g.cells.find(c => c.target === t && c.stop === s);
  const color = v => v == null ? "transparent" : v >= 0 ? `rgba(63,185,80,${Math.min(.15 + v * 1.5, .85)})` : `rgba(248,81,73,${Math.min(.15 - v * 1.2, .85)})`;
  $("#gridTable").innerHTML = `<tr><th>Take profit ↓ / Stop loss →</th>${g.stops.map(s => `<th>${s == null ? "No stop" : pct(s)}</th>`).join("")}</tr>` +
    g.targets.map(t => `<tr><th>${t == null ? "None (4 PM)" : pct(t)}</th>${g.stops.map(s => { const c = cell(t, s);
      return `<td data-t="${t}" data-s="${s}" style="background:${color(c.avg)}">${c.avg == null ? "—" : pct(c.avg)}<small>${c.win_rate == null ? "" : "won " + pct(c.win_rate).replace("+", "")}</small></td>`; }).join("")}</tr>`).join("");
  markGrid();
}
function markGrid() {
  const t = String(TARGETS[+$("#simTarget").value]), s = String(STOPS[+$("#simStop").value]);
  document.querySelectorAll("#gridTable td").forEach(td => td.classList.toggle("sel", td.dataset.t === t && td.dataset.s === s));
}
$("#gridTable").addEventListener("click", e => {
  const td = e.target.closest("td"); if (!td) return;
  const t = td.dataset.t === "null" ? null : +td.dataset.t, s = td.dataset.s === "null" ? null : +td.dataset.s;
  $("#simTarget").value = TARGETS.findIndex(x => x === t);
  $("#simStop").value = STOPS.findIndex(x => x === s);
  runSim(false);
});

// ---------- contest ----------
const TRADER = { hermes: ["🦉", "#a371f7"], qwen: ["🐉", "#58a6ff"], model: ["🧠", "#3fb950"], all: ["🐋", "#8b949e"] };
let contestU = "core", contestWho = "hermes", contestData = null;
const dollars = v => (v < 0 ? "−$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
async function loadContest() {
  try {
    const C = contestData = await api("contest", { universe: contestU });
    const r = C.rules;
    $("#contestRules").innerHTML = `<b>Same rules for everyone:</b> start with ${dollars(r.cash)} · put ${Math.round(r.size * 100)}% of the account in each trade
      (if the cash is free) · buy at the <span class="term" data-tip="ask">ask</span> right after the whale's trade · each trader
      gets out with <b>the strategy it chose</b> from the playbook (Hermes and Qwen pick one per trade, the model's strategy picker
      too; the baseline uses Standard: ${pct(r.target)} / ${pct(r.stop)} / 4 PM) · $${r.fee.toFixed(2)} per contract fees each way.
      <span class="muted">Fractional contracts are allowed, so expensive options still count. Updated every night after the close.</span>`;
    const ranked = [...C.traders].sort((a, b) => b.final - a.final);
    const medal = ["🥇", "🥈", "🥉", ""];
    $("#contestBoard").innerHTML = ranked.map((x, i) => `
      <div class="stat" style="${x.key === "all" ? "opacity:.75" : ""}">
        <div class="label">${medal[i]} ${TRADER[x.key][0]} <b style="color:${TRADER[x.key][1]}">${esc(x.name)}</b></div>
        <div class="value ${tone(x.ret)}">${dollars(x.final)}</div>
        <div class="sub"><b class="${tone(x.ret)}">${pct(x.ret)}</b> · ${x.trades} trades · won ${x.wins}${x.trades ? ` (${Math.round(x.wins / x.trades * 100)}%)` : ""}</div>
        <div class="sub">Biggest drop: ${pct(x.max_dd)}${x.skipped ? ` · ${x.skipped} skipped (no free cash)` : ""}</div>
        <div class="sub muted">${esc(x.about)}</div>
      </div>`).join("");
    draw("contestChart", { type: "line", data: { labels: ["Start", ...C.days.map(dayShort)],
      datasets: C.traders.map(x => ({ label: `${TRADER[x.key][0]} ${x.name}`, data: [r.cash, ...x.curve], borderColor: TRADER[x.key][1],
        backgroundColor: TRADER[x.key][1], borderDash: x.key === "all" ? [5, 4] : [], tension: .2 })) },
      options: { scales: { y: { ticks: { callback: v => "$" + v.toLocaleString() } } },
        plugins: { tooltip: { callbacks: { label: c => `${c.dataset.label}: ${dollars(c.raw)}` } } } } });
    $("#contestWho").innerHTML = C.traders.map(x => `<button data-who="${x.key}" class="${x.key === contestWho ? "active" : ""}">${TRADER[x.key][0]} ${esc(x.name)}</button>`).join("");
    renderContestTrades();
  } catch (e) {
    $("#contestBoard").innerHTML = `<div class="error">Couldn't load the contest: ${esc(e.message)}</div>`;
  }
}
function renderContestTrades() {
  const x = contestData.traders.find(t => t.key === contestWho);
  const why = { target: "Took profit", stop: "Stopped out", close: "Sold at 4 PM", trail: "Trailing stop hit",
    time: "Time exit", next_day: "Sold next morning", expiry: "Held to expiry" };
  const names = contestData.strategy_names || {};
  $("#contestTrades").innerHTML = `<tr><th>When</th><th>Contract</th><th>Play</th><th class="num">Paid</th><th class="num">Put in</th>
    <th class="num">Result</th><th class="num">Profit</th><th>How it ended</th></tr>` +
    (x.log.length ? x.log.map(t => `<tr class="click" data-id="${t.id}"><td>${dayShort(t.start)} ${clock(t.start)}</td><td>${esc(t.contract)}</td>
      <td>${esc(names[t.play] || t.play || "")}</td>
      <td class="num">${money(t.entry)}</td><td class="num">${dollars(t.stake)}</td><td class="num ${tone(t.ret)}">${pct(t.ret)}</td>
      <td class="num ${tone(t.pnl)}">${t.pnl >= 0 ? "+" : ""}${dollars(t.pnl)}</td><td>${why[t.why] || ""}</td></tr>`).join("")
      : `<tr><td colspan="8" class="muted">No trades yet.</td></tr>`);
}
$("#contestUniverse").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  contestU = b.dataset.u;
  document.querySelectorAll("#contestUniverse button").forEach(x => x.classList.toggle("active", x === b));
  loadContest();
});
$("#contestWho").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  contestWho = b.dataset.who;
  document.querySelectorAll("#contestWho button").forEach(x => x.classList.toggle("active", x === b));
  renderContestTrades();
});
$("#contestTrades").addEventListener("click", e => {
  const row = e.target.closest("tr[data-id]"); if (row) openDetail(row.dataset.id);
});

// ---------- lab ----------
const LAB_STATUS = {
  "searching": ["🔎", "Searching", "Not enough days yet for a blind test."],
  "not profitable yet": ["⏳", "Not profitable yet", "The search's blind trades don't make money reliably yet, or don't beat the luck test. It keeps testing every day as new data arrives."],
  "promising": ["🌱", "Promising", "The blind trades make money and beat most shuffled runs - but not convincingly yet. Paper only; more days needed."],
  "validated": ["✅", "Validated", "The blind trades made money over 30+ trades and beat 95% of shuffled runs. Next: a live paper test before any real money."],
};
async function loadLab() {
  try {
    const L = await api("lab");
    if (!L.run) { $("#labStatus").innerHTML = "The Lab hasn't finished its first run yet - it starts automatically and runs in the background."; return; }
    const r = L.run, st = LAB_STATUS[r.status] || LAB_STATUS["searching"];
    $("#labStatus").innerHTML = `<b style="font-size:18px">${st[0]} ${st[1]}</b><br>${st[2]}
      <span class="muted">Last full search: ${esc(runTime(r.finished_utc))} ET on ${r.n_picks} trades over ${r.n_days} days.</span>`;
    $("#labStats").innerHTML = `
      <div class="stat"><div class="label">Strategies per search</div><div class="value">${(r.n_strategies / 1e6).toFixed(0)}M</div><div class="sub">${r.n_conditions} conditions × ${r.n_columns} entry/exit combos</div></div>
      <div class="stat"><div class="label" data-tip="blindtest">Blind trades</div><div class="value ${tone(r.blind_avg)}">${pct(r.blind_avg)}</div><div class="sub">per trade · ${r.blind_n} trades over ${r.blind_days} days · won ${pct(r.blind_win).replace("+", "")}</div></div>
      <div class="stat"><div class="label" data-tip="lucktest">Luck test</div><div class="value">${L.p == null ? "—" : "p = " + L.p.toFixed(2)}</div><div class="sub">${L.nulls.length} shuffled reruns so far (${L.luck_tests_total} in total)</div></div>
      <div class="stat"><div class="label">$1,000 per blind trade</div><div class="value ${tone(r.blind_total)}">${r.blind_total >= 0 ? "+" : "−"}$${Math.abs(Math.round(r.blind_total * L.dollars_per_trade)).toLocaleString()}</div><div class="sub">before any real money: paper only</div></div>`;
    const bins = [-0.6, -0.4, -0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3, 0.5, 1, 9];
    const labels = bins.slice(0, -1).map((b, i) => `${pct(b)} to ${i === bins.length - 2 ? "more" : pct(bins[i + 1])}`);
    const counts = bins.slice(0, -1).map((b, i) => L.nulls.filter(v => v >= b && v < bins[i + 1]).length);
    const realBin = r.blind_avg == null ? -1 : bins.findIndex((b, i) => r.blind_avg >= b && r.blind_avg < bins[i + 1]);
    const top = Math.max(1, ...counts);
    draw("labNullChart", { type: "bar", data: { labels, datasets: [
      { label: "Shuffled runs", data: counts, backgroundColor: "#4b5563", stack: "a" },
      { label: "The real result", data: labels.map((_, i) => i === realBin ? top : null), backgroundColor: "rgba(63,185,80,.85)",
        stack: "b", barPercentage: 0.35 }] },
      options: { plugins: { tooltip: { callbacks: { label: c => c.datasetIndex ? `The real blind result: ${pct(r.blind_avg)} per trade`
        : `${c.raw} shuffled runs` } } }, scales: { x: { stacked: true }, y: { title: { display: true, text: "shuffled runs" }, ticks: { precision: 0 } } } } });
    const up = (r.blind_total || 0) >= 0;
    draw("labCurveChart", { type: "line", data: { labels: L.curve.map((c, i) => `${dayShort(c[0])} #${i + 1}`),
      datasets: [{ data: L.curve.map(c => c[1]), borderColor: up ? "#3fb950" : "#f85149", pointRadius: 2,
        backgroundColor: up ? "rgba(63,185,80,.12)" : "rgba(248,81,73,.12)", fill: true, tension: .15 }] },
      options: { plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => "$" + Math.round(c.raw).toLocaleString() } } },
        scales: { y: { ticks: { callback: v => "$" + Math.round(v).toLocaleString() } } } } });
    const stratTxt = s => `${s.filters.map(esc).join(" + ")} → ${esc(s.entry)}; ${esc(s.exit)}`;
    $("#labTrades").innerHTML = `<tr><th>Day</th><th>Contract</th><th>Strategy it used (found on earlier days)</th><th class="num">Result</th></tr>` +
      (L.trades.length ? L.trades.map(t => `<tr class="click" data-id="${t.pick_id}"><td>${dayShort(t.day)} ${esc(t.trade_time_et)}</td>
        <td>${esc(t.ticker)} ${strike(t.strike)} ${pcWord(t.put_call)} ${esc(t.expiration.slice(5))}</td>
        <td class="muted" style="white-space:normal;min-width:420px">${stratTxt(t.strategy)}</td>
        <td class="num ${tone(t.ret)}">${pct(t.ret)}</td></tr>`).join("") : `<tr><td colspan="4" class="muted">No blind trades yet.</td></tr>`);
    $("#labTop").innerHTML = `<tr><th>#</th><th>Strategy</th><th class="num">Trades</th><th class="num">Avg (hindsight)</th></tr>` +
      L.top.slice(0, 10).map((s, i) => `<tr><td>${i + 1}</td><td style="white-space:normal;min-width:520px">${stratTxt(s)}</td>
        <td class="num">${s.n_in_sample}</td><td class="num ${tone(s.avg_in_sample)}">${pct(s.avg_in_sample)}</td></tr>`).join("");
  } catch (e) {
    $("#labStatus").innerHTML = `<div class="error">Couldn't load the Lab: ${esc(e.message)}</div>`;
  }
}
$("#labTrades").addEventListener("click", e => {
  const row = e.target.closest("tr[data-id]"); if (row) openDetail(row.dataset.id);
});

// ---------- predictions ----------
let predScope = "mine";
async function loadPredictions() {
  try {
    const P = await api("predictions", { scope: predScope });
    $("#scorecard").innerHTML = P.scorecard.map(s => `
      <div class="stat"><div class="label">${esc(s.name)}</div>
        <div class="value">${s.rank_profit != null ? s.rank_profit.toFixed(2) : "—"} <span class="muted" style="font-size:13px;font-weight:400" data-tip="rank">rank agreement with real trade profit</span></div>
        <div class="sub">Its top ${s.n_top} picks as trades: <b class="${tone(s.top_profit)}">${pct(s.top_profit)} per trade</b>, won ${pct(s.top_win).replace("+", "")}
          (every whale: ${pct(P.base_trade)})</div>
        <div class="sub">Spike: rank ${s.rank != null ? s.rank.toFixed(2) : "—"} · top picks reached +30%: ${pct(s.top_hit).replace("+", "")}</div></div>`).join("");
    draw("calibChart", { data: { labels: P.calibration.map(c => c.label), datasets: [
      { type: "bar", label: "Result per trade", data: P.calibration.map(c => c.avg_trade), backgroundColor: P.calibration.map(c => (c.avg_trade ?? 0) >= 0 ? "#3fb950" : "#f85149") },
      { type: "line", label: "Following every whale", data: P.calibration.map(() => P.base_trade), borderColor: "#d29922", borderDash: [5, 4], pointRadius: 0 },
    ] }, options: { scales: { y: pctAxis(), x: { title: { display: true, text: P.profit_model ? "the model's guess (expected trade result)" : "the model's guess (expected best gain)" } } },
      plugins: { tooltip: { callbacks: { label: c => c.datasetIndex ? `Every whale: ${pct(c.raw)} per trade`
        : `${pct(c.raw)} per trade, won ${pct(P.calibration[c.dataIndex].win).replace("+", "")} (${P.calibration[c.dataIndex].n} picks)` } } } } });
    draw("scatterChart", { type: "scatter", data: { datasets: [{ data: P.scatter.map(([x, y]) => ({ x, y })), pointRadius: 2.5,
      backgroundColor: P.scatter.map(([, y]) => (P.profit_model ? y > 0 : y >= 0.3) ? "rgba(63,185,80,.7)" : "rgba(139,148,158,.5)") }] },
      options: { plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => `guess ${pct(c.raw.x)} → real ${pct(c.raw.y)}` } } },
        scales: { x: pctAxis({ title: { display: true, text: "model's guess" } }),
          y: pctAxis({ title: { display: true, text: P.profit_model ? "real trade result" : "best bid after the trade" } }) } } });
    const sideTxt = r => r.side ? ({ buy: '<span class="chip hit">bought</span>', sell: '<span class="chip hit neg">sold</span>', mid: '<span class="chip">mid</span>' }[r.side]
      + (r.structure ? ` <span class="chip ${r.our_leg === "sold" ? "hit neg" : ""}">🧩 ${esc(r.structure)}</span>` : "")) : '<span class="muted">—</span>';
    $("#predTable").innerHTML = `<tr><th>When</th><th>Contract</th><th data-tip="side">Whale</th><th class="num" data-tip="model">Model</th>
      <th class="num" data-tip="judges">Hermes</th><th class="num" data-tip="judges">Qwen</th><th class="num" data-tip="best">Real best</th>
      <th class="num" data-tip="tradeResult">As a trade</th><th>Result</th></tr>` +
      P.recent.map(r => { const m = r.replay ?? r.pred_max_gain;
        const res = r.real == null ? '<span class="muted">pending</span>' : r.real >= 0.3 ? "✅ winner" : '<span class="muted">no</span>';
        return `<tr class="click" data-id="${r.id}"><td>${dayShort(r.trade_date)} ${clock(r.trade_date + " " + r.trade_time_et)}</td>
          <td>${esc(r.ticker)} ${strike(r.strike)} ${pcWord(r.put_call)} ${esc(r.expiration.slice(5))}${r.pinged ? " 🔔" : ""}</td>
          <td>${sideTxt(r)}</td><td class="num">${pct(m)}</td><td class="num">${pct(r.hermes)}</td><td class="num">${pct(r.qwen)}</td>
          <td class="num ${tone(r.real)}">${pct(r.real)}</td><td class="num ${tone(r.trade)}">${pct(r.trade)}</td><td>${res}</td></tr>`; }).join("");
  } catch (e) {
    $("#scorecard").innerHTML = `<div class="error">Couldn't load: ${esc(e.message)}</div>`;
  }
}
$("#predScope").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  predScope = b.dataset.scope;
  document.querySelectorAll("#predScope button").forEach(x => x.classList.toggle("active", x === b));
  loadPredictions();
});
["#predTable", "#simTrades"].forEach(sel => $(sel).addEventListener("click", e => {
  const row = e.target.closest("tr[data-id]"); if (!row) return;
  openDetail(row.dataset.id);
}));

// ---------- help ----------
function renderHelp() {
  $("#help").innerHTML = `
    <h2>How to read FlowDesk</h2>
    <ol>
      <li><b>Market</b> is your live board: each of your tickers with its price, how busy trading is compared with a normal
        day, momentum, whether buyers or sellers are in control, and which way today's whales are betting. While the market is
        open everything refreshes every 2 seconds and flashes green or red when it changes. Click a ticker for its live candle
        chart (with the whales marked), the order book, and every option contract we're tracking on it.</li>
      <li><b>Pings</b> shows every whale trade we pinged you about. Green numbers = in profit, red = losing.</li>
      <li>Each card measures P&amp;L from the <b>whale's price</b> to the <b>bid</b> - the price you could sell at right now.</li>
      <li>Click a card for its full chart: when the whale bought, when our ping went out, the best moment to sell and the alerts.</li>
      <li>Hover over any dotted word or label to see what it means.</li>
    </ol>
    <p class="muted">FlowDesk only shows data - it never places trades. Prices come from your Interactive Brokers market-data feed;
      nothing here is financial advice.</p>
    <h2>Where the live data comes from</h2>
    <ul>
      <li><b>Option prices</b> (bid/ask on every tracked contract): IBKR's live OPRA feed, which your account has.</li>
      <li><b>Stock prices, volume, VWAP, momentum</b>: IBKR's 1-minute bars, which IBKR updates every few seconds while each
        minute forms. Your account has no real-time <i>stock</i> quotes for the API (IBKR answered "requires additional
        subscription for API"), so there is no live stock bid/ask and <b>buyers vs. sellers is an estimate</b> from the bars.</li>
      <li><b>Order book</b>: needs IBKR Level 2 depth data (IBKR answered "Need additional market data permissions - Depth:
        NASDAQ, ARCA, NYSE..."). </li>
      <li>If you add those subscriptions in IBKR (Client Portal → Settings → Market Data Subscriptions), FlowDesk switches to
        live quotes, exact buying vs. selling from every trade, and the order book on its own - no update needed.</li>
      <li><b>Whale flow</b>: the Trade Echo trades the logger already saves - no extra credits.</li>
    </ul>
    <h2>Glossary</h2>
    <dl class="gloss">${Object.values(GLOSSARY).map(([t, d]) => `<dt>${esc(t)}</dt><dd>${esc(d)}</dd>`).join("")}</dl>`;
}

// ---------- navigation ----------
const LOADERS = { market: () => loadMarket(), live: () => loadList(), learning: () => loadLearning(),
  predictions: () => loadPredictions(), contest: () => loadContest(), lab: () => loadLab() };

function showView(view) {
  if (view === "detail" && state.view !== "detail") state.prevView = state.view;
  state.view = view;
  document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
  $(`#view-${view}`).classList.remove("hidden");
  let tab = view === "detail" ? (state.prevView || "live") : view;
  if (tab === "ticker") tab = "market";
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  if (view !== "detail" && state.chart) { state.chart.remove(); state.chart = null; state.lastDetail = null; }
  if (view !== "ticker" && view !== "detail" && state.tChart) { state.tChart.remove(); state.tChart = null; state.tKey = null; }
  window.scrollTo(0, 0);
}

$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  showView(b.dataset.tab);
  (LOADERS[b.dataset.tab] || (() => {}))();
});
$("#scope").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  state.scope = b.dataset.scope;
  document.querySelectorAll("#scope button").forEach(x => x.classList.toggle("active", x === b));
  loadList();
});
function openDetail(id) {
  state.detailId = id;
  showView("detail");
  $("#back").textContent = { learning: "← Back to Learning", predictions: "← Back to Predictions",
    contest: "← Back to the Contest", lab: "← Back to the Lab", ticker: `← Back to ${state.tTicker}`,
    market: "← Back to the market" }[state.prevView] || "← Back to all pings";
  $("#detail").innerHTML = '<p class="muted">Loading…</p>';
  loadDetail();
}
function openCard(e) {
  const card = e.target.closest(".card"); if (!card) return;
  if (e.type === "keydown" && e.key !== "Enter") return;
  openDetail(card.dataset.id);
}
function goBack() {
  const to = state.prevView || "live";
  showView(to);
  if (to === "live") loadList();   // the others keep their charts and simulator settings
  else if (to === "ticker") { state.tKey = null; loadTicker(); }
  else if (to === "market") loadMarket();
}
$("#cards").addEventListener("click", openCard);
$("#cards").addEventListener("keydown", openCard);
$("#back").addEventListener("click", goBack);
function openTile(e) {
  const tile = e.target.closest(".tile"); if (!tile) return;
  if (e.type === "keydown" && e.key !== "Enter") return;
  openTicker(tile.dataset.t);
}
$("#tiles").addEventListener("click", openTile);
$("#tiles").addEventListener("keydown", openTile);
$("#tBack").addEventListener("click", () => { showView("market"); loadMarket(); });
$("#tContracts").addEventListener("click", e => {
  const row = e.target.closest("tr[data-id]"); if (!row) return;
  openDetail(row.dataset.id);
});
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (state.view === "detail") goBack();
  else if (state.view === "ticker") { showView("market"); loadMarket(); }
});

// tooltips: data-tip is a glossary key or plain text
const tip = $("#tooltip");
document.addEventListener("mouseover", e => {
  const el = e.target.closest("[data-tip]");
  if (!el) { tip.classList.add("hidden"); return; }
  const g = GLOSSARY[el.dataset.tip];
  tip.innerHTML = g ? `<b>${esc(g[0])}</b><br>${esc(g[1])}` : esc(el.dataset.tip);
  tip.classList.remove("hidden");
});
document.addEventListener("mousemove", e => {
  if (tip.classList.contains("hidden")) return;
  const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = e.clientY + 18 + tip.offsetHeight > window.innerHeight ? e.clientY - tip.offsetHeight - 12 : e.clientY + 18;
  tip.style.left = x + "px"; tip.style.top = y + "px";
});

// ---------- start ----------
renderHelp();
loadHealth();
// deep links: #live, #lab, #contest, #learning, #predictions, #help, #ticker/<T> or #pick/<id> open that screen
const deep = location.hash.slice(1);
if (deep.startsWith("pick/")) openDetail(deep.slice(5));
else if (deep.startsWith("ticker/")) openTicker(deep.slice(7).toUpperCase());
else if (LOADERS[deep] || deep === "help") { showView(deep); (LOADERS[deep] || (() => {}))(); }
else { showView("market"); loadMarket(); }

// refresh: every 2 s on the Market, ticker and pings screens while the market is open, else every 15 s
let lastHealth = Date.now();
async function heartbeat() {
  try {
    if (Date.now() - lastHealth > REFRESH_MS) { lastHealth = Date.now(); await loadHealth(); }
    const due = Date.now() - (state.loadedAt[state.view] || 0) >= (state.marketOpen ? LIVE_MS : REFRESH_MS) - 50;
    if (due) {
      state.loadedAt[state.view] = Date.now();
      if (state.view === "market") await loadMarket();
      else if (state.view === "ticker") await loadTicker();
      else if (state.view === "live") await loadList();
      else if (state.view === "detail" && state.detailLive && Date.now() - (state.detailAt || 0) >= REFRESH_MS) {
        state.detailAt = Date.now();
        await loadDetail();
      }
    }
  } catch (e) { /* each screen shows its own errors */ }
  setTimeout(heartbeat, state.marketOpen ? LIVE_MS : REFRESH_MS);
}
setTimeout(heartbeat, LIVE_MS);
