// market/signalsEngine.js
// Real-time signal engine: VWAP + Bid/Ask Flow + Volume analysis
const { ensure, patch } = require("./signalsStore");

// --- Configuration ---
const RVOL_WINDOW = 20;        // bars for relative volume
const RVOL_TRIGGER = 3;        // RVOL threshold for "volume surge"
const QUOTE_WINDOW = 30;       // rolling quote history size
const BID_ASK_STRONG = 1.5;    // ratio for "strong" imbalance
const BID_ASK_MILD = 1.2;      // ratio for "mild" imbalance

// --- Helpers ---

function pushRollingVol(st, v) {
  if (!isFinite(v) || v < 0) return;
  st.lastBarVols.push(v);
  if (st.lastBarVols.length > RVOL_WINDOW) st.lastBarVols.shift();

  const avg =
    st.lastBarVols.reduce((a, b) => a + b, 0) / st.lastBarVols.length;
  st.rvol = avg > 0 ? v / avg : null;
}

function pushQuote(st, bp, ap, bs, as) {
  st.quoteHistory.push({
    bid: bp,
    ask: ap,
    bidSize: bs,
    askSize: as,
    ts: Date.now(),
  });
  if (st.quoteHistory.length > QUOTE_WINDOW) st.quoteHistory.shift();
}

function computeBidAskMetrics(st) {
  const quotes = st.quoteHistory;
  if (quotes.length < 5) return; // need minimum data

  // Rolling bid/ask SIZE ratio (higher = buyers dominating)
  let totalBid = 0;
  let totalAsk = 0;
  for (const q of quotes) {
    totalBid += q.bidSize || 0;
    totalAsk += q.askSize || 0;
  }
  st.bidAskRatio = totalAsk > 0 ? totalBid / totalAsk : null;

  // Spread as % of midpoint
  const lastQ = quotes[quotes.length - 1];
  if (lastQ.bid > 0 && lastQ.ask > 0) {
    const mid = (lastQ.bid + lastQ.ask) / 2;
    st.spreadPct = mid > 0 ? ((lastQ.ask - lastQ.bid) / mid) * 100 : null;
  }
}

// --- Combined Signal Logic ---

function computeSignal(st) {
  // Not enough data yet
  if (!isFinite(st.last) || !isFinite(st.vwap) || st.bidAskRatio === null) {
    st.signal = "WAITING";
    st.confidence = 0;
    st.reason = "Collecting market data…";
    return;
  }

  const aboveVwap = st.last >= st.vwap;
  const pctFromVwap = ((st.last - st.vwap) / st.vwap) * 100;
  const buyersDominate = st.bidAskRatio >= BID_ASK_MILD;
  const buyersStrong = st.bidAskRatio >= BID_ASK_STRONG;
  const sellersDominate = st.bidAskRatio <= (1 / BID_ASK_MILD);
  const sellersStrong = st.bidAskRatio <= (1 / BID_ASK_STRONG);
  const volumeSurge = st.rvol !== null && st.rvol >= RVOL_TRIGGER;
  const volumeAboveAvg = st.rvol !== null && st.rvol >= 1.2;
  const tightSpread = st.spreadPct !== null && st.spreadPct < 0.05;

  // --- Calculate confidence score (0-100) ---
  let confidence = 0;
  let signal = "HOLD";
  let reason = "";

  // === DIP BUY DETECTION (highest priority) ===
  // Price dipped below or near VWAP but buyers are stepping in
  if (!aboveVwap && buyersStrong) {
    signal = "BUY";
    reason = "Dip near support — buyers accumulating";
    // Confidence scoring
    confidence += 30; // near VWAP support
    confidence += 25; // strong buyer imbalance
    if (volumeAboveAvg) { confidence += 20; reason += ", volume confirming"; }
    if (tightSpread)    { confidence += 15; }
    if (volumeSurge)    { confidence += 10; reason += "!"; }

  // === STRONG BUY: Above VWAP + buyers in control ===
  } else if (aboveVwap && buyersStrong && volumeAboveAvg) {
    signal = "BUY";
    reason = "Above fair value, buyers in control";
    confidence += 25; // above VWAP
    confidence += 25; // strong buyers
    confidence += 20; // volume
    if (tightSpread)  { confidence += 15; }
    if (volumeSurge)  { confidence += 15; reason += ", volume surge"; }

  // === MILD BUY: Above VWAP + slight buyer edge ===
  } else if (aboveVwap && buyersDominate) {
    signal = "BUY";
    reason = "Above fair value, buyers edging out";
    confidence += 20;
    confidence += 15;
    if (volumeAboveAvg) { confidence += 10; }
    if (tightSpread)    { confidence += 10; }

  // === SELL SIGNAL: Below VWAP + sellers dominating ===
  } else if (!aboveVwap && sellersStrong) {
    signal = "SELL";
    reason = "Below fair value, sellers dominating";
    confidence += 25;
    confidence += 25;
    if (volumeAboveAvg) { confidence += 20; reason += ", heavy selling"; }
    if (volumeSurge)    { confidence += 10; }

  // === MILD SELL: Below VWAP + some selling pressure ===
  } else if (!aboveVwap && sellersDominate) {
    signal = "SELL";
    reason = "Below fair value, sellers in control";
    confidence += 20;
    confidence += 15;
    if (volumeAboveAvg) { confidence += 10; }

  // === HOLD: Balanced / no clear edge ===
  } else if (aboveVwap) {
    signal = "HOLD";
    reason = "Stable above fair value";
    confidence += 15;
    if (tightSpread) { confidence += 10; }

  } else {
    signal = "HOLD";
    reason = "Balanced — no clear edge";
    confidence += 10;
  }

  // Clamp confidence
  st.signal = signal;
  st.confidence = Math.min(100, Math.max(0, Math.round(confidence)));
  st.reason = reason;
}

// --- Legacy signal (kept for backward compat) ---

function computeLegacyScenario(st) {
  if (!isFinite(st.last) || !isFinite(st.vwap)) {
    st.signalState = "IDLE";
    st.instruction = "Waiting for VWAP data…";
    return;
  }
  if (st.last < st.vwap) {
    st.signalState = "ABORT";
    st.instruction = "Below VWAP — avoid longs.";
    return;
  }
  if (st.rvol >= RVOL_TRIGGER) {
    st.signalState = "EXECUTE";
    st.instruction = "Above VWAP + volume surge. Enter on pullback hold above VWAP.";
  } else {
    st.signalState = "READY";
    st.instruction = "Above VWAP. Watch for pullback + volume.";
  }
}

// --- Event Handlers (called from Alpaca WebSocket) ---

function onQuote(m) {
  const st = ensure(m.S);
  if (!st) return;

  const bp = Number(m.bp);  // bid price
  const ap = Number(m.ap);  // ask price
  const bs = Number(m.bs);  // bid size
  const as = Number(m.as);  // ask size

  // Update raw values
  patch(st.symbol, { bid: bp, ask: ap, bidSize: bs, askSize: as });

  // Track quote history and compute bid/ask metrics
  pushQuote(st, bp, ap, bs, as);
  computeBidAskMetrics(st);

  // Recompute combined signal on every quote
  computeSignal(st);

  // Patch the signal output
  patch(st.symbol, {
    bidAskRatio: st.bidAskRatio,
    spreadPct: st.spreadPct,
    signal: st.signal,
    confidence: st.confidence,
    reason: st.reason,
  });
}

function onTrade(m) {
  const st = ensure(m.S);
  if (!st) return;
  patch(st.symbol, { last: m.p });
}

function onBar(m) {
  const st = ensure(m.S);
  if (!st) return;

  const c = Number(m.c);
  const v = Number(m.v);
  if (!isFinite(c) || !isFinite(v)) return;

  st.vwapPV += c * v;
  st.vwapVol += v;
  st.vwap = st.vwapVol > 0 ? st.vwapPV / st.vwapVol : null;
  st.last = c;

  pushRollingVol(st, v);
  computeLegacyScenario(st);
  computeSignal(st);

  patch(st.symbol, {
    last: st.last,
    vwap: st.vwap,
    rvol: st.rvol,
    signalState: st.signalState,
    instruction: st.instruction,
    signal: st.signal,
    confidence: st.confidence,
    reason: st.reason,
  });
}

module.exports = { onQuote, onTrade, onBar };
