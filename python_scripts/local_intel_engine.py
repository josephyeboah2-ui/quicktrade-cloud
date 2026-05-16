"""
local_intel_engine.py — QuickTrade Local Learning Brain

Replaces or supplements Gemini API calls by learning from every trade the bot executes.
Instead of calling Google every time, the bot builds a local statistical database that
gets smarter with every scan, backtest, and live session.

Schema (local_brain.json):
{
  "<pattern_key>": {
    "total": 10,
    "wins": 7,
    "total_pnl": 45.23,
    "avg_pnl": 4.52,
    "win_rate": 0.70,
    "last_seen": "2026-05-15 14:59:40",
    "last_decision": "APPROVED",
    "sample_tickers": ["BNZI", "SOUN"]
  }
}
"""

import json
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(line_buffering=True)

BRAIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_playbook", "local_brain.json")

# Minimum samples before the engine trusts its own data
MIN_SAMPLES_TO_TRUST = 5
# Minimum win rate to auto-APPROVE without Gemini
APPROVE_WIN_RATE = 0.60
# Maximum win rate threshold below which to auto-REJECT without Gemini
REJECT_WIN_RATE = 0.30
# Minimum avg PnL (per trade in $) to approve
MIN_AVG_PNL = 0.0


def _load_brain():
    """Load the local brain JSON file, or return empty dict if not found."""
    if os.path.exists(BRAIN_PATH):
        try:
            with open(BRAIN_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_brain(brain):
    """Persist the brain back to disk."""
    os.makedirs(os.path.dirname(BRAIN_PATH), exist_ok=True)
    with open(BRAIN_PATH, "w", encoding="utf-8") as f:
        json.dump(brain, f, indent=2)


def _build_pattern_key(strategy, price, vol_ratio, roc, above_vwap, hour):
    """
    Convert trade conditions into a stable, human-readable pattern key.
    Bucketing keeps patterns general enough to accumulate samples quickly.
    """
    # Price bucket
    if price < 1.0:
        price_bucket = "PENNY_SUB1"
    elif price < 5.0:
        price_bucket = "PENNY_1TO5"
    elif price < 15.0:
        price_bucket = "LOW_CAP_5TO15"
    else:
        price_bucket = "MID_CAP_15PLUS"

    # Volume ratio bucket
    if vol_ratio >= 10.0:
        vol_bucket = "VOL_10X_PLUS"
    elif vol_ratio >= 5.0:
        vol_bucket = "VOL_5X"
    elif vol_ratio >= 3.0:
        vol_bucket = "VOL_3X"
    elif vol_ratio >= 2.0:
        vol_bucket = "VOL_2X"
    else:
        vol_bucket = "VOL_NORMAL"

    # ROC bucket
    if roc >= 3.0:
        roc_bucket = "ROC_HIGH"
    elif roc >= 1.0:
        roc_bucket = "ROC_MED"
    elif roc >= 0.0:
        roc_bucket = "ROC_FLAT"
    else:
        roc_bucket = "ROC_NEG"

    # Time of day bucket
    if 9 <= hour < 10:
        time_bucket = "MORNING_OPEN"
    elif 10 <= hour < 12:
        time_bucket = "MORNING_MID"
    elif 12 <= hour < 14:
        time_bucket = "MIDDAY"
    elif 14 <= hour < 15:
        time_bucket = "AFTERNOON"
    else:
        time_bucket = "POWER_HOUR"

    vwap_bucket = "ABOVE_VWAP" if above_vwap else "BELOW_VWAP"

    return f"{strategy}__{price_bucket}__{vol_bucket}__{roc_bucket}__{time_bucket}__{vwap_bucket}"


def query_local_intel(strategy, price, current_vol, avg_vol, roc, vwap, ticker=""):
    """
    Query the local brain for a buy/reject signal based on accumulated trade history.

    Returns:
        dict with keys:
            "decision"  : "APPROVED" | "REJECTED" | "UNCERTAIN"
            "confidence": float (0.0 – 1.0)
            "reasoning" : str
            "samples"   : int
    """
    vol_ratio = (current_vol / avg_vol) if avg_vol and avg_vol > 0 else 1.0
    above_vwap = price > vwap if vwap and vwap > 0 else True
    hour = datetime.now().hour

    key = _build_pattern_key(strategy, price, vol_ratio, roc, above_vwap, hour)
    brain = _load_brain()

    if key not in brain:
        return {
            "decision": "UNCERTAIN",
            "confidence": 0.0,
            "reasoning": f"No local data yet for pattern: {key}",
            "samples": 0,
            "pattern_key": key
        }

    entry = brain[key]
    total = entry.get("total", 0)

    if total < MIN_SAMPLES_TO_TRUST:
        return {
            "decision": "UNCERTAIN",
            "confidence": total / MIN_SAMPLES_TO_TRUST,
            "reasoning": f"Only {total}/{MIN_SAMPLES_TO_TRUST} samples collected. Need more data.",
            "samples": total,
            "pattern_key": key
        }

    win_rate = entry.get("win_rate", 0.0)
    avg_pnl = entry.get("avg_pnl", 0.0)

    if win_rate >= APPROVE_WIN_RATE and avg_pnl >= MIN_AVG_PNL:
        decision = "APPROVED"
        reasoning = (
            f"Local brain APPROVED: {win_rate*100:.0f}% win rate, "
            f"avg PnL ${avg_pnl:.2f} over {total} trades. Pattern: {key}"
        )
    elif win_rate <= REJECT_WIN_RATE:
        decision = "REJECTED"
        reasoning = (
            f"Local brain REJECTED: only {win_rate*100:.0f}% win rate, "
            f"avg PnL ${avg_pnl:.2f} over {total} trades. Pattern: {key}"
        )
    else:
        decision = "UNCERTAIN"
        reasoning = (
            f"Local brain UNCERTAIN: {win_rate*100:.0f}% win rate, "
            f"avg PnL ${avg_pnl:.2f} over {total} trades. Deferring to Gemini."
        )

    return {
        "decision": decision,
        "confidence": win_rate,
        "reasoning": reasoning,
        "samples": total,
        "pattern_key": key,
        "win_rate": win_rate,
        "avg_pnl": avg_pnl
    }


def record_trade_outcome(strategy, price, current_vol, avg_vol, roc, vwap, pnl, ticker=""):
    """
    Called after a trade closes. Updates the local brain with the outcome.
    This is the core of the learning loop.

    Args:
        pnl: The realized profit/loss in dollars for this trade.
    """
    vol_ratio = (current_vol / avg_vol) if avg_vol and avg_vol > 0 else 1.0
    above_vwap = price > vwap if vwap and vwap > 0 else True
    # Use the hour of the ENTRY not the close - approximate with current hour
    hour = datetime.now().hour

    key = _build_pattern_key(strategy, price, vol_ratio, roc, above_vwap, hour)
    brain = _load_brain()

    if key not in brain:
        brain[key] = {
            "total": 0,
            "wins": 0,
            "total_pnl": 0.0,
            "avg_pnl": 0.0,
            "win_rate": 0.0,
            "last_seen": "",
            "last_decision": "APPROVED",
            "sample_tickers": []
        }

    entry = brain[key]
    entry["total"] += 1
    if pnl > 0:
        entry["wins"] += 1
    entry["total_pnl"] = round(entry["total_pnl"] + pnl, 4)
    entry["avg_pnl"] = round(entry["total_pnl"] / entry["total"], 4)
    entry["win_rate"] = round(entry["wins"] / entry["total"], 4)
    entry["last_seen"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Track sample tickers (up to 10, most recent)
    tickers_seen = entry.get("sample_tickers", [])
    if ticker and ticker not in tickers_seen:
        tickers_seen.append(ticker)
        if len(tickers_seen) > 10:
            tickers_seen = tickers_seen[-10:]
    entry["sample_tickers"] = tickers_seen

    brain[key] = entry
    _save_brain(brain)

    outcome = "WIN ✅" if pnl > 0 else "LOSS ❌"
    print(
        f"[LOCAL BRAIN] Recorded {outcome} | {ticker} | PnL: ${pnl:.2f} | "
        f"Pattern: {key} | Win Rate now: {entry['win_rate']*100:.0f}% ({entry['total']} samples)"
    )


def get_brain_summary():
    """Returns a human-readable summary of the local brain's top patterns."""
    brain = _load_brain()
    if not brain:
        return "Local brain is empty. Run some trades first to build intelligence!"

    lines = [f"\n{'='*60}", "  📊 LOCAL BRAIN SUMMARY", f"{'='*60}"]
    sorted_patterns = sorted(brain.items(), key=lambda x: x[1].get("total", 0), reverse=True)

    for key, data in sorted_patterns[:15]:
        total = data.get("total", 0)
        if total < 1:
            continue
        win_rate = data.get("win_rate", 0) * 100
        avg_pnl = data.get("avg_pnl", 0)
        tickers = ", ".join(data.get("sample_tickers", [])[:3])
        status = "✅ APPROVE" if data.get("win_rate", 0) >= APPROVE_WIN_RATE else ("❌ REJECT" if data.get("win_rate", 0) <= REJECT_WIN_RATE else "❓ UNCERTAIN")
        lines.append(
            f"  {status} | {key}\n"
            f"         Samples: {total} | Win Rate: {win_rate:.0f}% | Avg PnL: ${avg_pnl:.2f} | Tickers: {tickers}"
        )

    lines.append(f"{'='*60}\n")
    return "\n".join(lines)


if __name__ == "__main__":
    # Quick test
    print("Testing local intel engine...")
    # Simulate recording 6 winning trades on a MORNING_OPEN BNZI-style setup
    for i in range(6):
        record_trade_outcome("AGGRESSIVE", 8.03, 500000, 100000, 2.5, 7.8, pnl=12.50, ticker="BNZI")
    # And 2 losses
    for i in range(2):
        record_trade_outcome("AGGRESSIVE", 8.03, 500000, 100000, 2.5, 7.8, pnl=-5.00, ticker="BNZI")

    result = query_local_intel("AGGRESSIVE", 8.03, 500000, 100000, 2.5, 7.8, ticker="BNZI")
    print(f"\nQuery Result: {json.dumps(result, indent=2)}")
    print(get_brain_summary())
