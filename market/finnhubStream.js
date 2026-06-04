// market/finnhubStream.js
// ─────────────────────────────────────────────────────────────────────────────
// Price feed — reads from QuickTradeScanner's live WS cache via /api/price
//
// WHY: The scanner already has a Finnhub WebSocket connection maintaining
// real-time prices for every subscribed symbol. Opening a SECOND Finnhub WS
// from this backend burns the shared API key quota (300/min REST + WS limits),
// triggering 429s on both services and the 24-hour ban.
//
// The scanner exposes /api/price?sym=AAPL which returns the live WS price with
// ZERO additional Finnhub calls. We poll that endpoint instead.
// ─────────────────────────────────────────────────────────────────────────────

const SCANNER_URL = process.env.SCANNER_URL || "https://quicktradescanner-production.up.railway.app";
const POLL_INTERVAL_MS = 2000;   // poll scanner every 2s — matches extension's quote refresh rate

let subscribedSymbols  = [];
let onMsgCallback      = null;
let pollTimer          = null;
let _started           = false;

function start(key, onMessages) {
  // key is unused — we no longer talk to Finnhub directly
  onMsgCallback = onMessages;
  console.log(`[PriceFeed] Using QuickTradeScanner price cache — ${SCANNER_URL}/api/price`);
  _startPolling();
}

function _startPolling() {
  if (pollTimer) return;   // already running
  _poll();
}

async function _poll() {
  if (subscribedSymbols.length > 0) {
    for (const sym of subscribedSymbols) {
      try {
        const fetch   = (await import("node-fetch")).default;
        const res     = await fetch(`${SCANNER_URL}/api/price?sym=${sym}`, { signal: AbortSignal.timeout(3000) });
        const data    = await res.json();
        const price   = data.price || 0;
        if (price > 0 && onMsgCallback) {
          // Emit in the same format the rest of the backend expects
          onMsgCallback([{
            T:    "t",
            S:    sym,
            p:    price,
            s:    0,
            t:    new Date().toISOString(),
            bid:  data.bid   || price,
            ask:  data.ask   || price,
            chg:  data.chg   || 0,
          }]);
        }
      } catch (e) {
        // silent — scanner may be restarting, will retry next poll
      }
    }
  }
  pollTimer = setTimeout(_poll, POLL_INTERVAL_MS);
}

function subscribe(symbols) {
  subscribedSymbols = [...new Set([...subscribedSymbols, ...symbols])];
  // No WS frames to send — scanner already has these subscribed
}

// REST quote — also proxied through scanner to avoid Finnhub REST calls from backend
async function fetchQuote(symbol) {
  try {
    const fetch = (await import("node-fetch")).default;
    const res   = await fetch(`${SCANNER_URL}/api/price?sym=${symbol}`, { signal: AbortSignal.timeout(5000) });
    const data  = await res.json();
    const price = data.price || 0;
    return {
      T:         "q",
      S:         symbol,
      bp:        data.bid   || price - 0.01,
      ap:        data.ask   || price + 0.01,
      bs:        100,
      as:        100,
      price,
      changePct: data.chg   || 0,
    };
  } catch (e) {
    return null;
  }
}

module.exports = { start, subscribe, fetchQuote };
