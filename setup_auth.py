"""
setup_auth.py — one-time Schwab OAuth, run locally.
Uses manual flow (no port needed in callback URL).
"""

import os
from dotenv import load_dotenv
load_dotenv()

import schwab

API_KEY      = os.environ["SCHWAB_APP_KEY"]
API_SECRET   = os.environ["SCHWAB_APP_SECRET"]
REDIRECT_URI = os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1")
TOKEN_PATH   = os.getenv("SCHWAB_TOKEN_PATH", "token.json")

print(f"Starting Schwab OAuth MANUAL flow…")
print(f"Callback URL: {REDIRECT_URI}")
print()
print("Steps:")
print("  1. A long URL will be printed below.")
print("  2. Copy it and paste into your browser.")
print("  3. Log in to Schwab and click Allow.")
print("  4. Browser redirects to https://127.0.0.1/?code=...  (will show 'Site cant be reached' — IGNORE).")
print("  5. Copy the ENTIRE redirect URL from your browser address bar.")
print("  6. Paste it back here when prompted.")
print()

client = schwab.auth.client_from_manual_flow(
    api_key      = API_KEY,
    app_secret   = API_SECRET,
    callback_url = REDIRECT_URI,
    token_path   = TOKEN_PATH,
)

print(f"\n✅ Token written to {TOKEN_PATH}")
print(f"Next: cat {TOKEN_PATH}  →  paste full JSON into Railway env var SCHWAB_TOKEN_JSON")
