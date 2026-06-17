from ib_async import *
from datetime import datetime
import pandas as pd

ib = IB()
ib.connect('127.0.0.1', 4002, clientId=160)
print("✅ Connected to IBKR")

scan = ScannerSubscription(
    instrument='STK',
    locationCode='STK.US',
    scanCode='HIGH_OPT_IMP_VOLAT',
    abovePrice=5,
    belowPrice=40
)

print(f"[{datetime.now()}] Running scanner...")

try:
    results = ib.reqScannerData(scan)
    
    if results:
        print(f"\nFound {len(results)} tickers")
        
        data = []
        for r in results[:40]:
            # Correct way to access contract in ib_async 2.x
            if hasattr(r, 'contractDetails') and r.contractDetails:
                c = r.contractDetails.contract
                data.append({
                    'symbol': c.symbol,
                    'secType': c.secType,
                    'exchange': c.exchange,
                    'rank': r.rank
                })
        
        if data:
            df = pd.DataFrame(data)
            print(df.to_string(index=False))
            
            filename = f"high_iv_scan_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
            df.to_csv(filename, index=False)
            print(f"\nSaved to {filename}")
        else:
            print("No contract data extracted.")
    else:
        print("No results from scanner.")
        
except Exception as e:
    print(f"Error: {e}")

ib.disconnect()
