from ib_async import *
import pandas as pd
from datetime import datetime
import time
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

IB_HOST = "127.0.0.1"
IB_PORT = 4001
CLIENT_ID = 999
TIMEOUT = 25
PRICE_MIN = 0.5
PRICE_MAX = 35
AVG_OPT_VOL_MIN = 3500
MAX_RESULTS_PER_SCAN = 80
STAGE2_LIMIT = 70

SCAN_CODES = [
    ("HOT_BY_OPT_VOLUME", "Hot by Option Volume (vs 10-day avg)"),
    ("HIGH_OPT_IMP_VOLAT", "Highest Option Imp Vol"),
    ("HIGH_OPT_IMP_VOLAT_OVER_HIST", "High IV vs Historical"),
]

ALWAYS_INCLUDE = ["SOXS", "UVXY", "LABD", "SQQQ", "SPXS", "TECS", "DUST", "JDST", "TZA", "YANG",
                  "VXX", "VIXY", "BOIL", "KOLD", "SOXL", "LABU", "TECL", "BULZ", "CONL", "MSTU", "BLZE"]

OUTPUT_DIR = "/home/ibgateway/scans"

def run_scanner(ib, scan_code, label, max_results=None):
    logger.info(f"=== Running: {label} ({scan_code}) ===")
    scan = ScannerSubscription(
        instrument="STK",
        locationCode="STK.US.MAJOR",
        scanCode=scan_code,
        abovePrice=PRICE_MIN,
        belowPrice=PRICE_MAX,
        averageOptionVolumeAbove=AVG_OPT_VOL_MIN,
        stockTypeFilter="ALL",
        numberOfRows=max_results if max_results else MAX_RESULTS_PER_SCAN,
    )
    try:
        results = ib.reqScannerData(scan)
        candidates = []
        limit = max_results if max_results else MAX_RESULTS_PER_SCAN
        for r in results[:limit]:
            if hasattr(r, "contractDetails") and r.contractDetails:
                candidates.append({
                    "symbol": r.contractDetails.contract.symbol,
                    "rank": getattr(r, "rank", None),
                    "scan_code": scan_code,
                    "label": label
                })
        logger.info(f"Found {len(candidates)} tickers")
        return candidates
    except Exception as e:
        logger.error(f"Scanner error ({scan_code}): {e}")
        return []

def get_skew_and_straddle(ib, symbol):
    try:
        stock = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(stock)
        t = ib.reqMktData(stock, snapshot=True)
        ib.sleep(1.8)
        price = t.marketPrice()
        if not price or price < 1:
            return None, None, "No price"

        chain = ib.reqSecDefOptParams(stock.symbol, "", "STK", stock.conId)[0]
        exp = sorted(chain.expirations)[0]

        atm_strike = min(chain.strikes, key=lambda x: abs(x - price))
        call_atm = Option(symbol, exp, atm_strike, "C", "SMART")
        put_atm = Option(symbol, exp, atm_strike, "P", "SMART")
        ib.qualifyContracts(call_atm, put_atm)
        ct = ib.reqMktData(call_atm, snapshot=True)
        pt = ib.reqMktData(put_atm, snapshot=True)
        ib.sleep(2)
        straddle = (ct.marketPrice() or 0) + (pt.marketPrice() or 0)
        straddle_pct = round((straddle / price) * 100, 1) if straddle > 0 else 0

        upper_strike = min(chain.strikes, key=lambda x: abs(x - price * 1.08))
        lower_strike = min(chain.strikes, key=lambda x: abs(x - price * 0.92))

        call_otm = Option(symbol, exp, upper_strike, "C", "SMART")
        put_otm  = Option(symbol, exp, lower_strike, "P", "SMART")
        ib.qualifyContracts(call_otm, put_otm)
        ct_otm = ib.reqMktData(call_otm, snapshot=True)
        pt_otm = ib.reqMktData(put_otm, snapshot=True)
        ib.sleep(2)

        call_p = ct_otm.marketPrice() or 0
        put_p  = pt_otm.marketPrice() or 0

        if call_p < 0.02 or put_p < 0.02:
            call_p = ct.marketPrice() or 0
            put_p  = pt.marketPrice() or 0

        skew_ratio = round(put_p / call_p, 2) if call_p > 0.02 else 0

        if skew_ratio > 1.45:
            skew_type = "Put Skew"
        elif skew_ratio < 0.72:
            skew_type = "Call Skew"
        else:
            skew_type = "Balanced"

        return straddle_pct, skew_ratio, skew_type

    except Exception as e:
        return None, None, "Error"

def main():
    import os
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ib = IB()
    try:
        ib.connect(IB_HOST, IB_PORT, clientId=CLIENT_ID, timeout=TIMEOUT)
        ib.reqMarketDataType(3)
        logger.info("✅ Connected to IBKR Gateway\n")

        all_candidates = []
        for scan_code, label in SCAN_CODES:
            if scan_code == "HIGH_OPT_IMP_VOLAT":
                global PRICE_MIN, PRICE_MAX
                price_ranges = [(0.5, 10), (9, 20), (18, 35)]
                for p_min, p_max in price_ranges:
                    original_min, original_max = PRICE_MIN, PRICE_MAX
                    PRICE_MIN, PRICE_MAX = p_min, p_max
                    candidates = run_scanner(ib, scan_code, label)
                    PRICE_MIN, PRICE_MAX = original_min, original_max
                    all_candidates.extend(candidates)
                    logger.info(f"  → {label} ({p_min}-{p_max}): {len(candidates)} candidates")
                    time.sleep(2)
            else:
                candidates = run_scanner(ib, scan_code, label)
                all_candidates.extend(candidates)
                logger.info(f"  → {label}: {len(candidates)} candidates")
                time.sleep(2)

        df = pd.DataFrame(all_candidates)
        if df.empty:
            df = pd.DataFrame(columns=["symbol", "rank", "scan_code", "label"])

        df = df.sort_values("rank", na_position="last").drop_duplicates(subset=["symbol"], keep="first")

        existing = set(df["symbol"].tolist())
        forced = [{"symbol": sym, "rank": None, "scan_code": "FORCED", "label": "Always Include"} 
                  for sym in ALWAYS_INCLUDE if sym not in existing]
        if forced:
            df = pd.concat([df, pd.DataFrame(forced)], ignore_index=True)

        top_symbols = df.head(STAGE2_LIMIT)["symbol"].tolist()
        logger.info(f"\n=== Running Stage 2 (Skew + Straddle) on top {len(top_symbols)} symbols ===")

        stage2_data = {}
        for sym in top_symbols:
            straddle_pct, skew_ratio, skew_type = get_skew_and_straddle(ib, sym)
            stage2_data[sym] = {"straddle_pct": straddle_pct, "skew_ratio": skew_ratio, "skew_type": skew_type}
            time.sleep(1.2)

        df["straddle_pct"] = df["symbol"].map(lambda x: stage2_data.get(x, {}).get("straddle_pct"))
        df["skew_ratio"] = df["symbol"].map(lambda x: stage2_data.get(x, {}).get("skew_ratio"))
        df["skew_type"] = df["symbol"].map(lambda x: stage2_data.get(x, {}).get("skew_type"))

        df = df.sort_values(["skew_type", "straddle_pct"], ascending=[True, False])

        output_file = f"{OUTPUT_DIR}/high_iv_candidates.csv"
        df.to_csv(output_file, index=False)
        logger.info(f"Saved to: {output_file}")

        puts_expensive = df[df["skew_type"] == "Put Skew"]["symbol"].tolist()
        calls_expensive = df[df["skew_type"] == "Call Skew"]["symbol"].tolist()

        logger.info("\n" + "=" * 70)
        logger.info(f"TOTAL UNIQUE TICKERS: {len(df)}")
        logger.info("=" * 70)

        print("\n=== PUTS EXPENSIVE (Put Skew) ===")
        print(", ".join(puts_expensive) if puts_expensive else "None")

        print("\n=== CALLS EXPENSIVE (Call Skew) ===")
        print(", ".join(calls_expensive) if calls_expensive else "None")

        print("\n=== TOP 15 BY STRADDLE % ===")
        top_straddle = df.dropna(subset=["straddle_pct"]).head(15)
        for _, row in top_straddle.iterrows():
            print(f"{row['symbol']}: {row['straddle_pct']}% | {row['skew_type']}")

        final_list = list(dict.fromkeys(puts_expensive + calls_expensive + df["symbol"].head(40).tolist()))
        with open("/home/ibgateway/tickers.txt", "w") as f:
            f.write(str(final_list))

        # Write metadata for source tracking
        import json
        meta = []
        for sym in final_list:
            row = df[df["symbol"] == sym].iloc[0] if sym in df["symbol"].values else None
            meta.append({
                "symbol": sym,
                "scan_code": row["scan_code"] if row is not None else "FORCED",
                "skew_type": str(row.get("skew_type", "")) if row is not None else "",
                "straddle_pct": float(row["straddle_pct"]) if row is not None and pd.notna(row.get("straddle_pct")) else None,
            })
        with open("/home/ibgateway/tickers_meta.json", "w") as f:
            json.dump(meta, f)
        logger.info(f"Wrote {len(meta)} entries to tickers_meta.json")

        print("\n=== FINAL INTERESTING SYMBOLS ===")
        print(final_list)
        logger.info(f"Wrote {len(final_list)} symbols to tickers.txt")

    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)
    finally:
        if ib.isConnected():
            ib.disconnect()
            logger.info("\n🔌 Disconnected from IBKR")

if __name__ == "__main__":
    main()
