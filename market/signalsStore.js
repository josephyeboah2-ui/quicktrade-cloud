// market/signalsStore.js
// In-memory state store for real-time signal engine

const stateBySymbol = Object.create(null);
let watchlist = [];

function setWatchlist(list) {
  watchlist = (Array.isArray(list) ? list : [])
    .map(s => String(s || "").toUpperCase().trim())
    .filter(Boolean);
  return watchlist;
}

function getWatchlist() {
  return watchlist.slice();
}

function ensure(symbol) {
  const s = String(symbol || "").toUpperCase().trim();
  if (!s) return null;
  if (!stateBySymbol[s]) {
    stateBySymbol[s] = {
      symbol: s,
      last: null,
      bid: null,
      ask: null,
      bidSize: null,
      askSize: null,

      // VWAP
      vwap: null,
      vwapPV: 0,
      vwapVol: 0,

      // Relative volume
      lastBarVols: [],
      rvol: null,

      // Bid/Ask flow tracking (rolling window)
      quoteHistory: [],       // last N quotes: { bid, ask, bidSize, askSize, ts }
      bidAskRatio: null,      // rolling avg bid/ask size ratio (>1 = buyers dominate)
      spreadPct: null,        // spread as % of midpoint

      // Combined signal output
      signal: "WAITING",      // BUY | SELL | HOLD | WAITING
      confidence: 0,          // 0-100
      reason: "Collecting market data…",

      // Legacy fields
      signalState: "IDLE",
      instruction: "",
      updatedAt: null,
    };
  }
  return stateBySymbol[s];
}

function patch(symbol, patchObj) {
  const st = ensure(symbol);
  if (!st) return null;
  Object.assign(st, patchObj, { updatedAt: new Date().toISOString() });
  return st;
}

function get(symbol) {
  return stateBySymbol[String(symbol || "").toUpperCase()] || null;
}

function getAll() {
  return Object.values(stateBySymbol);
}

module.exports = {
  setWatchlist,
  getWatchlist,
  ensure,
  patch,
  get,
  getAll,
};
