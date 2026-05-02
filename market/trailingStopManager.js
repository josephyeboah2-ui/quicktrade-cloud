// market/trailingStopManager.js
// Manages trailing stops by monitoring Finnhub quotes and updating stop orders

const { fetchQuote } = require("./finnhubStream");

// Active trailing stops: { symbol -> { qty, trailPct, entryPrice, highWaterMark, currentStopPrice, stopOrderId } }
const activeStops = new Map();

let orderPlaceFn = null;   // function(symbol, qty, stopPrice) -> orderId
let orderCancelFn = null;  // function(orderId)
let pollIntervalId = null;

function init({ placeStopOrder, cancelOrder }) {
  orderPlaceFn = placeStopOrder;
  orderCancelFn = cancelOrder;

  // Poll every 5 seconds
  if (pollIntervalId) clearInterval(pollIntervalId);
  pollIntervalId = setInterval(checkAndAdjust, 5000);
  console.log("[TrailingStop] Manager started — polling every 5s");
}

function register(symbol, qty, trailPct, entryPrice) {
  const stopPrice = +(entryPrice * (1 - trailPct / 100)).toFixed(2);

  activeStops.set(symbol, {
    qty,
    trailPct,
    entryPrice,
    highWaterMark: entryPrice,
    currentStopPrice: stopPrice,
    stopOrderId: null,
    lastUpdated: Date.now(),
  });

  console.log(`[TrailingStop] Registered ${symbol}: trail=${trailPct}%, entry=$${entryPrice}, initial stop=$${stopPrice}`);

  // Place initial stop order
  placeOrUpdateStop(symbol, qty, stopPrice);

  return { ok: true, symbol, stopPrice, trailPct };
}

function deregister(symbol) {
  const entry = activeStops.get(symbol);
  if (entry && entry.stopOrderId && orderCancelFn) {
    try { orderCancelFn(entry.stopOrderId); } catch (e) {}
  }
  activeStops.delete(symbol);
  console.log(`[TrailingStop] Deregistered ${symbol}`);
  return { ok: true };
}

async function placeOrUpdateStop(symbol, qty, newStopPrice) {
  const entry = activeStops.get(symbol);
  if (!entry) return;

  // Cancel existing stop order if any
  if (entry.stopOrderId && orderCancelFn) {
    try { await orderCancelFn(entry.stopOrderId); } catch (e) {}
  }

  // Place new stop order
  if (orderPlaceFn) {
    try {
      const orderId = await orderPlaceFn(symbol, qty, newStopPrice);
      entry.stopOrderId = orderId;
      entry.currentStopPrice = newStopPrice;
      entry.lastUpdated = Date.now();
      console.log(`[TrailingStop] ${symbol}: stop updated to $${newStopPrice} (order: ${orderId})`);
    } catch (e) {
      console.warn(`[TrailingStop] Failed to place stop for ${symbol}:`, e.message);
    }
  }
}

async function checkAndAdjust() {
  for (const [symbol, entry] of activeStops.entries()) {
    try {
      const quote = await fetchQuote(symbol);
      if (!quote || !quote.price || quote.price <= 0) continue;

      const currentPrice = quote.price;

      // Has price made a new high?
      if (currentPrice > entry.highWaterMark) {
        entry.highWaterMark = currentPrice;

        // Calculate new trailing stop price
        const newStopPrice = +(currentPrice * (1 - entry.trailPct / 100)).toFixed(2);

        // Only move stop UP (never down)
        if (newStopPrice > entry.currentStopPrice) {
          console.log(`[TrailingStop] ${symbol}: price=$${currentPrice} (new high), moving stop $${entry.currentStopPrice} → $${newStopPrice}`);
          await placeOrUpdateStop(symbol, entry.qty, newStopPrice);
        }
      }
    } catch (e) {
      // Silent fail — next poll will retry
    }
  }
}

function getActive() {
  const result = [];
  for (const [symbol, entry] of activeStops.entries()) {
    result.push({
      symbol,
      qty: entry.qty,
      trailPct: entry.trailPct,
      entryPrice: entry.entryPrice,
      highWaterMark: entry.highWaterMark,
      currentStopPrice: entry.currentStopPrice,
      stopOrderId: entry.stopOrderId,
      lastUpdated: entry.lastUpdated,
    });
  }
  return result;
}

module.exports = { init, register, deregister, getActive };
