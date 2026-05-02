// market/finnhubStream.js
// Real-time market data via Finnhub WebSocket + REST polling
const WebSocket = require("ws");

const WS_URL = "wss://ws.finnhub.io";
let ws;
let apiKey = "";
let subscribedSymbols = [];
let onMsgCallback = null;
let reconnectTimer = null;

function start(key, onMessages) {
  apiKey = key;
  onMsgCallback = onMessages;
  connect();
}

function connect() {
  if (reconnectTimer) clearTimeout(reconnectTimer);

  ws = new WebSocket(`${WS_URL}?token=${apiKey}`);

  ws.on("open", () => {
    console.log("[Finnhub WS] Connected");
    // Re-subscribe all symbols
    subscribedSymbols.forEach(s => {
      ws.send(JSON.stringify({ type: "subscribe", symbol: s }));
    });
  });

  ws.on("message", (raw) => {
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.type === "trade" && Array.isArray(msg.data)) {
        // Finnhub sends: { type: "trade", data: [{ s: symbol, p: price, v: volume, t: timestamp }] }
        const converted = msg.data.map(d => ({
          T: "t",          // trade
          S: d.s,          // symbol
          p: d.p,          // price
          s: d.v,          // size/volume
          t: new Date(d.t).toISOString(),
        }));
        if (onMsgCallback) onMsgCallback(converted);
      }
    } catch (e) {
      // ignore parse errors
    }
  });

  ws.on("close", () => {
    console.log("[Finnhub WS] Disconnected, reconnecting in 3s...");
    reconnectTimer = setTimeout(connect, 3000);
  });

  ws.on("error", (err) => {
    console.warn("[Finnhub WS] Error:", err.message);
  });
}

function subscribe(symbols) {
  const newSymbols = symbols.filter(s => !subscribedSymbols.includes(s));
  subscribedSymbols = [...new Set([...subscribedSymbols, ...symbols])];

  if (ws && ws.readyState === WebSocket.OPEN) {
    newSymbols.forEach(s => {
      ws.send(JSON.stringify({ type: "subscribe", symbol: s }));
    });
  }
}

// REST quote fetch for bid/ask data (Finnhub WS only sends trades)
async function fetchQuote(symbol) {
  try {
    const fetch = (await import("node-fetch")).default;
    const url = `https://finnhub.io/api/v1/quote?symbol=${symbol}&token=${apiKey}`;
    const res = await fetch(url);
    const data = await res.json();
    // c=current, h=high, l=low, o=open, pc=previous close, d=change, dp=change%
    return {
      T: "q",
      S: symbol,
      bp: data.c - 0.01,  // simulated bid (current - 1 cent)
      ap: data.c + 0.01,  // simulated ask (current + 1 cent)
      bs: 100,
      as: 100,
      price: data.c,
      high: data.h,
      low: data.l,
      open: data.o,
      prevClose: data.pc,
      change: data.d,
      changePct: data.dp,
    };
  } catch (e) {
    return null;
  }
}

module.exports = { start, subscribe, fetchQuote };
