"""
auth.py — Standalone Schwab token refresher.
Run this on any laptop with Python 3.10+. Outputs token.json + prints contents
to paste into Railway's SCHWAB_TOKEN_JSON env var.
"""

import json
import sys
from schwab.auth import client_from_manual_flow

# ============================================================
# FILL THESE IN (copy from Railway → Variables tab):
# ============================================================
API_KEY      = "YOUR_SCHWAB_APP_KEY_HERE"
APP_SECRET   = "YOUR_SCHWAB_APP_SECRET_HERE"
CALLBACK_URL = "https://127.0.0.1"          # exactly this — no port, no path
TOKEN_PATH   = "token.json"
# ============================================================


def main():
    if "YOUR_SCHWAB" in API_KEY or "YOUR_SCHWAB" in APP_SECRET:
        print("❌ Edit auth.py and paste your real API_KEY and APP_SECRET first.")
        sys.exit(1)

    print("Starting Schwab manual auth flow…")
    print("=" * 60)
    print("Steps:")
    print("  1. The script will print a long Schwab URL")
    print("  2. Open that URL in any browser")
    print("  3. Log in with your Schwab brokerage credentials")
    print("  4. Click 'Allow' to grant access")
    print("  5. Browser will redirect to https://127.0.0.1/?code=...&session=...")
    print("     (page will fail to load — that's expected)")
    print("  6. COPY THE ENTIRE URL from the browser address bar")
    print("     including the ?code= and everything after")
    print("  7. Paste it here when prompted, then press Enter")
    print("=" * 60)
    print()

    client = client_from_manual_flow(
        API_KEY,
        APP_SECRET,
        CALLBACK_URL,
        TOKEN_PATH,
    )

    print()
    print("✅ SUCCESS — token saved to:", TOKEN_PATH)
    print()
    print("=" * 60)
    print("COPY EVERYTHING BELOW into Railway → SCHWAB_TOKEN_JSON variable:")
    print("=" * 60)
    with open(TOKEN_PATH) as f:
        print(f.read())
    print("=" * 60)


if __name__ == "__main__":
    main()
