"""
github_store.py
Manages two files stored in a GitHub repo:

    tickers.txt    – one ticker per line
    whitelist.txt  – one Telegram user-id per line

Required env vars:
    GITHUB_TOKEN       – PAT with `repo` scope
    GITHUB_REPO        – e.g.  "youruser/collar-bot-data"
    GITHUB_TICKERS_PATH    default "tickers.txt"
    GITHUB_WHITELIST_PATH  default "whitelist.txt"
"""

import os
import time
import logging
from github import Github, GithubException

logger = logging.getLogger(__name__)

_TOKEN          = os.getenv("GITHUB_TOKEN", "")
_REPO_NAME      = os.getenv("GITHUB_REPO", "")
_TICKER_PATH    = os.getenv("GITHUB_TICKERS_PATH",   "tickers.txt")
_WHITELIST_PATH = os.getenv("GITHUB_WHITELIST_PATH", "whitelist.txt")

# simple in-memory TTL cache so we don't hammer GitHub on every command
_CACHE_TTL = 60   # seconds
_cache: dict[str, tuple[float, list[str]]] = {}


# ---------------------------------------------------------------------------
def _repo():
    if not (_TOKEN and _REPO_NAME):
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPO must be set.")
    return Github(_TOKEN).get_repo(_REPO_NAME)


def _get_file(path: str):
    repo = _repo()
    try:
        return repo, repo.get_contents(path)
    except GithubException as e:
        if e.status == 404:
            repo.create_file(path, f"init {path}", "")
            return repo, repo.get_contents(path)
        raise


def _read_lines(path: str) -> list[str]:
    now = time.time()
    cached = _cache.get(path)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    _, contents = _get_file(path)
    raw = contents.decoded_content.decode("utf-8")
    lines = [l.strip() for l in raw.splitlines() if l.strip() and not l.strip().startswith("#")]
    _cache[path] = (now, lines)
    return lines


def _write_lines(path: str, lines: list[str], commit_msg: str):
    repo, contents = _get_file(path)
    new_content = "\n".join(lines) + ("\n" if lines else "")
    repo.update_file(contents.path, commit_msg, new_content, contents.sha)
    _cache.pop(path, None)        # invalidate cache


# ---------------------------------------------------------------------------
# Tickers
# ---------------------------------------------------------------------------

def get_tickers() -> list[str]:
    return sorted({l.upper() for l in _read_lines(_TICKER_PATH)})


def add_ticker(ticker: str) -> tuple[bool, str]:
    ticker = ticker.upper().strip()
    if not ticker.isalpha() or len(ticker) > 6:
        return False, f"'{ticker}' doesn't look like a valid ticker."
    current = get_tickers()
    if ticker in current:
        return False, f"{ticker} is already on the list."
    current.append(ticker)
    _write_lines(_TICKER_PATH, sorted(current), f"add {ticker}")
    return True, f"✅ {ticker} added."


def remove_ticker(ticker: str) -> tuple[bool, str]:
    ticker = ticker.upper().strip()
    current = get_tickers()
    if ticker not in current:
        return False, f"{ticker} is not on the list."
    current.remove(ticker)
    _write_lines(_TICKER_PATH, current, f"remove {ticker}")
    return True, f"🗑 {ticker} removed."


# ---------------------------------------------------------------------------
# Whitelist  (Telegram user IDs)
# ---------------------------------------------------------------------------

def get_whitelist() -> set[int]:
    ids = set()
    for line in _read_lines(_WHITELIST_PATH):
        try:
            ids.add(int(line))
        except ValueError:
            logger.warning(f"whitelist: bad line {line!r}")
    return ids


def is_authorized(user_id: int) -> bool:
    return user_id in get_whitelist()
