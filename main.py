"""Capitulation buyer for btc-race-24h (cbBTC/USDC, 1-minute bars).

The bot buys only after a capitulation: the close at least 7.5% below the
session high while RSI(14) is under 40. One open position, 95% of equity
per trade. It exits on a +3% take-profit, a -1.5% stop-loss, or after 120
bars held, and it stops opening new trades once drawdown reaches 20% of
the $1,000 start. It trades once or twice per window at most: the edge is
a positive expected value per trade, not frequency.

All decisions use only the bars received so far (no lookahead, no wall
clock, no network, no files outside the repo). Deterministic: the same
bars always produce the same orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
RSI_PERIOD = 14
DROP_BPS = 750            # entry: close >= 7.5% below the session high
RSI_MAX = 40.0            # entry: RSI(14) < 40
TP_BPS = 300              # take-profit: +3% from the fill price
SL_BPS = 150              # stop-loss: -1.5% from the fill price
MAX_HOLD = 120            # exit after 120 bars held
SIZE_BPS = 9500           # 95% of equity per trade
HALT_DD_UNITS = 200_000_000   # halt new entries after $200 drawdown (20% of $1,000)
MAX_SLIPPAGE_BPS = 2000
MIN_NOTIONAL = 1_000_000  # $1, the competition's minimum

# --- module state, rebuilt from the bars as they arrive -------------------
closes = []               # close prices (ints, price_scale 5), in order
session_high = 0          # highest high seen so far
entry_price = None        # fill price of the open position (None when flat)
entry_seq = -1            # batch seq at which the buy filled
peak_equity = 1_000_000_000
halted = False


def rsi_wilder(values, period):
    """RSI(period) on a list of closes, Wilder's smoothing.

    Returns None until period + 1 closes are known. The first average gain
    and loss are the simple means of the first `period` changes; after that
    each is smoothed as (prev * (period - 1) + change) / period.
    """
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(diff if diff > 0 else 0.0)
        losses.append(-diff if diff < 0 else 0.0)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def note_fills(state, batch):
    """Record our own fills from this batch (entry price / entry bar)."""
    global entry_price, entry_seq
    for fill in batch.get("fills", []):
        if fill.get("symbol") != SYMBOL or fill.get("status") != "filled":
            continue
        if fill.get("side") == "buy":
            entry_price = int(fill["price"])
            entry_seq = state.seq
        elif fill.get("side") == "sell":
            entry_price = None
            entry_seq = -1


def on_batch(state, batch):
    global session_high, peak_equity, halted
    if state.warming_up:
        return []
    bar = state.latest.get(SYMBOL)
    if bar is None:
        return []
    close = int(bar["c"])
    high = int(bar["h"])
    closes.append(close)
    if high > session_high:
        session_high = high
    if state.equity > peak_equity:
        peak_equity = state.equity
    note_fills(state, batch)

    position = state.positions.get(SYMBOL)
    holding = position is not None and position.qty > 0

    if holding:
        # --- exits: take-profit, stop-loss, max hold -------------------
        # Prefer the fill price; fall back to the position's cost-implied
        # average price (cost_quote_units over qty) if a fill was missed.
        price = entry_price
        if price is None and position.cost > 0 and position.qty > 0:
            price = position.cost * 10_000_000 // position.qty
        if price is not None:
            take_profit = price * (10000 + TP_BPS) // 10000
            stop_loss = price * (10000 - SL_BPS) // 10000
            held = state.seq - entry_seq if entry_seq >= 0 else 0
            if close >= take_profit or close <= stop_loss or held >= MAX_HOLD:
                return [{
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": position.qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }]
        return []

    # --- flat: halt check ----------------------------------------------
    if halted or (peak_equity - state.equity) >= HALT_DD_UNITS:
        halted = True
        return []

    # --- flat: entry ----------------------------------------------------
    if len(closes) < RSI_PERIOD + 1:
        return []
    rsi = rsi_wilder(closes, RSI_PERIOD)
    if rsi is None or rsi >= RSI_MAX:
        return []
    # close at least 7.5% below the session high
    if close * 10000 > session_high * (10000 - DROP_BPS):
        return []
    notional = state.equity * SIZE_BPS // 10000
    if notional < MIN_NOTIONAL:
        return []
    if notional > state.cash:
        notional = state.cash
    if notional < MIN_NOTIONAL:
        return []
    return [{
        "side": "buy",
        "symbol": SYMBOL,
        "notional_quote_units": notional,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }]


makefun_bars.run(on_batch)
