"""
github_store.py
Read/write three plain-text files in a GitHub data repo:
  - tickers.txt          : one ticker per line       (collar/spreads/deepcall)
  - div_tickers.txt      : TICKER:FREQUENCY per line (dca scanner)
  - whitelist.txt        : one Telegram user ID/line (auth)

Env vars:
  GITHUB_TOKEN
  GITHUB_REPO
  GITHUB_TICKERS_PATH      default "tickers.txt"
  GITHUB_DIV_TICKERS_PATH  default "div_tickers.txt"
  GITHUB_WHITELIST_PATH    default "whitelist.txt"
"""

import os
import logging
from github import Github, GithubException

logger = logging.getLogger(__name__)

_TOKEN              = os.getenv("GITHUB_TOKEN", "")
_REPO               = os.getenv("GITHUB_REPO", "")
_TICKERS_PATH       = os.getenv("GITHUB_TICKERS_PATH", "tickers.txt")
_DIV_TICKERS_PATH   = os.getenv("GITHUB_DIV_TICKERS_PATH", "div_tickers.txt")
_WHITELIST_PATH     = os.getenv("GITHUB_WHITELIST_PATH", "whitelist.txt")


def _repo():
    return Github(_TOKEN).get_repo(_REPO)


def _get_or_create_file(path: str, initial_body: str = ""):
    repo = _repo()
    try:
        return repo, repo.get_contents(path)
    except GithubException:
        repo.create_file(path, f"init {path}", initial_body)
        return repo, repo.get_contents(path)


def _read_lines(path: str) -> list[str]:
    _, f = _get_or_create_file(path)
    raw = f.decoded_content.decode("utf-8")
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def _write_lines(path: str, lines: list[str], msg: str):
    repo, f = _get_or_create_file(path)
    body = "\n".join(lines) + "\n"
    repo.update_file(f.path, msg, body, f.sha)


# Tickers (collar/spreads/deepcall)

def get_tickers() -> list[str]:
    try:
        return sorted({t.upper() for t in _read_lines(_TICKERS_PATH)})
    except Exception as e:
        logger.error(f"Failed to read tickers: {e}")
        return []


def add_ticker(ticker: str) -> tuple[bool, str]:
    ticker = ticker.upper().strip()
    if not ticker.isalpha() or len(ticker) > 6:
        return False, f"❌ '{ticker}' doesn't look like a valid ticker."
    current = get_tickers()
    if ticker in current:
        return False, f"ℹ️ {ticker} is already in the watchlist."
    current.append(ticker)
    _write_lines(_TICKERS_PATH, sorted(set(current)), f"add {ticker}")
    return True, f"✅ {ticker} added."


def remove_ticker(ticker: str) -> tuple[bool, str]:
    ticker = ticker.upper().strip()
    current = get_tickers()
    if ticker not in current:
        return False, f"ℹ️ {ticker} is not in the watchlist."
    current.remove(ticker)
    _write_lines(_TICKERS_PATH, current, f"remove {ticker}")
    return True, f"🗑 {ticker} removed."


# Div tickers (dca scanner)

def get_div_tickers() -> dict[str, str]:
    """
    Returns {TICKER: FREQUENCY} parsed from div_tickers.txt.
    Lines starting with # are ignored.
    """
    try:
        out = {}
        for line in _read_lines(_DIV_TICKERS_PATH):
            if line.startswith("#"):
                continue
            # support inline comments
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" in line:
                tk, freq = line.split(":", 1)
                tk = tk.strip().upper()
                freq = freq.strip().upper()[:1]
                if tk and freq in {"M", "Q", "S", "A", "W"}:
                    out[tk] = freq
        return out
    except Exception as e:
        logger.error(f"Failed to read div tickers: {e}")
        return {}


# Whitelist (auth)

def get_whitelist() -> set[int]:
    try:
        ids = set()
        for line in _read_lines(_WHITELIST_PATH):
            if line.startswith("#"):
                continue
            try:
                ids.add(int(line))
            except ValueError:
                pass
        return ids
    except Exception as e:
        logger.error(f"Failed to read whitelist: {e}")
        return set()


def is_authorized(user_id: int) -> bool:
    return user_id in get_whitelist()
