#!/bin/bash
set -e

echo "=== Running scanner ==="
docker exec ib-gateway python3 /home/ibgateway/scanner_v3.py

echo "=== Syncing repo ==="
cd ~/collars-scanner
git checkout -- .
git pull origin main --rebase

echo "=== Copying tickers.txt ==="
docker cp ib-gateway:/home/ibgateway/tickers.txt ~/collars-scanner/tickers.txt

echo "=== Pushing tickers.txt ==="
git add tickers.txt
git commit -m "Update tickers - $(date '+%Y-%m-%d %H:%M')" || echo "No changes"
git push origin main

echo "=== Done ==="
