"""
github_store.py
Read/write plain-text files in a GitHub data repo.

Files managed:
  tickers.txt               one ticker per line
  div_tickers.txt           TICKER:FREQUENCY per line
  whitelist.txt             one Telegram user ID per line

Schwab tokens — loaded from env vars first, GitHub as fallback:
  Primary token:  SCHWAB_TOKEN_JSON env var (existing)
  Per-user tokens: SCHWAB_TOKEN_JSON_{user_id} env var
                   Falls back to schwab_token_{uid}.json in GitHub repo

On token refresh (save_schwab_token):
  Always writes to GitHub so token persists across redeploys.

Env vars:
  GITHUB_TOKEN
  GITHUB_REPO
  GITHUB_TICKERS_PATH       default "tickers.txt"
  GITHUB_DIV_TICKERS_PATH   default "div_tickers.txt"
  GITHUB_WHITELIST_PATH     default "whitelist.txt"
  SCHWAB_TOKEN_JSON_{uid}   per-user Schwab token JSON (optional, overrides GitHub)
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
    return f"schwab_token_{user_id}.json"


def _schwab_token_path_local(user_id: int) -> str:
    return f"token_{user_id}.json"


def _env_var_for_user(user_id: int) -> str:
    return f"SCHWAB_TOKEN_JSON_{user_id}"


def save_schwab_token(user_id: int, token_json: str) -> None:
    """
    Persist a user's Schwab token JSON to GitHub.
    Always writes to GitHub so token survives redeploys.
    (env var copy is read-only at runtime — Railway vars require manual update)
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
    Load a user's Schwab token and write it to a local file.
    Priority:
      1. SCHWAB_TOKEN_JSON_{user_id} env var (Railway variable)
      2. schwab_token_{user_id}.json file in GitHub repo
    Returns local file path, or None if no token found.
    """
    local_path = _schwab_token_path_local(user_id)

    # 1. Try env var first
    env_key    = _env_var_for_user(user_id)
    token_json = os.getenv(env_key)
    if token_json and token_json.strip():
        Path(local_path).write_text(token_json)
        logger.info(f"Loaded Schwab token for user {user_id} from env var {env_key}")
        return local_path

    # 2. Fall back to GitHub
    gh_path = _schwab_token_path_github(user_id)
    repo    = _repo()
    try:
        f          = repo.get_contents(gh_path)
        token_json = f.decoded_content.decode("utf-8")
        Path(local_path).write_text(token_json)
        logger.info(f"Loaded Schwab token for user {user_id} from GitHub to {local_path}")
        return local_path
    except GithubException:
        logger.info(f"No Schwab token found for user {user_id} (env or GitHub)")
        return None


def list_schwab_token_user_ids() -> list[int]:
    """
    Return all Telegram user IDs that have a stored Schwab token.
    Checks env vars (SCHWAB_TOKEN_JSON_{uid}) first, then GitHub files.
    Deduplicates — if user has both env var and GitHub file, counted once.
    """
    user_ids = set()

    # 1. Scan env vars for SCHWAB_TOKEN_JSON_{uid}
    for key in os.environ:
        if key.startswith("SCHWAB_TOKEN_JSON_"):
            suffix = key[len("SCHWAB_TOKEN_JSON_"):]
            try:
                uid = int(suffix)
                if os.environ[key].strip():  # non-empty value
                    user_ids.add(uid)
            except ValueError:
                pass

    # 2. Scan GitHub repo for schwab_token_{uid}.json
    try:
        repo     = _repo()
        contents = repo.get_contents("")
        for item in contents:
            name = item.name
            if name.startswith("schwab_token_") and name.endswith(".json"):
                try:
                    uid = int(name[len("schwab_token_"):-len(".json")])
                    user_ids.add(uid)
                except ValueError:
                    pass
    except Exception as e:
        logger.error(f"Failed to list Schwab token files from GitHub: {e}")

    return list(user_ids)
