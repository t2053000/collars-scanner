#!/bin/bash
# Continuous IB scanner loop — runs during market hours, 5s pause between cycles

while true; do
    HOUR=$(TZ=America/New_York date +%H%M)
    if [ "$HOUR" -ge "0930" ] && [ "$HOUR" -le "1600" ]; then
        echo "=== Scanner cycle starting at $(date) ==="

        docker exec ib-gateway python3 /home/ibgateway/scanner_v3.py

        echo "=== Syncing repo ==="
        cd ~/collars-scanner
        git checkout -- .
        git pull origin main --rebase

        echo "=== Copying output files ==="
        docker cp ib-gateway:/home/ibgateway/tickers.txt ~/collars-scanner/tickers.txt
        docker cp ib-gateway:/home/ibgateway/tickers_meta.json ~/collars-scanner/tickers_meta.json 2>/dev/null

        echo "=== Pushing ==="
        git add tickers.txt tickers_meta.json
        git commit -m "Update tickers - $(date '+%Y-%m-%d %H:%M')" || echo "No changes"
        git push origin main

        echo "=== Done — sleeping 5s ==="
        sleep 5
    else
        sleep 60
    fi
done
