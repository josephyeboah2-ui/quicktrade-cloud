// probe_symbols.js — runs against LIVE SnapTrade to show exactly what
// SnapTrade returns for each ticker so we can build the correct resolver
require("dotenv").config();
const { Snaptrade } = require("snaptrade-typescript-sdk");

const snaptrade = new Snaptrade({
  clientId:    process.env.SNAPTRADE_CLIENT_ID,
  consumerKey: process.env.SNAPTRADE_CONSUMER_KEY,
});

const USER_ID     = process.env.SNAPTRADE_USER_ID;
const USER_SECRET = process.env.SNAPTRADE_USER_SECRET;

// Tickers that have failed orders + common ones
const TEST_SYMS = ["SDOT", "XOS", "SELX", "AAPL", "TSLA", "NVDA", "WCT", "BJDX", "SILO", "ANY", "CWBHF"];

async function probe() {
  console.log("=== SnapTrade Symbol Resolution Probe ===\n");

  for (const sym of TEST_SYMS) {
    try {
      const resp = await snaptrade.referenceData.getSymbols({ substring: sym });
      const list = (resp.data || []).filter(s =>
        s && s.raw_symbol && String(s.raw_symbol).toUpperCase() === sym.toUpperCase()
      );
      const all  = (resp.data || []).slice(0, 6); // top 6 results

      console.log(`\n── ${sym} ──`);
      if (list.length === 0) {
        console.log(`  ⚠ NO exact match. Top results:`);
        all.forEach(s => {
          const exch = s.exchange?.code || s.exchange || "?";
          console.log(`    id=${s.id}  raw=${s.raw_symbol}  sym=${s.symbol}  exch=${exch}  desc=${(s.description||"").slice(0,40)}`);
        });
      } else {
        list.forEach(s => {
          const exch = s.exchange?.code || s.exchange || "?";
          const flags = [];
          if (["NASDAQ","NYSE","AMEX","BATS"].includes(exch)) flags.push("🇺🇸 US");
          if (["TSX","TSXV","CSE"].includes(exch))            flags.push("🇨🇦 CA");
          console.log(`  ✅ id=${s.id}`);
          console.log(`     raw=${s.raw_symbol}  sym=${s.symbol}  exch=${exch}  ${flags.join(" ")}`);
          console.log(`     desc=${(s.description||"").slice(0,60)}`);
        });
        if (list.length > 1) console.log(`  ⚠ AMBIGUOUS — ${list.length} exact matches (causes 1011)`);
      }
    } catch(e) {
      console.log(`  ❌ ERROR: ${e.message}`);
    }
    await new Promise(r => setTimeout(r, 300)); // be polite
  }

  console.log("\n=== Done ===");
}

probe().catch(console.error);
