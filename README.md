# Options Flow Research

A local, always-on system that tests one question: **do large options prints ("whale flow") point to contracts that will gain +30% or more after the print?**

It captures noteworthy flow from [Trade Echo](https://tradeecho.com) for a fixed watchlist and scores every pick with local AI judges and a nightly-retrained model. Likely winners are pinged to Discord. Every pick is then graded on real prices from Interactive Brokers (IBKR): the best price you could actually have **sold** at after the print. Everything runs on one Windows PC. No cloud AI is used at runtime.

> Research software, not financial advice. It never places orders: the IBKR connection is market data only, and account, position and order calls are blocked in code.

## How it works

```
Trade Echo (noteworthy flow, every 2 min)
   -> rules filter (watchlist, score > 30, 0-14 DTE, $350K+ premium)
   -> AI judges (Hermes 3 + Qwen 3 via Ollama, local GPU)
   -> ML model (estimates the best sellable gain after the print)
   -> Discord ping when the estimate clears tonight's bar
                       |
IBKR (read-only): 1-minute option + stock prices, best bid after the print,
                  Greeks minute by minute, your live entry quote at ping time
```

### Daily cycle

| When | What |
|---|---|
| **Market hours (9:30-4:00 ET)** | Poll flow every 2 min, judge and score picks, ping Discord. The IBKR tracker updates Greeks and stock context every 5 min and records the live bid/ask right after each ping. Integrity checks run every 30 min. |
| **Nightly (after 4:15 ET)** | Sweep for missed picks, record prices (Trade Echo + IBKR), retrain with a walk-forward model contest, post bot reports. |
| **Overnight (until 9:00 ET)** | Harvest extra real training data with spare credits, backfill IBKR 1-minute prices, recompute Greeks, reload for the open. |

### The honest target

Trade Echo's daily bars include prices from **before** the print, which nobody following the flow could trade. On the same 80 picks, the daily high averaged **+41%** while the best IBKR **bid after the print** averaged **+22%**. The model therefore trains on `true_gain_pct`:

| Source | Weight | Meaning |
|---|---|---|
| `ibkr` | 1.0 | Best bid after the fill minute, from IBKR 1-minute bars |
| `te_confirmed` | 0.4 | Trade Echo day high, only when it is known to come after the print |
| `te_unconfirmed` | not used | The high may predate the print |

The nightly contest replays past days walk-forward (train only on earlier days, predict the next) and is graded **only on IBKR-verified outcomes**.

### Judged on trading profit

Predicting the spike turned out not to be the same as picking profitable trades: the spike model's guesses had +0.39 rank agreement with the best bid but **-0.01** with what a trade actually earned (volatile contracts spike *and* crash). So every pick also gets `trade_ret`: **buy at the ask right after the print, sell at +30% / -50% / the 4 PM bid, minus $0.65 per contract each way**. Every method competes twice - trained on the spike and trained on `trade_ret` (`profit:` methods) - and the contest is decided by the **average blind-test trade result** of the trades each method would take with its own "take when the guess is >= X" rule (at least 5% of the replay). On Oct 3: following every whale -19.3% per trade, the old spike model -18.4%, the winning profit-trained model -6.0% (46 trades).

### Strategy library and the strategy picker

`strategies.py` replays every graded pick under eight ways of trading it, all buying at the ask right after the whale and graded on real IBKR bars (after fees): **Standard** (+30% / -50% / 4 PM), **Quick scalp** (+15% / -15% / 30 min), **Runner** (+100% / -40%), **Trailing stop** (after +20%, sell 15% off the best bid), **One-hour hold**, **Out by 2 PM**, **Hold overnight** (sell at the next 10 AM bid) and **Hold to expiry**. A strategy with no recorded prices for a pick (no next-day bars, expiry not reached) has no result - nothing is priced by formula.

`picker:` models train one regressor per strategy and trade each pick with its best predicted strategy (or skip it). In the walk-forward replay a strategy's result is only used for training once its trade had **closed before the day being predicted** (a hold-to-expiry result can arrive days later), and whales that sold are never taken, exactly like live pings. Pings name the chosen play. Hermes and Qwen get the playbook, each strategy's results on similar earlier trades and their own trading record, and choose a strategy with every take/skip; their Contest accounts trade their choice. On Oct 3 the strategy picker was the first method with a positive blind-test result: +1.4% per trade over 88 trades (within noise for now).

`ibkr.price_legs()` records the real 1-minute bid/ask of each big whale's companion contracts (next strike out, next expiry, the other side) every night, so vertical, calendar and straddle strategies can be added once enough days are recorded.

### Strategy Lab (runs nonstop)

`lab.py --daemon` (kept alive by `run_lab.ps1` / the "Strategy Lab" task, at idle priority so live work always comes first) builds its own strategies - 1-3 conditions on *which* whales (call/put, DTE, time of day, size, whale bought/sold, IV, trend, the model's and judges' opinions, ...) x 6 ways in (now, after 2/5/15 min, a limit at the whale's price, no chasing) x 393 ways out (take-profit x stop-loss x time limits, trailing stops, overnight) - about **300 million strategies per search**, replayed on real IBKR minute bars (its standard column matches `trade_ret` exactly). Because that many tries always find something that looks great in hindsight, it only trusts:

1. **The blind test of the search itself**: for every day D it searches using only days before D (ranking by average trade minus its standard error, then re-ranking the best by how consistently they did day by day) and trades the winner on D.
2. **The luck test**: the whole blind test rerun on outcomes shuffled among each day's picks, over and over; p = share of shuffled runs that did at least as well.

Status goes searching -> not profitable yet -> promising (blind trades positive, p < 0.20) -> **validated** (30+ blind trades, positive, p < 0.05 after 100+ luck tests), announced on Discord; everything stays paper until a live paper test confirms it. On Oct 3 (8 days of data) the best strategy *in hindsight* averaged +97% per trade, while the honest blind test made +17% per trade on 17 trades - all of it from one day, p = 0.25: not proven. FlowDesk's **Lab** tab shows all of it.

### More training data

Whale-size raw prints ($350K+, 0-14 DTE) from the whale watch and the per-ticker polls become training picks (`source = 'whale'`, never pinged) after the close; `py flow_logger.py --whale-history` pulls the past week of them from Trade Echo's raw feed. They get IBKR prices, Greeks and both judges like any pick.

## Files

| File | Purpose |
|---|---|
| `flow_logger.py` | Main 24/7 process: polling, credit budget, scheduling, nightly and overnight runs, CLI |
| `pipeline.py` | Picks, prices, Greeks, features, model contest, LLM judges, Discord pings, harvest |
| `ibkr.py` | Read-only IBKR data: 1-minute bars, honest outcomes, Greeks paths, live tracker, entry quotes |
| `bots.py` | Simulator (backtests + exit-plan grid), Analyst, Judges report, Scout |
| `integrity.py` | 27 rule-based data-integrity checks + a local Qwen audit whose findings are re-verified by code |
| `flow_app.py`, `app_data.py`, `ui/` | **FlowDesk** desktop app: live P&L of every ping since the whale's fill, per-contract charts, model vs. reality (read-only) |
| `trade_context.py` | Was the whale buying (ask) or selling (bid)? Was the print one leg of a spread/collar? No ping when the whale sold |
| `cases.py` | Incident cases: evidence -> local Qwen investigation -> verified write-up in `cases/` + Discord |
| `dashboard.py` | Operator dashboard at http://localhost:8050 (what every bot is doing) |
| `greeks.py` | Black-Scholes IV and Greeks (no API calls) |
| `tradeecho_probe.py` | Minimal Trade Echo MCP client (Streamable HTTP, JSON-RPC) |
| `run_logger.ps1`, `run_ibkr_live.ps1` | Restart loops started by Windows Task Scheduler |
| `reload_logger.ps1` | Restarts the logger so it picks up code changes |
| `setup_webhooks.ps1` | Saves the Discord webhook URLs as user environment variables |

## Setup

**Requirements:** Windows, Python 3.14+, [Ollama](https://ollama.com) with `hermes3` and `qwen3:14b`, a Trade Echo account with MCP access and, optionally, IBKR TWS.

```bash
pip install -r requirements.txt
ollama pull hermes3
ollama pull qwen3:14b
```

**Secrets live only in environment variables, never in files:**

| Variable | What |
|---|---|
| `TRADEECHO_TOKEN` | Trade Echo MCP bearer token |
| `DISCORD_WEBHOOK_URL` | Herald (pings) |
| `DISCORD_WEBHOOK_SCOUT`, `_JUDGES`, `_ARENA`, `_SIMULATOR`, `_ANALYST`, `_AUDITOR` | One channel per bot (`setup_webhooks.ps1` sets these) |
| `IBKR_PORT` | Optional, TWS API port (default 7496) |

**IBKR / TWS:** Global Configuration -> API -> Settings: enable the API, tick **Read-Only API** and **Allow connections from localhost only**. Historical 1-minute bars work without a subscription. Live quotes and IBKR Greeks need the OPRA and US equity streaming subscriptions.

## Running

```bash
py flow_logger.py            # the 24/7 logger (normally started by Task Scheduler)
py flow_logger.py --status   # what's running, credits, picks
py flow_logger.py --check    # data-integrity checks
py flow_logger.py --train    # retrain the model now
py flow_logger.py --ibkr     # IBKR 1-minute prices, stock bars, Greeks
py flow_logger.py --ibkr-live  # market-hours Greeks tracker + entry quotes
py flow_logger.py --bots     # Simulator, Analyst and Judges reports
py flow_logger.py --dashboard
py flow_logger.py --help     # everything else
```

### FlowDesk (desktop app)

```bash
pythonw flow_app.py          # opens the app window (the FlowDesk desktop shortcut runs this)
py flow_app.py --browser     # serve only, at http://127.0.0.1:8060
```

FlowDesk only reads `flow.db`: it never connects to IBKR or Trade Echo and never writes, so it can't disturb the logger, the IBKR tracker or the Discord pings. Market data is shown for personal use only.

| Screen | What it shows |
|---|---|
| **Live** | Every pinged contract: P&L from the whale's fill to the live bid, bought/sold and spread badges, click-through charts with the whale's buy, ping time, alerts and the model's target |
| **Predictions** | Model vs. Hermes vs. Qwen on IBKR outcomes (rank agreement, top-20% hit rate, bias), "when the model says X" calibration from the walk-forward replay, every guess vs. reality, recent picks table |
| **Contest** | Paper money: Hermes, Qwen and the model each start with $5,000 and trade the picks they chose under identical rules (10% per trade, buy at the ask right after the whale, +30% / -50% / 4 PM exit, $0.65/contract fees), vs. a "follow every whale" baseline. A separate scoreboard - never fed back into the judges' prompts or the model |
| **Learning** | Training history, the nightly model contest, the day-by-day walk-forward replay, what the model looks at, and an **exit simulator**: choose trades, whale side, entry (whale's fill / ask right after the trade / your ask at the ping), take-profit and stop-loss, replayed on IBKR's minute bids, with a $1,000-per-trade account curve and a take-profit x stop-loss heat map |

`app_data.py` serves the Live screen, `app_research.py` the Learning and Predictions screens (same exit rule as the Simulator bot).

## Data rules

- **No synthetic data.** Every pick keeps Trade Echo's raw response, and every price traces to a real API call.
- **No peeking.** Judges and models only see outcomes from earlier days. Market-context features use only bars that end before the print minute.
- **Credits.** Trade Echo is capped at 100 credits/hour per connection with a 15-call burst limit. The logger budgets about 98/hour in session and never retries refused calls.
- **Expired options.** IBKR keeps no history for expired contracts, so every pick is priced nightly before it expires.

## Status (Oct 1, 2026)

The data engine works; the edge is not proven yet. On honest after-print prices, 27% of picks reached +30%. The model's replay pings reached +30% 51% of the time (37 pings over 5 days). The LLM judges show almost no signal on honest outcomes. The next milestone is 3-4 weeks of honest data, graded from a real entry price.
