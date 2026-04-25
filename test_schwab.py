import os
from dotenv import load_dotenv
load_dotenv()

import schwab

c = schwab.auth.client_from_token_file(
    os.getenv("SCHWAB_TOKEN_PATH", "token.json"),
    os.environ["SCHWAB_APP_KEY"],
    os.environ["SCHWAB_APP_SECRET"],
)

resp = c.get_quote("AAPL")
resp.raise_for_status()
print("✅ Token works! AAPL quote received.")
print(f"   Last price: ${resp.json()['AAPL']['quote']['lastPrice']}")
