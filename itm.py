def scan_ticker(self, ticker, skip_skew_filter: bool = False):
    """
    /itm normal scan.
    Stage 1: lightweight chain (45d), check call skew at nearest strike below spot.
    Stage 2: full chain + APY calculation only if skew found.
    """
    # === MINIMAL DEFENSIVE FIX ===
    if isinstance(ticker, (list, tuple)):
        ticker = ticker[0] if ticker else None
    if not ticker:
        return [], {}

    results = []
    debug = Counter()
    ticker = ticker.upper()
    freq = self.ticker_freqs.get(ticker, "Q")

    # Stage 1 — call skew pre-filter
    if not skip_skew_filter:
        lite = self.schwab.get_option_chain_lite(ticker, days=DTE_MAX)
        if lite:
            spot_lite = lite.get("underlyingPrice") or 0.0
            if spot_lite > 0:
                call_map_lite = lite.get("callExpDateMap", {})
                put_map_lite = lite.get("putExpDateMap", {})
                if call_map_lite and put_map_lite:
                    if not _check_call_skew(call_map_lite, put_map_lite, spot_lite):
                        debug["skew_filtered"] += 1
                        return results, debug

    # Stage 2 — full chain fetch
    try:
        chain = self.schwab.get_option_chain(ticker)
    except Exception as e:
        logger.error(f"[{ticker}] option chain fetch failed: {e}")
        raise

    spot = chain.get("underlyingPrice")
    if not spot or spot <= 0:
        debug["no_spot"] += 1
        return results, debug

    call_map = chain.get("callExpDateMap", {})
    put_map = chain.get("putExpDateMap", {})
    if not call_map or not put_map:
        debug["empty_chain"] += 1
        return results, debug

    annual_div, last_ex_div, next_ex_div_date, short_int = \
        self._fetch_fundamentals(ticker, spot)
    htb = short_int >= HTB_SHORT_INT_THRESHOLD

    all_exp_dates = (set(k.split(":")[0] for k in call_map) &
                     set(k.split(":")[0] for k in put_map))
    min_locked = MIN_LOCKED_AFTER_COMM_PER_CONTRACT / 100.0

    call_key_map = {k.split(":")[0]: k for k in call_map}
    put_key_map = {k.split(":")[0]: k for k in put_map}

    for exp_date in sorted(all_exp_dates):
        try:
            exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
        except ValueError:
            continue
        dte = (exp_dt - datetime.utcnow()).days
        if dte < DTE_MIN or dte > DTE_MAX:
            debug["dte_out_of_range"] += 1
            continue

        ck = call_key_map.get(exp_date)
        pk = put_key_map.get(exp_date)
        if not ck or not pk:
            continue

        calls = call_map[ck]
        puts = put_map[pk]

        strikes_below = []
        for s in calls:
            try:
                fs = float(s)
                if fs < spot and s in puts:
                    strikes_below.append(fs)
            except ValueError:
                pass
        strikes_below.sort(reverse=True)
        strikes_below = strikes_below[:STRIKES_BELOW_SPOT]

        for strike in strikes_below:
            debug["candidates"] += 1
            strike_str = next(
                (s for s in calls if abs(float(s) - strike) < 0.001), None)
            if not strike_str or strike_str not in puts:
                continue

            call_opt = (calls.get(strike_str) or [{}])[0]
            put_opt = (puts.get(strike_str) or [{}])[0]

            if not _has_market(call_opt):
                debug["call_no_market"] += 1
                continue
            if not _has_market(put_opt):
                debug["put_no_market"] += 1
                continue
            if _oi(call_opt) < MIN_OI:
                debug["call_oi_low"] += 1
                continue
            if _oi(put_opt) < MIN_OI:
                debug["put_oi_low"] += 1
                continue
            if _spread_pct(call_opt) > MAX_SPREAD_PCT:
                debug["call_spread_wide"] += 1
                continue
            if _spread_pct(put_opt) > MAX_SPREAD_PCT:
                debug["put_spread_wide"] += 1
                continue

            call_credit_p = _sell_price(call_opt)
            put_cost_p = _buy_price(put_opt)
            net_credit_p, locked_p, locked_total_p, apy_p = \
                _locked_and_apy(spot, strike, call_credit_p, put_cost_p, dte)

            if locked_p < min_locked:
                debug["below_min_locked_after_comm"] += 1
                continue

            call_credit_f = _sell_price(call_opt, extra_frac=FALLBACK_STEP_FRAC)
            put_cost_f = _buy_price(put_opt, extra_frac=FALLBACK_STEP_FRAC)
            _, locked_f, locked_total_f, apy_f = \
                _locked_and_apy(spot, strike, call_credit_f, put_cost_f, dte)

            primary_debit = spot - call_credit_p + put_cost_p
            fallback_debit = spot - call_credit_f + put_cost_f
            div_yield_pct = (annual_div / spot * 100.0) if spot > 0 else 0.0
            num_ex_divs = _project_ex_div_dates(last_ex_div, freq, exp_dt)
            in_window = _ex_div_before_expiry(next_ex_div_date, exp_date)

            debug["passed"] += 1
            results.append(dict(
                ticker=ticker, exp_date=exp_date, dte=dte,
                spot=round(spot, 2), strike=strike,
                call_credit=round(call_credit_p, 2),
                put_cost=round(put_cost_p, 2),
                net_credit=round(net_credit_p, 2),
                gap=round(spot - strike, 2),
                locked_profit=round(locked_p, 4),
                locked_total=round(locked_total_p, 2),
                locked_apy=round(apy_p, 1),
                primary_debit=round(primary_debit, 2),
                fallback_debit=round(fallback_debit, 2),
                fallback_locked_total=round(locked_total_f, 2),
                fallback_apy=round(apy_f, 1),
                cost_basis=round(spot - net_credit_p, 2),
                annual_div=round(annual_div, 2),
                div_yield_pct=round(div_yield_pct, 2),
                num_ex_divs=num_ex_divs,
                next_ex_div_date=next_ex_div_date,
                ex_div_in_window=in_window,
                short_int=round(short_int, 4),
                htb=htb,
                freq=freq,
                call_oi=_oi(call_opt), put_oi=_oi(put_opt),
                call_bid=_bid(call_opt), call_ask=_ask(call_opt),
                put_bid=_bid(put_opt), put_ask=_ask(put_opt),
                reverse=False,
                borrow_cost=0.0,
            ))

    return results, debug


def scan_ticker_reverse(self, ticker, skip_skew_filter: bool = False):
    """
    /itm r: strikes ABOVE spot where put_mid > call_mid.
    Stage 1: lightweight chain (15d), check put skew at nearest strike above spot.
    Stage 2: full chain + APY calculation only if skew found.
    Short stock + sell put + buy call.
    Borrow cost: 20% APR × spot × dte/365 deducted from locked profit.
    Ex-div in window: flagged + 25 APY point sort penalty (you PAY dividend).
    """
    # === MINIMAL DEFENSIVE FIX ===
    if isinstance(ticker, (list, tuple)):
        ticker = ticker[0] if ticker else None
    if not ticker:
        return [], {}

    results = []
    debug = Counter()
    ticker = ticker.upper()
    freq = self.ticker_freqs.get(ticker, "Q")

    # Stage 1 — put skew pre-filter
    if not skip_skew_filter:
        lite = self.schwab.get_option_chain_lite(ticker, days=REVERSE_DTE_MAX)
        if lite:
            spot_lite = lite.get("underlyingPrice") or 0.0
            if spot_lite > 0:
                call_map_lite = lite.get("callExpDateMap", {})
                put_map_lite = lite.get("putExpDateMap", {})
                if call_map_lite and put_map_lite:
                    if not _check_put_skew(call_map_lite, put_map_lite, spot_lite):
                        debug["skew_filtered"] += 1
                        return results, debug

    # Stage 2 — full chain fetch
    try:
        chain = self.schwab.get_option_chain(ticker)
    except Exception as e:
        logger.error(f"[{ticker}] reverse scan chain fetch failed: {e}")
        raise

    spot = chain.get("underlyingPrice")
    if not spot or spot <= 0:
        debug["no_spot"] += 1
        return results, debug

    call_map = chain.get("callExpDateMap", {})
    put_map = chain.get("putExpDateMap", {})
    if not call_map or not put_map:
        debug["empty_chain"] += 1
        return results, debug

    annual_div, last_ex_div, next_ex_div_date, short_int = \
        self._fetch_fundamentals(ticker, spot)
    htb = short_int >= HTB_SHORT_INT_THRESHOLD

    all_exp_dates = (set(k.split(":")[0] for k in call_map) &
                     set(k.split(":")[0] for k in put_map))
    min_locked = MIN_LOCKED_AFTER_COMM_PER_CONTRACT / 100.0

    call_key_map = {k.split(":")[0]: k for k in call_map}
    put_key_map = {k.split(":")[0]: k for k in put_map}

    for exp_date in sorted(all_exp_dates):
        try:
            exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
        except ValueError:
            continue
        dte = (exp_dt - datetime.utcnow()).days
        if dte < DTE_MIN or dte > REVERSE_DTE_MAX:
            debug["dte_out_of_range"] += 1
            continue

        ck = call_key_map.get(exp_date)
        pk = put_key_map.get(exp_date)
        if not ck or not pk:
            continue

        calls = call_map[ck]
        puts = put_map[pk]

        borrow_cost = spot * REVERSE_BORROW_RATE * (dte / 365.0)

        strikes_above = []
        for s in calls:
            try:
                fs = float(s)
                if fs > spot and s in puts:
                    strikes_above.append(fs)
            except ValueError:
                pass
        strikes_above.sort()
        strikes_above = strikes_above[:STRIKES_ABOVE_SPOT_REVERSE]

        for strike in strikes_above:
            debug["candidates"] += 1
            strike_str = next(
                (s for s in calls if abs(float(s) - strike) < 0.001), None)
            if not strike_str or strike_str not in puts:
                continue

            call_opt = (calls.get(strike_str) or [{}])[0]
            put_opt = (puts.get(strike_str) or [{}])[0]

            if not _has_market(call_opt):
                debug["call_no_market"] += 1
                continue
            if not _has_market(put_opt):
                debug["put_no_market"] += 1
                continue
            if _oi(call_opt) < MIN_OI:
                debug["call_oi_low"] += 1
                continue
            if _oi(put_opt) < MIN_OI:
                debug["put_oi_low"] += 1
                continue
            if _spread_pct(call_opt) > MAX_SPREAD_PCT:
                debug["call_spread_wide"] += 1
                continue
            if _spread_pct(put_opt) > MAX_SPREAD_PCT:
                debug["put_spread_wide"] += 1
                continue

            put_mid = (_bid(put_opt) + _ask(put_opt)) / 2.0
            call_mid = (_bid(call_opt) + _ask(call_opt)) / 2.0
            if put_mid <= call_mid:
                debug["no_put_skew"] += 1
                continue

            put_credit_p = _sell_price(put_opt)
            call_cost_p = _buy_price(call_opt)
            net_credit_p = put_credit_p - call_cost_p
            gap = strike - spot
            commission_per_share = COMMISSION_PER_CONTRACT / 100.0

            in_window = _ex_div_before_expiry(next_ex_div_date, exp_date)
            div_cost = 0.0
            if in_window and annual_div > 0:
                cycles = {"M": 12, "Q": 4, "S": 2, "A": 1, "W": 52}.get(freq, 4)
                div_cost = annual_div / cycles

            locked_p = (net_credit_p - gap - commission_per_share
                        - borrow_cost - div_cost)

            if locked_p < min_locked:
                debug["below_min_locked_after_comm"] += 1
                continue

            apy_p = (locked_p / spot) * (365.0 / dte) * 100.0 if spot > 0 and dte > 0 else 0.0

            put_credit_f = _sell_price(put_opt, extra_frac=FALLBACK_STEP_FRAC)
            call_cost_f = _buy_price(call_opt, extra_frac=FALLBACK_STEP_FRAC)
            net_credit_f = put_credit_f - call_cost_f
            locked_f = (net_credit_f - gap - commission_per_share
                        - borrow_cost - div_cost)
            apy_f = (locked_f / spot) * (365.0 / dte) * 100.0 if locked_f > 0 and spot > 0 and dte > 0 else 0.0

            div_yield_pct = (annual_div / spot * 100.0) if spot > 0 else 0.0
            num_ex_divs = _project_ex_div_dates(last_ex_div, freq, exp_dt)

            debug["passed"] += 1
            results.append(dict(
                ticker=ticker, exp_date=exp_date, dte=dte,
                spot=round(spot, 2), strike=strike,
                call_credit=round(call_cost_p, 2),
                put_cost=round(put_credit_p, 2),
                net_credit=round(net_credit_p, 2),
                gap=round(gap, 2),
                locked_profit=round(locked_p, 4),
                locked_total=round(locked_p * 100, 2),
                locked_apy=round(apy_p, 1),
                primary_debit=round(spot - net_credit_p, 2),
                fallback_debit=round(spot - net_credit_f, 2),
                fallback_locked_total=round(locked_f * 100, 2),
                fallback_apy=round(apy_f, 1),
                cost_basis=round(spot, 2),
                annual_div=round(annual_div, 2),
                div_yield_pct=round(div_yield_pct, 2),
                num_ex_divs=num_ex_divs,
                next_ex_div_date=next_ex_div_date,
                ex_div_in_window=in_window,
                short_int=round(short_int, 4),
                htb=htb,
                freq=freq,
                call_oi=_oi(call_opt), put_oi=_oi(put_opt),
                call_bid=_bid(call_opt), call_ask=_ask(call_opt),
                put_bid=_bid(put_opt), put_ask=_ask(put_opt),
                reverse=True,
                borrow_cost=round(borrow_cost, 4),
                div_cost=round(div_cost, 4),
            ))

    return results, debug
