# -*- coding: utf-8 -*-
"""
seed_brain.py — One-Time Historical Intel Seeder

Reads ALL existing intel files and feeds them into local_brain.json
so the bots start with a fully-primed knowledge base instead of zero.

Sources consumed:
  1. sleeper_intel.json       — 622 real trades tagged by sector (70.9% win rate)
  2. backtest_comparison.json — 90-day strategy-level win rates
  3. PaperTrade_Journal.csv   — Any paper trades already logged
  4. historical_gainers.json  — Which tickers were top gainers on which days
  5. tags.json                — Tag definitions (logged as metadata, not trade outcomes)
"""

import sys, os, json, csv
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from local_intel_engine import record_trade_outcome, _load_brain, _save_brain, BRAIN_PATH, _build_pattern_key

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

total_seeded = 0

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 1: sleeper_intel.json
# 622 trades across 9 sectors. We know win/loss counts per sector.
# We'll seed synthetic outcomes matching the exact win rates.
# ─────────────────────────────────────────────────────────────────────────────
print("\n[1/5] Seeding from sleeper_intel.json...")
sleeper_path = os.path.join(SCRIPTS_DIR, "sleeper_intel.json")
if os.path.exists(sleeper_path):
    try:
        with open(sleeper_path, encoding='utf-8') as f:
            data = json.load(f)

        sector_intel = data.get("intel_tags", {})

        # Sector → typical price range and vol characteristics for pattern bucketing
        sector_profiles = {
            "Technology":             {"price": 8.0,  "vol": 600000, "avg_vol": 150000, "roc": 2.0, "vwap": 7.5},
            "Healthcare":             {"price": 5.0,  "vol": 400000, "avg_vol": 100000, "roc": 1.8, "vwap": 4.8},
            "Basic Materials":        {"price": 3.0,  "vol": 300000, "avg_vol": 80000,  "roc": 1.5, "vwap": 2.9},
            "Communication Services": {"price": 4.0,  "vol": 350000, "avg_vol": 90000,  "roc": 2.5, "vwap": 3.8},
            "Financial Services":     {"price": 6.0,  "vol": 250000, "avg_vol": 70000,  "roc": 1.2, "vwap": 5.8},
            "Consumer Cyclical":      {"price": 7.0,  "vol": 280000, "avg_vol": 75000,  "roc": 1.0, "vwap": 6.7},
            "Industrials":            {"price": 9.0,  "vol": 200000, "avg_vol": 60000,  "roc": 0.9, "vwap": 8.7},
            "Real Estate":            {"price": 12.0, "vol": 150000, "avg_vol": 50000,  "roc": 0.8, "vwap": 11.5},
            "Utilities":              {"price": 5.5,  "vol": 180000, "avg_vol": 55000,  "roc": 0.7, "vwap": 5.3},
        }

        for sector, stats in sector_intel.items():
            wins  = stats.get("wins", 0)
            total = stats.get("total", 0)
            losses = total - wins
            profile = sector_profiles.get(sector, {"price": 5.0, "vol": 300000, "avg_vol": 100000, "roc": 1.5, "vwap": 4.8})
            tickers = stats.get("winning_tickers", [])[:3] + stats.get("losing_tickers", [])[:2]
            sample_ticker = tickers[0] if tickers else sector[:4].upper()

            # Inject wins
            for _ in range(wins):
                record_trade_outcome(
                    "STANDARD", profile["price"], profile["vol"], profile["avg_vol"],
                    profile["roc"], profile["vwap"],
                    pnl=+8.50,  # Typical sleeper win
                    ticker=sample_ticker
                )
                total_seeded += 1

            # Inject losses
            for _ in range(losses):
                record_trade_outcome(
                    "STANDARD", profile["price"], profile["vol"], profile["avg_vol"],
                    profile["roc"], profile["vwap"],
                    pnl=-4.00,  # Typical sleeper loss
                    ticker=sample_ticker
                )
                total_seeded += 1

        print(f"  Seeded {data.get('total_trades', 0)} trades from sleeper_intel.json")
    except Exception as e:
        print(f"  WARN: {e}")
else:
    print("  sleeper_intel.json not found — skipping")

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 2: backtest_comparison.json — Strategy-level aggregates
# Inject these as high-confidence pattern overrides directly into the brain.
# ─────────────────────────────────────────────────────────────────────────────
print("\n[2/5] Seeding from backtest_comparison.json...")
bt_path = os.path.join(SCRIPTS_DIR, "backtest_comparison.json")
if os.path.exists(bt_path):
    try:
        with open(bt_path, encoding='utf-8') as f:
            bt_data = json.load(f)

        results = bt_data.get("results", [])
        brain = _load_brain()

        for r in results:
            strategy_name = r.get("strategy", "")
            win_rate = r.get("win_rate", 0) / 100.0
            total_bt = 200  # Treat each backtest strategy as 200 representative samples
            wins_bt = int(total_bt * win_rate)
            losses_bt = total_bt - wins_bt
            avg_pnl = r.get("yield", 0) * 5.0  # yield% × avg position = rough $/trade

            # Map strategy name to internal key
            strat = "STANDARD" if "Standard" in strategy_name else "AGGRESSIVE"

            # Inject as a general mid-session ABOVE_VWAP pattern (most common setup)
            key = _build_pattern_key(strat, 6.0, 4.0, 1.5, True, 10)  # Morning mid session
            if key not in brain:
                brain[key] = {"total": 0, "wins": 0, "total_pnl": 0.0, "avg_pnl": 0.0,
                              "win_rate": 0.0, "last_seen": "2026-05-11", "last_decision": "APPROVED",
                              "sample_tickers": ["BACKTEST"]}

            entry = brain[key]
            entry["total"] += total_bt
            entry["wins"] += wins_bt
            entry["total_pnl"] = round(entry["total_pnl"] + (avg_pnl * total_bt), 2)
            entry["avg_pnl"] = round(entry["total_pnl"] / entry["total"], 4)
            entry["win_rate"] = round(entry["wins"] / entry["total"], 4)
            entry["last_seen"] = "2026-05-11 (90-day backtest)"
            if "BACKTEST" not in entry["sample_tickers"]:
                entry["sample_tickers"].append("BACKTEST")
            brain[key] = entry
            total_seeded += total_bt
            print(f"  {strategy_name}: {win_rate*100:.1f}% win rate → {total_bt} samples injected")

        _save_brain(brain)
        best = bt_data.get("best_strategy", {})
        print(f"  Best strategy per 90-day backtest: {best.get('strategy')} ({best.get('win_rate')}% win rate)")
    except Exception as e:
        print(f"  WARN: {e}")
else:
    print("  backtest_comparison.json not found — skipping")

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 3: PaperTrade_Journal.csv — Real paper trades already executed
# ─────────────────────────────────────────────────────────────────────────────
print("\n[3/5] Seeding from PaperTrade_Journal.csv...")
journal_path = os.path.join(SCRIPTS_DIR, "PaperTrade_Journal.csv")
if os.path.exists(journal_path):
    try:
        with open(journal_path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        closed = [r for r in rows if r.get("Status", "").upper() == "CLOSED"]
        for row in closed:
            try:
                ticker   = row.get("Ticker", "UNK")
                strategy = row.get("Strategy", "STANDARD")
                price    = float(row.get("Execution_Price") or row.get("Expected_Price") or 0)
                pnl      = float(row.get("PnL") or 0)
                if price <= 0:
                    continue
                # Use sensible defaults — we don't have vol/roc per row in the CSV
                record_trade_outcome(strategy, price, 400000, 100000, 1.5, price * 0.98, pnl=pnl, ticker=ticker)
                total_seeded += 1
            except Exception:
                pass
        print(f"  Seeded {len(closed)} closed paper trades")
    except Exception as e:
        print(f"  WARN: {e}")
else:
    print("  PaperTrade_Journal.csv not found — skipping")

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 4: historical_gainers.json — Tag recent top-gainer lists
# These aren't trade outcomes, but we use them to add tickers to the brain
# as confirmed "high-probability morning candidates" for that day's pattern.
# ─────────────────────────────────────────────────────────────────────────────
print("\n[4/5] Reading historical_gainers.json for context...")
gainers_path = os.path.join(SCRIPTS_DIR, "historical_gainers.json")
if os.path.exists(gainers_path):
    try:
        with open(gainers_path, encoding='utf-8') as f:
            gainers_data = json.load(f)
        all_gainers = []
        for date, tickers in gainers_data.items():
            all_gainers.extend(tickers)
        unique = list(set(all_gainers))
        print(f"  Found {len(unique)} unique top-gainer tickers across {len(gainers_data)} days")
        print(f"  These tickers are already being used by the scanner — no direct seeding needed")
        print(f"  Sample: {', '.join(unique[:10])}")
    except Exception as e:
        print(f"  WARN: {e}")
else:
    print("  historical_gainers.json not found — skipping")

# ─────────────────────────────────────────────────────────────────────────────
# SOURCE 5: Print current brain state
# ─────────────────────────────────────────────────────────────────────────────
print("\n[5/5] Final brain state after seeding...")
brain = _load_brain()
print(f"  Total patterns tracked: {len(brain)}")
total_samples = sum(v.get('total', 0) for v in brain.values())
print(f"  Total samples in brain : {total_samples}")
for key, data in sorted(brain.items(), key=lambda x: x[1].get('total', 0), reverse=True)[:5]:
    wr = data.get('win_rate', 0) * 100
    status = "APPROVE" if data.get('win_rate', 0) >= 0.60 else ("REJECT" if data.get('win_rate', 0) <= 0.30 else "UNCERTAIN")
    print(f"  [{status}] {key}")
    print(f"           Samples: {data['total']} | Win Rate: {wr:.0f}% | Avg PnL: ${data.get('avg_pnl', 0):.2f}")

print(f"\nDONE — {total_seeded} total samples seeded into brain.")
print(f"Brain saved to: {BRAIN_PATH}")
