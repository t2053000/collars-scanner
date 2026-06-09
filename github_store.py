"""
github_store.py
Read/write plain-text files in a GitHub data repo.

Files managed:
  tickers.txt               one ticker per line
  div_tickers.txt           TICKER:FREQUENCY per line
  whitelist.txt             one Telegram user ID per line
  schwab_token_{uid}.json   per-user Schwab token (one file per Telegram user)

Env vars:
  GITHUB_TOKEN
  GITHUB_REPO
  GITHUB_TICKERS_PATH       default "tickers.txt"
  GITHUB_DIV_TICKERS_PATH   default "div_tickers.txt"
  GITHUB_WHITELIST_PATH     default "whitelist.txt"
"""

import os
import json
import logging
from pathlib import Path
from github import Github, GithubException

logger = logging.getLogger(__name__)

_TOKEN            = os.getenv("GITHUB_TOKEN", "")
_REPO             = os.getenv("GITHUB_REPO", "")
_TICKERS_PATH     = os.getenv("GITHUB_TICKERS_PATH",     "tickers.txt")
_DIV_TICKERS_PATH = os.getenv("GITHUB_DIV_TICKERS_PATH", "div_tickers.txt")
_WHITELIST_PATH   = os.getenv("GITHUB_WHITELIST_PATH",   "whitelist.txt")


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
    raw  = f.decoded_content.decode("utf-8")
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def _write_lines(path: str, lines: list[str], msg: str):
    repo, f = _get_or_create_file(path)
    body    = "\n".join(lines) + "\n"
    repo.update_file(f.path, msg, body, f.sha)


# ---------------------------------------------------------------------------
# Tickers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Div tickers
# ---------------------------------------------------------------------------

def get_div_tickers() -> dict[str, str]:
    try:
        out = {}
        for line in _read_lines(_DIV_TICKERS_PATH):
            if line.startswith("#"):
                continue
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if ":" in line:
                tk, freq = line.split(":", 1)
                tk   = tk.strip().upper()
                freq = freq.strip().upper()[:1]
                if tk and freq in {"M", "Q", "S", "A", "W"}:
                    out[tk] = freq
        return out
    except Exception as e:
        logger.error(f"Failed to read div tickers: {e}")
        return {}


# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Per-user Schwab tokens
# ---------------------------------------------------------------------------

def _schwab_token_path_github(user_id: int) -> str:
    """GitHub path for a user's Schwab token file."""
    return f"schwab_token_{user_id}.json"


def _schwab_token_path_local(user_id: int) -> str:
    """Local filesystem path for a user's Schwab token file."""
    return f"token_{user_id}.json"


def save_schwab_token(user_id: int, token_json: str) -> None:
    """
    Persist a user's Schwab token JSON to GitHub.
    Creates the file if it doesn't exist, updates it if it does.
    """
    gh_path = _schwab_token_path_github(user_id)
    repo    = _repo()
    try:
        existing = repo.get_contents(gh_path)
        repo.update_file(
            gh_path,
            f"update schwab token for user {user_id}",
            token_json,
            existing.sha,
        )
        logger.info(f"Updated Schwab token for user {user_id} in GitHub")
    except GithubException:
        repo.create_file(
            gh_path,
            f"create schwab token for user {user_id}",
            token_json,
        )
        logger.info(f"Created Schwab token for user {user_id} in GitHub")


def load_schwab_token(user_id: int) -> str | None:
    """
    Load a user's Schwab token from GitHub and write it to a local file.
    Returns the local file path, or None if no token exists for this user.
    """
    gh_path    = _schwab_token_path_github(user_id)
    local_path = _schwab_token_path_local(user_id)
    repo       = _repo()
    try:
        f          = repo.get_contents(gh_path)
        token_json = f.decoded_content.decode("utf-8")
        Path(local_path).write_text(token_json)
        logger.info(f"Loaded Schwab token for user {user_id} to {local_path}")
        return local_path
    except GithubException:
        logger.info(f"No Schwab token found in GitHub for user {user_id}")
        return None


def list_schwab_token_user_ids() -> list[int]:
    """
    Return all Telegram user IDs that have a stored Schwab token in GitHub.
    Looks for files matching schwab_token_{int}.json at repo root.
    """
    repo    = _repo()
    user_ids = []
    try:
        contents = repo.get_contents("")
        for item in contents:
            name = item.name
            if name.startswith("schwab_token_") and name.endswith(".json"):
                try:
                    uid = int(name[len("schwab_token_"):-len(".json")])
                    user_ids.append(uid)
                except ValueError:
                    pass
    except Exception as e:
        logger.error(f"Failed to list Schwab token files: {e}")
    return user_ids