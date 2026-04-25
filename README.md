# Collar Scanner Bot

Telegram bot that scans your watchlist for **positive-edge collars** using Schwab options data.

## What it does

For each ticker in your GitHub-hosted `tickers.txt`, the bot checks the next **10 monthly expirations**.  For each expiration it:

1. Finds the nearest **call strike ABOVE** the current price → records the bid/ask mid
2. Finds the nearest **put strike BELOW** the current price → records the bid/ask mid
3. Skips any strike with no market (bid ≤ 0 or ask ≤ 0)
4. Computes the edge:

   ```
   edge           = (call_mid - put_mid) - (spot - put_strike)
   monthly_yield% = edge / spot × (30 / dte) × 100
   ```

5. Keeps the expiration only if `monthly_yield% > 1.0`

All hits across all tickers are sent back to Telegram as **one summary**, sorted by monthly yield % descending.

## Architecture

```
┌───────────┐     /scan      ┌────────────┐
│ Telegram  │ ─────────────> │  Railway   │
│   user    │ <───results─── │  worker    │
└───────────┘                │            │
                             │  ├─ bot.py │
                             │  ├─ scanner.py
                             │  └─ schwab │──> Schwab API (options)
                             └─────┬──────┘
                                   │
                                   ▼
                         ┌─────────────────────┐
                         │  GitHub data repo   │
                         │   tickers.txt       │
                         │   whitelist.txt     │
                         └─────────────────────┘
```

## One-time setup

### 1. Create the GitHub data repo

Create a **private** repo, e.g. `yourname/collar-bot-data`.  Add two files:

**`tickers.txt`**
```
AAPL
MSFT
NVDA
```

**`whitelist.txt`** — one Telegram user-id per line.  The bot command `/whoami` will tell anyone their id.
```
11111111
22222222
33333333
```

### 2. Create a GitHub Personal Access Token

Settings → Developer settings → Personal access tokens → **Fine-grained token** with `Contents: read/write` on the data repo.

### 3. Create a Telegram bot

Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.

### 4. Get Schwab API credentials

You already have these from the Schwab developer portal.  Make sure your app's callback URL is **exactly** `https://127.0.0.1`.

### 5. Run the OAuth flow locally (one-time)

```bash
git clone <this-repo>
cd trading-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in SCHWAB_APP_KEY, SCHWAB_APP_SECRET
python setup_auth.py
```

A browser opens → log in to Schwab → allow access.  The script writes `token.json` and prints its contents.

### 6. Deploy to Railway

1. Push this repo to GitHub (without `.env` / `token.json` – see `.gitignore`).
2. Railway → New Project → Deploy from GitHub.
3. Add environment variables:

   | Key | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from BotFather |
   | `SCHWAB_APP_KEY` | Schwab app key |
   | `SCHWAB_APP_SECRET` | Schwab app secret |
   | `SCHWAB_REDIRECT_URI` | `https://127.0.0.1` |
   | `SCHWAB_TOKEN_JSON` | **entire** contents of `token.json` |
   | `GITHUB_TOKEN` | your PAT |
   | `GITHUB_REPO` | `yourname/collar-bot-data` |

4. Railway starts the worker via `Procfile` (`python main.py`).

## Telegram commands

| Command | Description |
|---|---|
| `/scan` | Run the collar scan across every ticker in `tickers.txt` |
| `/list` | Show current watchlist |
| `/add AAPL TSLA` | Add one or more tickers |
| `/remove AAPL` | Remove tickers |
| `/whoami` | Show your Telegram user-id (for adding to whitelist) |
| `/help` | Show help |

Commands other than `/start`, `/help`, `/whoami` require your Telegram id to be in `whitelist.txt`.

## Tuning

Edit `scanner.py`:

```python
MAX_EXPIRATIONS       = 10     # how many expirations forward to check
MIN_MONTHLY_YIELD_PCT = 1.0    # alert threshold
```

Edit `bot.py`:

```python
SCAN_CONCURRENCY = 5           # parallel Schwab requests
```

## Notes

- The Schwab token auto-refreshes every 7 days.  If the bot ever says "auth failed" in logs, re-run `setup_auth.py` locally and update `SCHWAB_TOKEN_JSON` on Railway.
- This is **not** financial advice.  Mid-price is indicative only; real fills will differ.  Always verify before trading.
