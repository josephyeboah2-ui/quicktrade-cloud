// full_order_test.js — comprehensive test of symbol resolver + order flow logic
// Tests the exact tickers that were failing, against live SnapTrade data
require("dotenv").config();
const { Snaptrade } = require("snaptrade-typescript-sdk");

const snaptrade = new Snaptrade({
  clientId:    process.env.SNAPTRADE_CLIENT_ID,
  consumerKey: process.env.SNAPTRADE_CONSUMER_KEY,
});

const EXCHANGE_PRIORITY = [
  "NASDAQ", "NYSE", "AMEX", "BATS", "ARCA", "IEX",
  "TSX", "TSXV", "CSE", "NEO", "CNSX",
  "OTCM", "OTC", "OTCBB", "PINK",
];

const quoteSymbolCache = new Map();

async function resolveQuoteSymbolId(rawSymbol) {
  const upper = String(rawSymbol || "").toUpperCase().trim();
  if (!upper) throw new Error("Missing symbol");
  if (quoteSymbolCache.has(upper)) return quoteSymbolCache.get(upper);

  const searchBase = upper.split(".")[0];
  const forcedRegion = upper.endsWith(".TO") || upper.endsWith(".V") ? "CA"
                     : upper.endsWith(".US")                         ? "US"
                     : null;

  const resp = await snaptrade.referenceData.getSymbols({ substring: searchBase });
  const list = resp.data || [];
  if (!Array.isArray(list) || list.length === 0)
    throw new Error(`No SnapTrade symbol found for ${upper}`);

  const exact = list.filter(s =>
    s && s.raw_symbol && String(s.raw_symbol).toUpperCase() === searchBase
  );
  const pool = exact.length > 0 ? exact : list;
  const getExch = s => String(s?.exchange?.code || s?.exchange || "").toUpperCase();

  let candidates = pool;
  if (forcedRegion === "US") candidates = pool.filter(s => ["NASDAQ","NYSE","AMEX","BATS","ARCA","IEX"].includes(getExch(s)));
  else if (forcedRegion === "CA") candidates = pool.filter(s => ["TSX","TSXV","CSE","NEO","CNSX"].includes(getExch(s)));
  if (candidates.length === 0) candidates = pool;

  let best = null;
  for (const exch of EXCHANGE_PRIORITY) {
    best = candidates.find(s => getExch(s) === exch);
    if (best) break;
  }
  if (!best) best = candidates[0] || pool[0];
  if (!best?.id) throw new Error(`Symbol ID missing for ${upper}`);

  quoteSymbolCache.set(upper, best.id);
  return { id: best.id, exch: getExch(best), sym: best.symbol, totalMatches: pool.length };
}

// Expected results based on probe output
const EXPECTED = {
  SDOT:  { exch: "NASDAQ", id: "57d77ba8-349f-4493-ac3c-fa1977d1733d" },
  XOS:   { exch: "NASDAQ", id: "4676e456-e762-4a57-b618-d2672e2d766a" },
  SELX:  { exch: "NASDAQ", id: "b79f9750-877e-4219-8d82-01edb3d65866" },
  AAPL:  { exch: "NASDAQ", id: "c15a817e-7171-4940-9ae7-f7b4a95408ee" },
  TSLA:  { exch: "NASDAQ", id: "a7ceb2ae-2b3f-4246-b153-8c15292330e5" },
  NVDA:  { exch: "NASDAQ", id: "f2d10516-9aff-4338-b983-8db42a8bce91" },
  WCT:   { exch: "NASDAQ", id: "f41aa48b-b94d-43f1-8d08-dfe31e6fd6b7" },
  BJDX:  { exch: "NASDAQ", id: "b54b5917-ff64-4470-8f3f-e83874409531" },
  SILO:  { exch: "NASDAQ", id: "250357fb-a1f8-4764-bed0-0ecf2c9cc3b8" },
  ANY:   { exch: "NASDAQ", id: "5a398fc3-4509-49bb-bef7-e0b56b6ea87e" },
  CWBHF: { exch: "OTCM",   id: "3048313d-84bc-4d51-b08a-b351416b0ef6" },
};

async function run() {
  console.log("=== Full Symbol Resolver Test ===\n");
  let pass = 0, fail = 0;

  for (const [sym, expected] of Object.entries(EXPECTED)) {
    try {
      const result = await resolveQuoteSymbolId(sym);
      const idOk   = result.id   === expected.id;
      const exchOk = result.exch === expected.exch;
      const ok     = idOk && exchOk;

      if (ok) {
        console.log(`[PASS] ${sym.padEnd(6)} → exch=${result.exch} id=${result.id} (${result.totalMatches} raw matches resolved correctly)`);
        pass++;
      } else {
        console.log(`[FAIL] ${sym.padEnd(6)} → got exch=${result.exch} id=${result.id}`);
        if (!exchOk) console.log(`         expected exch=${expected.exch}`);
        if (!idOk)   console.log(`         expected id=${expected.id}`);
        fail++;
      }
    } catch(e) {
      console.log(`[FAIL] ${sym.padEnd(6)} → ERROR: ${e.message}`);
      fail++;
    }
    await new Promise(r => setTimeout(r, 250));
  }

  console.log(`\n${pass}/${pass+fail} passed${fail ? " -- " + fail + " FAILURES" : " -- ALL CLEAR"}`);

  // Static code checks
  console.log("\n=== Static Code Checks ===\n");
  const fs  = require("fs");
  const srv = fs.readFileSync("./server.js", "utf8");
  const fin = fs.readFileSync("./market/finnhubStream.js", "utf8");

  const static_checks = [
    ["EXCHANGE_PRIORITY list in server.js",             srv.includes("EXCHANGE_PRIORITY")],
    ["NASDAQ first priority",                            srv.indexOf('"NASDAQ"') < srv.indexOf('"TSX"')],
    ["OTC/OTCM included for OTC tickers",               srv.includes('"OTCM"')],
    ["SymbolResolver logs chosen exchange",              srv.includes("[SymbolResolver]")],
    ["1011 fallback uses forcePayload (raw sym)",        srv.includes("forcePayload") && srv.includes("symbol.toUpperCase()")],
    ["1011 block NOT using universal_symbol_id payload", !srv.slice(srv.indexOf("1011 (ambiguous symbol)"), srv.indexOf("1011 (ambiguous symbol)") + 900).includes("placeForceOrder(payload)")],
    ["SnapTrade 429 retry with 5s delay",               srv.includes("setTimeout(r, 5000)")],
    ["finnhubStream - no competing Finnhub WS",         !fin.includes("wss://ws.finnhub.io")],
    ["finnhubStream - uses scanner price cache",        fin.includes("SCANNER_URL") && fin.includes("/api/price")],
    ["finnhubStream - no 24h sleep",                    !fin.includes("86400")],
  ];

  let sp = 0, sf = 0;
  static_checks.forEach(([name, ok]) => {
    console.log((ok ? "[PASS]" : "[FAIL]") + " " + name);
    ok ? sp++ : sf++;
  });

  console.log(`\n${sp}/${static_checks.length} static checks passed${sf ? " -- " + sf + " FAILURES" : " -- ALL CLEAR"}`);
  console.log(`\nOVERALL: ${pass+sp}/${pass+fail+static_checks.length} checks passed`);
  if (fail + sf === 0) console.log("✅ ALL CLEAR — ready to trade");
  else console.log(`❌ ${fail+sf} issues need fixing`);
}

run().catch(console.error);
