// market/alpacaStream.js
const WebSocket = require("ws");

const FEED = process.env.ALPACA_FEED || "iex";
const WS_URL = `wss://stream.data.alpaca.markets/v2/${FEED}`;

let ws;
let authed = false;
let pending = [];

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

function start(onMessages) {
  ws = new WebSocket(WS_URL);

  ws.on("open", () => {
    send({
      action: "auth",
      key: process.env.ALPACA_API_KEY,
      secret: process.env.ALPACA_API_SECRET,
    });
  });

  ws.on("message", (raw) => {
    const msgs = JSON.parse(raw.toString());
    const list = Array.isArray(msgs) ? msgs : [msgs];

    for (const m of list) {
      if (m?.T === "success") {
        authed = true;
        if (pending.length) subscribe(pending);
      }
    }
    onMessages(list);
  });

  ws.on("close", () => {
    authed = false;
    setTimeout(() => start(onMessages), 3000);
  });
}

function subscribe(symbols) {
  pending = symbols;
  if (!authed) return;

  send({
    action: "subscribe",
    trades: symbols,
    quotes: symbols,
    bars: symbols,
  });
}

module.exports = { start, subscribe };
