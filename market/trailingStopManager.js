// market/trailingStopManager.js
// Manages trailing stops by monitoring Finnhub quotes and updating stop orders

const { fetchQuote } = require("./finnhubStream");

// Active trailing stops: { symbol -> { qty, trailPct, entryPrice, highWaterMark, currentStopPrice, stopOrderId } }
const activeStops = new Map();

let placeEquityOrderFn = null; 
let pollIntervalId = null;

function isRegularMarketHours() {
  try {
    const nowStr = new Date().toLocaleString("en-US", {timeZone: "America/New_York"});
    const now = new Date(nowStr);
    
    const day = now.getDay();
    if (day === 0 || day === 6) return false; // Weekend
    
    const hours = now.getHours();
    const minutes = now.getMinutes();
    
    // Market is 9:30 to 16:00
    const timeInMinutes = hours * 60 + minutes;
    const openTime = 9 * 60 + 30; // 570
    const closeTime = 16 * 60;    // 960
    
    return timeInMinutes >= openTime && timeInMinutes < closeTime;
  } catch (e) {
    return true; // Fallback to market orders if timezone fails
  }
}

function init({ placeEquityOrder }) {
  placeEquityOrderFn = placeEquityOrder;

  // Poll every 1 second for faster synthetic stop execution
  if (pollIntervalId) clearInterval(pollIntervalId);
  pollIntervalId = setInterval(checkAndAdjust, 1000);
  console.log("[TrailingStop] Synthetic Manager started — polling every 1s");
}

function register(symbol, qty, trailPct, entryPrice, accountId) {
  const stopPrice = +(entryPrice * (1 - trailPct / 100)).toFixed(2);

  activeStops.set(symbol, {
    qty,
    trailPct,
    entryPrice,
    accountId, // Store account ID for execution
    highWaterMark: entryPrice,
    currentStopPrice: stopPrice,
    lastUpdated: Date.now(),
  });

  console.log(`[TrailingStop] Synthetic Registered ${symbol}: trail=${trailPct}%, entry=$${entryPrice}, initial stop=$${stopPrice}, account=${accountId}`);

  return { ok: true, symbol, stopPrice, trailPct };
}

function deregister(symbol) {
  activeStops.delete(symbol);
  console.log(`[TrailingStop] Synthetic Deregistered ${symbol}`);
  return { ok: true };
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
        const newStopPrice = +(currentPrice * (1 - entry.trailPct / 100)).toFixed(2);
        
        if (newStopPrice > entry.currentStopPrice) {
          entry.currentStopPrice = newStopPrice;
          console.log(`[TrailingStop] ${symbol}: price=$${currentPrice} (new high), moving internal stop to $${newStopPrice}`);
        }
      }

      // Has price triggered the stop?
      if (currentPrice <= entry.currentStopPrice) {
        const isMarketOpen = isRegularMarketHours();
        
        let orderType = "Market";
        let finalLimitPrice = null;

        if (isMarketOpen) {
           console.log(`[TrailingStop] TRIGGERED for ${symbol} at $${currentPrice}! Firing Market Sell...`);
        } else {
           // Extended Hours Strict Limit (0% Buffer)
           finalLimitPrice = +(currentPrice).toFixed(2);
           orderType = "Limit";
           console.log(`[TrailingStop] TRIGGERED for ${symbol} at $${currentPrice} (Extended Hours)! Firing Strict Limit Sell @ $${finalLimitPrice}...`);
        }

        // Immediately deregister to prevent double execution
        activeStops.delete(symbol);

        if (placeEquityOrderFn) {
          await placeEquityOrderFn({
            action: "SELL",
            symbol: symbol,
            qty: entry.qty,
            orderType: orderType,
            limitPrice: finalLimitPrice,
            stopPrice: null,
            accountId: entry.accountId
          }).then(res => {
            console.log(`[TrailingStop] Synthetic SELL placed successfully for ${symbol}`);
          }).catch(err => {
            console.error(`[TrailingStop] Failed to execute synthetic sell for ${symbol}:`, err.message);
          });
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
