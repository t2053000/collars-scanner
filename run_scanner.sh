#!/bin/bash
# Continuous IB scanner loop — pushes tickers as soon as they're found

while true; do
    HOUR=$(TZ=America/New_York date +%H%M)
    if [ "$HOUR" -ge "0930" ] && [ "$HOUR" -le "1600" ]; then
        echo "=== Scanner cycle starting at $(date) ==="

        cd ~/collars-scanner
        git checkout -- .
        git pull origin main --rebase

        # Run scanner in background inside Docker
        docker exec ib-gateway python3 /home/ibgateway/scanner_v3.py &
        SCANNER_PID=$!

        # Monitor tickers.txt for changes and push immediately
        LAST_HASH=""
        while kill -0 $SCANNER_PID 2>/dev/null; do
            docker cp ib-gateway:/home/ibgateway/tickers.txt ~/collars-scanner/tickers.txt 2>/dev/null
            docker cp ib-gateway:/home/ibgateway/tickers_meta.json ~/collars-scanner/tickers_meta.json 2>/dev/null
            NEW_HASH=$(md5sum ~/collars-scanner/tickers.txt 2>/dev/null | cut -d' ' -f1)
            if [ "$NEW_HASH" != "$LAST_HASH" ] && [ -n "$NEW_HASH" ]; then
                git add tickers.txt tickers_meta.json 2>/dev/null
                git commit -m "Hot tickers - $(date '+%Y-%m-%d %H:%M:%S')" 2>/dev/null && \
                    git push origin main && \
                    echo "📤 Pushed updated tickers at $(date '+%H:%M:%S')"
                LAST_HASH=$NEW_HASH
            fi
            sleep 5
        done

        # Wait for scanner to fully exit
        wait $SCANNER_PID

        # Final push with complete data
        docker cp ib-gateway:/home/ibgateway/tickers.txt ~/collars-scanner/tickers.txt
        docker cp ib-gateway:/home/ibgateway/tickers_meta.json ~/collars-scanner/tickers_meta.json 2>/dev/null
        git add tickers.txt tickers_meta.json 2>/dev/null
        git commit -m "Update tickers - $(date '+%Y-%m-%d %H:%M')" 2>/dev/null && git push origin main

        echo "=== Done — sleeping 5s ==="
        sleep 5
    else
        echo "$(date) — Market closed, sleeping 60s"
        sleep 60
    fi
done
