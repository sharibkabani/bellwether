# Bellwether 📈

**An autonomous crypto trading bot that runs through Kraken — a Canadian-registered exchange. You give it capital and a risk budget; it watches trending coins, decides what to buy, manages the positions 24/7, and texts or emails you a plain-English report every day.**

Bellwether reads the market, asks a **free, open-source LLM** (Llama 3.1 via Ollama by default — runs locally for $0) to estimate each coin's expected return, blends that with price momentum, and trades only where conviction clears a threshold — inside hard risk limits with a kill switch. It runs forever on a schedule and tells you what it did with your money each evening.

```
● KRAKEN PAPER — real Kraken prices, simulated fills (no risk)
14:32:51 equity $5,066.82 · 1 in / 0 out · 1 open
...
Bellwether daily report — 2026-06-22  [KRAKEN]
====================================================
Equity:        $5,066.82
Today's P&L:   +$66.82 (+1.3%)
Trades today (1):
  BUY  0.00873398 BTC @ $63,716.64 — long exp +5.1%, conf 86% — momentum: rising trend
Open positions (1):
  LONG 0.00873398 BTC @ $63,716.64 (now $64,556.50) → +$7.33 (+1.3%)
```

> 🇨🇦 **Why Kraken?** I needed something a Canadian can actually use. Most algo-friendly brokers (Alpaca, Tradier) are geo-blocked here, and Canadian stock brokers either lack a trading API (Wealthsimple) or restrict it to partners (Questrade). **Kraken is registered with Canadian regulators** (a Restricted Dealer under the OSC/CSA, April 2025), trades cheaply, and has a clean bot-friendly REST API — the cleanest fully-legal path for a self-serve algo bot in Canada.

> ⚠️ **Safe by default.** Out of the box (`mode: sim`) Bellwether runs a fully **offline simulator** — no network, no keys, no money. `mode: kraken` uses **real Kraken prices** but still **paper-fills** (no keys, no risk) until you pass `--live`, which places real orders. See [Going live](#going-live-on-kraken).

---

## Why it's interesting

A complete autonomous agent loop around a real exchange, built cleanly:

- **An AI that forms opinions — for free.** A pluggable LLM reads each coin and returns an expected return + confidence + one-line rationale as JSON. It defaults to **Groq's free tier running `openai/gpt-oss-120b`** — a 120B-class open-source reasoner, free with one key (no credit card), comfortably within the free daily cap at ~100 calls/day. Switch to local **Ollama** (no key at all), OpenRouter, OpenAI, or Anthropic with one config line. One `requests`-based OpenAI-compatible client covers them all — no paid SDK required.
- **Discipline, not vibes.** Every trade is **conviction-sized** (a fraction of equity scaled by confidence), capped per-trade / per-day / by gross exposure, and protected by stop-loss, take-profit, and a drawdown **kill switch**. The bot is built to *not* blow up.
- **Real exchange integration.** Live orders route through **Kraken's REST API** — public market data for quotes, and the HMAC-SHA512-signed private API for orders (see [Kraken integration notes](#kraken-integration-notes)). Verified against the live endpoint.
- **Actually autonomous.** Runs on a timer, manages open positions, survives restarts (SQLite-persisted), and reports daily over SMS or email.
- **Swappable everything.** Venue (simulator ↔ live Kraken), strategies (momentum, AI, or both), and notifiers (console, email, SMS) sit behind clean interfaces — the same architecture previously targeted prediction markets and equities, which is why swapping the venue was a contained change.

---

## How it works

```
            ┌────────────────── every 15 min ──────────────────┐
            ▼                                                   │
   ┌─────────────────┐  instruments ┌──────────────────────────┴────┐
   │  Venue          │   + quotes   │  Signal engine                 │
   │  sim | kraken   │ ───────────▶ │  • momentum (price trend)      │
   └─────────────────┘              │  • trending (free LLM)         │
            ▲                       └──────────────┬─────────────────┘
            │ orders                                │ ideas (direction, expected return)
            │                       ┌───────────────▼────────────────┐
            │                       │  Risk manager                  │
            │                       │  conviction sizing · exposure  │
            │                       │  caps · stop-loss · kill switch │
            │                       └───────────────┬────────────────┘
            │       approved, sized orders          │
            └───────────────────────────────────────┘
                            │ fills
                            ▼
                  ┌──────────────────┐      ┌─────────────────────┐
                  │  Portfolio (P&L) │ ───▶ │  Daily report       │
                  │  SQLite-backed   │      │  SMS · email · CLI  │
                  └──────────────────┘      └─────────────────────┘
```

**One cycle:** refresh coins + quotes → close any position that hit stop-loss / take-profit → ask the strategies for expected returns → blend into ranked trade ideas → risk-check and size the entries (in fractional coin amounts) → execute → snapshot equity. The loop repeats on the poll interval and fires the daily report once a day.

### The AI signal (free, pluggable)

For each coin, the model acts as a calibrated analyst and returns the **expected return over ~1 week** (a signed fraction) plus its confidence, reasoning about catalysts, momentum, and sentiment. It's **grounded in live news**: each cycle the bot pulls recent headlines from free crypto RSS feeds (CoinDesk, Cointelegraph, Decrypt — no key) and injects the ones relevant to each coin into the prompt, so "trending" reflects *today's* events rather than the model's training cutoff. Output is parsed defensively (tolerates markdown fences and stray prose).

The provider is one config line:

| Provider | Model | Cost | Setup |
|---|---|---|---|
| **Groq** (default) | `openai/gpt-oss-120b` | **Free**, best quality | free key at [console.groq.com](https://console.groq.com) (no card) → `GROQ_API_KEY` |
| **Ollama** | `qwen3:8b` | **Free**, local, no key | `brew install ollama` → `ollama pull qwen3:8b`, set `provider: ollama` |
| **OpenRouter** | `openai/gpt-oss-120b:free` | Free variants | `OPENROUTER_API_KEY` |
| Anthropic / OpenAI | — | Paid | matching key + `pip install anthropic` for Claude |

**Why Groq + gpt-oss-120b is the default:** it's the best *free* model for this job — 120B-class reasoning for market judgment, the only large free model with strict JSON-schema decoding, and a single free key (no credit card). If you'd rather run with zero keys, switch to Ollama. If no model is reachable (no key, Ollama not running, or a call fails), the strategy **degrades gracefully** to momentum-only — the bot never crashes on a bad LLM call.

### Risk controls (why live trading is defensible)

| Control | What it does |
|---|---|
| Conviction sizing | Position $ = (equity × `position_pct`) scaled by signal confidence |
| Per-trade / gross-exposure caps | Hard ceilings on one coin and on total deployed capital |
| Daily-spend cap | Limits new capital deployed per day |
| Stop-loss / take-profit | Auto-closes positions past −10% / +20% (configurable; crypto-tuned) |
| Drawdown kill switch | Halts all new entries if equity falls 25% from its peak |
| Long-only | Kraken spot is long-only — no leverage, no shorting |
| One position per coin | No stacking or over-concentration |
| **Live wallet reconciliation** | In live mode, sizing/risk use your **real Kraken cash + positions** each cycle, not a config number |
| **Withhold-on-uncertainty** | If a live reconcile fails (network/API), new entries are withheld that cycle — never sized against stale state |

---

## The self-learning loop

Trading runs every 15 minutes; **learning runs once a day** in a separate *reflection* job (`bellwether reflect`, also fired automatically before the daily report). The bot compounds over time instead of repeating the same mistakes:

1. **Prediction journal.** Every cycle, each strategy's signal (coin, expected return, confidence, rationale, price-at-the-time) is written down. You can't learn from mistakes you never recorded — this is the substrate everything else learns from. Once a prediction's horizon (default 24h) elapses, it's **scored** against what the price actually did.
2. **Reflection memory.** The model is shown its own scorecard (hit rate + calibration per coin/strategy, realized P&L) and writes a few short **lessons** into its trading journal (`memory/lessons.md`), which are injected into the next day's analyst prompt — exactly how a disciplined human reviews their journal. No retraining; just better context.
3. **Reliability weights.** Each *(strategy × coin)* earns a bounded trust multiplier (**0.5×–1.5×**) from its track record. Consistently-wrong sources get quietly down-weighted, accurate ones up-weighted — regularized toward the prior and gated on a minimum sample count, so it can't overfit to noise or run away.
4. **Universe discovery.** Daily, the bot pulls Kraken's tradable USD pairs with 24h volume, screens for a **liquidity floor**, and (optionally) lets the model nominate additions. New coins enter on **probation** — watched and traded at a fraction of normal size — and only **graduate** to full sizing once they prove out. Chronically illiquid or unprofitable coins are **retired**.

### Bounded autonomy — the safety line ⚠️

Self-learning bots blow up when they edit their own risk settings. So the line is **hard and enforced in code**, not by convention:

| | |
|---|---|
| **Immutable (human-owned)** | `max_position_per_trade`, `max_daily_spend`, `max_drawdown_pct`, `stop_loss_pct`, `take_profit_pct`, `max_total_exposure`, `max_open_positions` — the bot can **never** touch these |
| **Adaptable (selection only, within hard bounds)** | `min_confidence` (nudged only within `[0.50, 0.70]`), strategy weights (within `[0.5, 3.0]`) |

Every self-change is small, evidence-gated, clamped to its band, written to a **changelog**, and surfaced in the daily email so a human always sees what the bot adjusted and why. The honest caveat: short-term crypto returns are extremely noisy — the durable wins here are **calibration** (stop being overconfident), **avoiding bad coins**, and the **reflection journal**. It makes the bot more disciplined; it doesn't magic up alpha. Slow learning beats fast overfitting.

Turn the whole loop off with `learning.enabled: false`.

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml          # sim mode by default

python -m bellwether.cli markets             # universe + quotes + signals
python -m bellwether.cli once                # run one trading cycle
python -m bellwether.cli run                 # start the always-on loop (+ daily reflection)
python -m bellwether.cli reflect             # run the learning loop now: score, adapt, discover
python -m bellwether.cli status              # show the portfolio / daily report
```

Set `mode: kraken` to use **real Kraken prices** (still paper-fills without `--live`). The AI signal defaults to Groq's free `gpt-oss-120b` — grab a free key at [console.groq.com](https://console.groq.com), put it in `.env` as `GROQ_API_KEY`, and it works at no cost. (Prefer no keys? Set `provider: ollama` and `ollama pull qwen3:8b`.) Without any model reachable, the bot runs on momentum alone.

---

## Going live on Kraken

1. **Open a Kraken account** at [kraken.com](https://www.kraken.com) (available to Canadians) and fund it with what you're willing to risk.
2. **Create an API key** (Settings → API) with *Query Funds* and *Create & Modify Orders* permissions.
3. In `.env`: set `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`.
4. In `config.yaml`: set `mode: kraken` and adjust the `strategy.universe` and risk caps.
5. Run:
   ```bash
   python -m bellwether.cli run               # real prices, PAPER fills (no risk)
   python -m bellwether.cli --live run        # REAL ORDERS, real money
   ```
   Without `--live` the bot uses real Kraken prices but simulates fills — so you can watch it trade the live market with zero risk before committing real money. Start with small caps (`max_position_per_trade`, `max_daily_spend`).

   In `--live` mode the bot **reconciles against your real Kraken wallet every cycle** — it reads your actual USD cash and coin balances and sizes/risks against those, not a config number. If it can't reach Kraken to confirm balances, it withholds new entries that cycle (exits still run). Fund the account with what you're willing to risk; the bot trades within your real balance.

### Notifications

- **Email** (free): `notify.channel: email` + `SMTP_PASSWORD` (a Gmail App Password works).
- **SMS** (~1¢/msg via Twilio): `notify.channel: sms` + `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` + your numbers.

---

## Kraken integration notes

The live client ([`venues/kraken.py`](bellwether/venues/kraken.py)):

- **Quotes** come from Kraken's **public** `/0/public/Ticker` endpoint — no credentials, so even paper mode runs on real prices. (Verified live: BTC/ETH/SOL quotes round-trip correctly.)
- **Orders** use the **private** API with Kraken's auth scheme: an `API-Key` header plus an `API-Sign` HMAC-SHA512 signature over `path + SHA256(nonce + POST body)`, base64-encoded — implemented and unit-tested for determinism.
- **Paper vs live in one class:** `paper=True` fetches real prices and simulates fills; `paper=False` posts real `AddOrder`s. The mode is chosen by the `--live` flag, and live refuses to start without API keys.
- **Fractional sizing & price precision:** quantities are floats (buy 0.0087 BTC) and limit prices are rounded to a precision Kraken accepts for the magnitude.

---

## Testing

```bash
pytest          # 73 tests
```

Coverage spans the simulator, signed portfolio accounting, **live wallet reconciliation** (keep cost basis, adopt untracked holdings, drop sold positions, ignore dust, re-baseline P&L), the **withhold-entries-on-failed-reconcile** safety path, every risk control (conviction sizing, caps, stop-loss/take-profit, kill switch), signal blending, the **LLM client factory + defensive JSON parsing**, the **news RSS parser + relevance matching**, the Kraken **HMAC signature**, **paper-fill**, and **account-snapshot asset mapping** logic, the full trading cycle, and the report renderers.

---

## Project layout

```
bellwether/
├── config.py          configuration + secret handling (env, never YAML)
├── models.py          Instrument, Quote, Position (fractional), Order, Fill, Signal, TradeIdea
├── storage.py         SQLite persistence (positions, fills, prices, equity, predictions, reflections)
├── news.py            free crypto-news RSS feed (grounds the AI signal)
├── portfolio.py       cash, positions, realized/unrealized P&L + live reconciliation
├── risk.py            conviction sizing, exposure caps, stop-loss, kill switch
├── signals/
│   ├── momentum.py    price-trend strategy (deterministic, no network)
│   ├── trending.py    AI strategy — an LLM estimates expected returns
│   ├── llm.py         pluggable LLM client (Ollama/Groq/OpenRouter/OpenAI/Claude)
│   └── engine.py      blends strategies into ranked trade ideas
├── venues/
│   ├── paper.py       offline crypto-market simulator (the default)
│   └── kraken.py      live Kraken client (public quotes + signed private orders)
├── executor.py        sends orders, books fills
├── trader.py          the cycle and the always-on loop (+ journals predictions)
├── learning/
│   ├── journal.py     prediction journal — log every signal, score vs reality
│   ├── reliability.py bounded trust weights per (strategy × coin)
│   ├── memory.py      the bot's trading journal — model writes its own lessons
│   ├── discovery.py   universe discovery (probation → graduate / retire)
│   ├── autotune.py    bounded selection tuning; capital limits are immutable
│   └── reflect.py     the daily reflection orchestrator
├── report.py          daily digest (terminal / HTML / SMS) + what was learned
├── notify/            console · email · sms channels
└── cli.py             run · once · reflect · report · status · markets
```

## Disclaimer

Crypto trading involves substantial risk, including total loss of capital. Bellwether is provided as-is for educational and personal use and is not financial advice. Past simulated performance does not predict real results. Trade only what you can afford to lose, run it in paper mode first, and review the risk limits before using `--live`.

## License

MIT
