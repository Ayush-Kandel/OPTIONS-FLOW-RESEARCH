"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const ALERTS = ["+30%", "+50%", "+100%", "-50%"];
const REFRESH_MS = 15000;
const state = { view: "live", scope: "open", detailId: null, detailLive: false, chart: null, lastDetail: null };

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
  score: ["Flow score", "Trade Echo's 0-100 rating of how unusual and aggressive the trade was. We only consider trades above 30."],
};

// ---------- formatting ----------
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = v => v == null ? "—" : "$" + Number(v).toFixed(2);
const bigMoney = v => v == null ? "—" : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}K`;
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
    const ib = s.ibkr.ok ? "ok" : s.ibkr.idle ? "idle" : "bad";
    $("#health").innerHTML = `
      <span data-tip="US options trade 9:30 AM - 4:00 PM Eastern, Monday to Friday.">
        <span class="dot ${s.market_open ? "ok" : "idle"}"></span>${s.market_open ? "Market open" : "Market closed"}</span>
      <span data-tip="${s.flow.ok ? "The flow logger is pulling whale trades from Trade Echo." : "The flow logger hasn't checked for new trades recently - is the PC awake?"}">
        <span class="dot ${s.flow.ok ? "ok" : "bad"}"></span>Flow alerts</span>
      <span data-tip="${s.ibkr.ok ? "Live option prices are streaming from Interactive Brokers." : s.ibkr.idle ? "No live prices while the market is closed." : "No live prices for a while - check that TWS is open and logged in."}">
        <span class="dot ${ib}"></span>IBKR prices</span>
      ${s.model ? `<span data-tip="The model that decides what to ping. Retrained every night; this version was trained ${esc(s.model.trained)} ET.">Model: ${esc(s.model.kind)}</span>` : ""}
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
    <div class="whale">Whale bought <b>${Number(c.size).toLocaleString()}</b> at <b>${money(c.fill)}</b> · ${day(c.trade_date)} ${clock(c.trade_date + " " + c.time)} · ${bigMoney(c.premium)}</div>
    <div class="pnl-row">
      <span class="pnl ${tone(c.pnl)}" data-tip="pnl">${pct(c.pnl)}</span>
      <span class="bidnow">${label}<br><b>${money(c.bid)}</b></span>
    </div>
    ${spark(c.spark, c.fill, (c.pnl ?? 0) >= 0)}
    <div class="card-foot">
      <span data-tip="best">Best <b class="${tone(c.best_pnl)}">${pct(c.best_pnl)}</b>${c.best_at ? " at " + when(c.best_at, c.trade_date) : ""}</span>
      <span data-tip="model">Model expected <b>${pct(c.model)}</b></span>
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
};

function storyHTML(d) {
  const start = d.trade_date + " " + d.time;
  const s = [`On <b>${day(d.trade_date)} at ${clock(start)}</b>, a whale bought <b>${Number(d.size).toLocaleString()}</b> contracts of
    <b>${esc(name(d))}</b> at <b>${money(d.fill)}</b> each (${bigMoney(d.premium)} in total).`];
  if (d.flags.length) s.push(`The trade was ${d.flags.map(f => FLAG_WORDS[f] || esc(f.replace(/_/g, " "))).join(" and ")}.`);
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

// ---------- help ----------
function renderHelp() {
  $("#help").innerHTML = `
    <h2>How to read FlowDesk</h2>
    <ol>
      <li><b>Live</b> shows every whale trade we pinged you about. Green numbers = in profit, red = losing.</li>
      <li>Each card measures P&amp;L from the <b>whale's price</b> to the <b>bid</b> - the price you could sell at right now.</li>
      <li>Click a card for its full chart: when the whale bought, when our ping went out, the best moment to sell and the alerts.</li>
      <li>Hover over any dotted word or label to see what it means.</li>
    </ol>
    <p class="muted">FlowDesk only shows data - it never places trades. Prices come from your Interactive Brokers market-data feed;
      nothing here is financial advice.</p>
    <h2>Glossary</h2>
    <dl class="gloss">${Object.values(GLOSSARY).map(([t, d]) => `<dt>${esc(t)}</dt><dd>${esc(d)}</dd>`).join("")}</dl>`;
}

// ---------- navigation ----------
function showView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach(v => v.classList.add("hidden"));
  $(`#view-${view}`).classList.remove("hidden");
  const tab = view === "detail" ? "live" : view;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === tab));
  if (view !== "detail" && state.chart) { state.chart.remove(); state.chart = null; state.lastDetail = null; }
  window.scrollTo(0, 0);
}

$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  showView(b.dataset.tab);
  if (b.dataset.tab === "live") loadList();
});
$("#scope").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  state.scope = b.dataset.scope;
  document.querySelectorAll("#scope button").forEach(x => x.classList.toggle("active", x === b));
  loadList();
});
function openCard(e) {
  const card = e.target.closest(".card"); if (!card) return;
  if (e.type === "keydown" && e.key !== "Enter") return;
  state.detailId = card.dataset.id;
  showView("detail");
  $("#detail").innerHTML = '<p class="muted">Loading…</p>';
  loadDetail();
}
$("#cards").addEventListener("click", openCard);
$("#cards").addEventListener("keydown", openCard);
$("#back").addEventListener("click", () => { showView("live"); loadList(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && state.view === "detail") { showView("live"); loadList(); } });

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
loadList();
setInterval(() => {
  loadHealth();
  if (state.view === "live") loadList();
  else if (state.view === "detail" && state.detailLive) loadDetail();
}, REFRESH_MS);
