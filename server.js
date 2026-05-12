// server.js
// QuickTrade REAL MONEY backend using SnapTrade
// - Serves:
//     GET  /                               -> health check
//     GET  /api/quote/:symbol              -> real quote via SnapTrade
//     GET  /api/holdings                   -> REAL holdings + cash via SnapTrade
//     POST /api/order                      -> main endpoint used by panel/hotkeys
//     POST /api/trade                      -> legacy endpoint
//     GET  /api/snaptrade/connect-portal   -> connection portal (no broker hard-coded)
//     GET  /api/snaptrade/connect-wealthsimple -> same as connect-portal (alias)
//     GET  /api/snaptrade/accounts         -> list linked user accounts (not implemented here)
//     GET  /api/market-clock               -> exchange session + recommended order type

require("dotenv").config({ path: require("path").join(__dirname, ".env") });

// --- GLOBAL ERROR HANDLERS (must be first!) ---
process.on("unhandledRejection", (reason) => {
  console.error("[QuickTrade] Unhandled Promise Rejection (server stays alive):", reason?.message || reason);
});
process.on("uncaughtException", (err) => {
  console.error("[QuickTrade] Uncaught Exception (server stays alive):", err?.message || err);
});

const express = require("express");
const bodyParser = require("body-parser");
const cors = require("cors");
const { Snaptrade } = require("snaptrade-typescript-sdk");

// -------- Finnhub Market Data (signals + quotes) --------
const { start: startFinnhubWS, subscribe: finnhubSubscribe, fetchQuote } = require("./market/finnhubStream");
const { onQuote, onTrade, onBar } = require("./market/signalsEngine");
const { makeSignalsRouter } = require("./market/signals.routes");
const { setWatchlist, getWatchlist } = require("./market/signalsStore");
const trailingStopMgr = require("./market/trailingStopManager");

const FINNHUB_KEY = process.env.Finnhub_KEY || "";


// ---------------- ENV ----------------

const CLIENT_ID = process.env.SNAPTRADE_CLIENT_ID;
const CONSUMER_KEY = process.env.SNAPTRADE_CONSUMER_KEY;
const USER_ID = process.env.SNAPTRADE_USER_ID;
const USER_SECRET = process.env.SNAPTRADE_USER_SECRET;
const ACCOUNT_ID = process.env.SNAPTRADE_ACCOUNT_ID;
const BROKERAGE_AUTH_ID = process.env.SNAPTRADE_BROKERAGE_AUTH_ID;

console.log("=== QuickTrade SnapTrade ENV CHECK ===");
console.log("SNAPTRADE_CLIENT_ID:", CLIENT_ID ? "OK" : "MISSING");
console.log("SNAPTRADE_CONSUMER_KEY:", CONSUMER_KEY ? "OK" : "MISSING");
console.log("SNAPTRADE_USER_ID:", USER_ID ? "OK" : "MISSING");
console.log("SNAPTRADE_USER_SECRET:", USER_SECRET ? "OK" : "MISSING");
console.log("SNAPTRADE_ACCOUNT_ID:", ACCOUNT_ID ? "OK" : "MISSING");
console.log("SNAPTRADE_BROKERAGE_AUTH_ID:", BROKERAGE_AUTH_ID ? "OK" : "MISSING");
console.log("======================================");

// ------------- INIT SNAPTRADE CLIENT -------------

const snaptrade = new Snaptrade({
  clientId: CLIENT_ID,
  consumerKey: CONSUMER_KEY,
});

// ------------- EXPRESS SETUP -------------

const app = express();

app.use(
  cors({
    origin: "*",
    methods: ["GET", "POST"],
    allowedHeaders: ["Content-Type"],
  })
);

app.use(bodyParser.json());

// -------- Signals API (read-only, no trading) --------
app.use(
  makeSignalsRouter({
    onWatchlistChanged: (symbols) => finnhubSubscribe(symbols),
  })
);

// -------- Python Bot Spawner --------
const { spawn, exec } = require("child_process");
const path = require("path");
const fs = require("fs");

const activeBots = {};

function spawnPythonBot(scriptName, reqBody) {
  if (activeBots[scriptName]) {
    console.log(`[QuickTrade] Killing active ${scriptName}...`);
    activeBots[scriptName].kill();
  }
  const { tickers, maxSize, maxLoss, takeProfitPct, trailingStopPct, broker, accountId, strategy } = reqBody;
  
  const scriptPath = path.resolve(__dirname, `../QuickTradeExtension/backend/${scriptName}`);
  const args = [scriptPath];
  
  if (tickers) args.push("--tickers", tickers);
  if (maxSize) args.push("--max_size", maxSize.toString());
  if (maxLoss) args.push("--max_loss", maxLoss.toString());
  if (takeProfitPct) args.push("--take_profit_pct", takeProfitPct.toString());
  if (trailingStopPct) args.push("--trailing_stop_pct", trailingStopPct.toString());
  if (broker) args.push("--broker", broker);
  if (accountId) args.push("--account_id", accountId);
  if (strategy) args.push("--strategy", strategy);

  console.log(`\n[QuickTrade] Spawning Python Bot: ${scriptName}`);
  const pyProcess = spawn("python", args, {
    env: { ...process.env, PYTHONIOENCODING: "utf-8" }
  });

  activeBots[scriptName] = pyProcess;

  pyProcess.stdout.on("data", (data) => {
    process.stdout.write(data.toString());
  });

  pyProcess.stderr.on("data", (data) => {
    process.stderr.write(data.toString());
  });

  pyProcess.on("close", (code) => {
    console.log(`[QuickTrade] Python Bot (${scriptName}) exited with code ${code}`);
    delete activeBots[scriptName];
  });
}

function stopPythonBot(scriptName) {
  if (activeBots[scriptName]) {
    console.log(`[QuickTrade] Stopping Python Bot: ${scriptName}`);
    activeBots[scriptName].kill();
    delete activeBots[scriptName];
  }
}

app.get("/api/bots/status", (req, res) => {
  res.json({
    live: !!activeBots["live_trader.py"],
    paper: !!activeBots["paper_trader.py"]
  });
});


app.get("/api/history", (req, res) => {
    const { exec } = require("child_process");
    exec("python fetch_history.py", { cwd: __dirname }, (error, stdout, stderr) => {
        if (error) {
            console.error("Error fetching history:", error);
            return res.json([]);
        }
        try {
            const data = JSON.parse(stdout);
            res.json(data);
        } catch (e) {
            console.error("Error parsing history JSON:", e);
            res.json([]);
        }
    });
});

app.post("/api/live/start", (req, res) => {
  spawnPythonBot("live_trader.py", req.body);
  res.json({ ok: true, message: "Live bot started" });
});

app.post("/api/live/stop", (req, res) => {
  stopPythonBot("live_trader.py");
  res.json({ ok: true, message: "Live bot stopped" });
});

app.post("/api/paper/start", (req, res) => {
  spawnPythonBot("paper_trader.py", req.body);
  res.json({ ok: true, message: "Paper bot started" });
});

app.post("/api/paper/stop", (req, res) => {
  stopPythonBot("paper_trader.py");
  res.json({ ok: true, message: "Paper bot stopped" });
});
app.post("/api/backtest", (req, res) => {
  const { tickers, days, balance, riskPct, dailyQuota, strategy } = req.body;
  const scriptName = "backtester.py";
  const scriptPath = path.join(__dirname, "../QuickTradeExtension/backend", scriptName);

  let args = [];
  if (tickers) args.push("--tickers", `"${tickers}"`);
  if (days) args.push("--days", days);
  if (balance) args.push("--balance", balance);
  if (riskPct) args.push("--risk_pct", riskPct);
  if (strategy) args.push("--strategy", strategy);
  if (dailyQuota) args.push("--daily_quota", dailyQuota);

  const command = `python "${scriptPath}" ${args.join(" ")}`;
  console.log(`[QuickTrade] Running Backtest: ${command}`);

  exec(command, { maxBuffer: 1024 * 1024 * 10, env: { ...process.env, PYTHONIOENCODING: "utf-8" } }, (error, stdout, stderr) => {
    if (error) {
      console.error(`[QuickTrade] Backtest Error: ${error}`);
      return res.status(500).json({ ok: false, error: error.message });
    }
    
    try {
      // Find the JSON block in stdout
      const match = stdout.match(/===BACKTEST_RESULT===\r?\n([\s\S]+?)\r?\n===END_RESULT===/);
      if (match && match[1]) {
        const result = JSON.parse(match[1]);
        res.json({ ok: true, data: result });
      } else {
        res.status(500).json({ ok: false, error: "Invalid backtest output" });
      }
    } catch (err) {
      res.status(500).json({ ok: false, error: "Failed to parse backtest results" });
    }
  });
});


app.get("/api/history/:ticker", (req, res) => {
  const ticker = req.params.ticker;
  const period = req.query.period || "1mo";
  const scriptName = "fetch_history.py";
  const scriptPath = path.join(__dirname, "../QuickTradeExtension/backend", scriptName);

  const command = `python "${scriptPath}" --ticker ${ticker} --period ${period}`;
  
  exec(command, { maxBuffer: 1024 * 1024 * 2 }, (error, stdout, stderr) => {
    if (error) {
      return res.status(500).json({ ok: false, error: error.message });
    }
    try {
      const match = stdout.match(/===CHART_DATA===\r?\n([\s\S]+?)\r?\n===END_CHART_DATA===/);
      if (match && match[1]) {
        const result = JSON.parse(match[1]);
        res.json({ ok: true, data: result });
      } else {
        res.status(500).json({ ok: false, error: "Invalid chart output" });
      }
    } catch (err) {
      res.status(500).json({ ok: false, error: "Failed to parse chart data" });
    }
  });
});

// health check
app.get("/", (req, res) => {
  res.json({ ok: true, message: "QuickTrade SnapTrade backend alive" });
});

// ------------- TOP MOVERS (Gainers — ALL US stocks) -------------
app.get("/api/top-movers", async (req, res) => {
  try {
    const minGain = parseFloat(req.query.min || "3");
    const limit = parseInt(req.query.limit || "5");

    const fetch = (await import("node-fetch")).default;

    // Primary: Yahoo Finance screener (large-cap + small-cap gainers)
    try {
      const screeners = ["day_gainers", "small_cap_gainers"];
      const allQuotes = [];

      for (const scrId of screeners) {
        try {
          const yUrl = `https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=${scrId}&count=25`;
          const yResp = await fetch(yUrl, {
            headers: { "User-Agent": "Mozilla/5.0" },
            timeout: 8000,
          });
          const yData = await yResp.json();
          const quotes = yData?.finance?.result?.[0]?.quotes || [];
          allQuotes.push(...quotes);
        } catch {}
      }

      // Deduplicate by symbol
      const seen = new Set();
      const unique = allQuotes.filter(q => {
        if (seen.has(q.symbol)) return false;
        seen.add(q.symbol);
        return true;
      });

      if (unique.length > 0) {
        const gainers = unique
          .map(q => ({
            symbol: q.symbol,
            price: q.regularMarketPrice || 0,
            changePct: q.regularMarketChangePercent || 0,
            volume: q.regularMarketVolume || 0,
            name: q.shortName || q.symbol,
          }))
          .filter(g => g.changePct >= minGain && g.price > 0)
          .sort((a, b) => b.changePct - a.changePct)
          .slice(0, limit);

        return res.json({ ok: true, source: "yahoo", gainers });
      }
    } catch (yErr) {
      console.warn("[QuickTrade] Yahoo screener failed, trying Finnhub:", yErr.message);
    }

    // Fallback: Finnhub scan of popular symbols
    if (FINNHUB_KEY) {
      const FALLBACK = ["AAPL","TSLA","NVDA","AMZN","META","GOOGL","MSFT","AMD","PLTR","SOFI","COIN","MARA","RIOT","HOOD","GME","AMC","NIO","SNAP","SMCI","MSTR","MRNA","DKNG","LCID","ARM","NFLX"];
      const promises = FALLBACK.map(async (sym) => {
        try {
          const r = await fetch(`https://finnhub.io/api/v1/quote?symbol=${sym}&token=${FINNHUB_KEY}`);
          const d = await r.json();
          return { symbol: sym, price: d.c, changePct: d.dp || 0 };
        } catch { return null; }
      });
      const results = (await Promise.all(promises)).filter(Boolean);
      const gainers = results
        .filter(g => g.changePct >= minGain)
        .sort((a, b) => b.changePct - a.changePct)
        .slice(0, limit);
      return res.json({ ok: true, source: "finnhub", gainers });
    }

    res.json({ ok: true, source: "none", gainers: [] });
  } catch (err) {
    console.error("[QuickTrade] Top movers error:", err.message);
    res.json({ ok: true, source: "error", gainers: [], error: err.message });
  }
});

// ------------- TRAILING STOP ENDPOINTS -------------
// Initialize trailing stop manager
trailingStopMgr.init({
  placeEquityOrder: placeEquityOrder,
});

// POST /api/trailing-stop — register a trailing stop
app.post("/api/trailing-stop", (req, res) => {
  const { symbol, qty, trailPct, entryPrice, accountId } = req.body;
  if (!symbol || !qty || !trailPct || !entryPrice) {
    return res.status(400).json({ ok: false, error: "Missing symbol, qty, trailPct, or entryPrice" });
  }
  const result = trailingStopMgr.register(symbol, qty, trailPct, entryPrice, accountId);
  res.json(result);
});

// DELETE /api/trailing-stop/:symbol — deregister a trailing stop
app.delete("/api/trailing-stop/:symbol", (req, res) => {
  const result = trailingStopMgr.deregister(req.params.symbol.toUpperCase());
  res.json(result);
});

// GET /api/trailing-stops — list active trailing stops
app.get("/api/trailing-stops", (req, res) => {
  res.json({ ok: true, stops: trailingStopMgr.getActive() });
});

// ------------- LIST CONNECTIONS (BROKERAGE AUTHORIZATIONS) -------------
// ✅ Option B: backend endpoint your extension can call to see why trading is blocked
// Uses: GET https://api.snaptrade.com/api/v1/authorizations :contentReference[oaicite:1]{index=1}
app.get("/api/snaptrade/accounts", async (req, res) => {
  console.log("[QuickTrade] /api/snaptrade/accounts");

  try {
    if (!CLIENT_ID || !CONSUMER_KEY || !USER_ID || !USER_SECRET) {
      return res.status(500).json({
        ok: false,
        error: "Missing SNAPTRADE_CLIENT_ID / CONSUMER_KEY / USER_ID / USER_SECRET",
      });
    }

    // ✅ Correct SDK call:
    const resp = await snaptrade.connections.listBrokerageAuthorizations({
      userId: USER_ID,
      userSecret: USER_SECRET,
    });

    const list = resp.data || resp || [];

    // Keep response clean + useful for debugging in the extension
    const connections = (Array.isArray(list) ? list : []).map((c) => ({
      id: c.id,
      name: c.name,
      type: c.type, // "read" | "trade"
      disabled: !!c.disabled,
      disabled_date: c.disabled_date || null,
      brokerage: {
        id: c.brokerage?.id,
        slug: c.brokerage?.slug,
        name: c.brokerage?.name,
        display_name: c.brokerage?.display_name,
        maintenance_mode: !!c.brokerage?.maintenance_mode,
        allows_trading: c.brokerage?.allows_trading ?? null,
        enabled: c.brokerage?.enabled ?? null,
      },
    }));

    // Helpful summary flags
    const anyDisabled = connections.some((c) => c.disabled);
    const anyMaintenance = connections.some((c) => c.brokerage.maintenance_mode);

    return res.json({
      ok: true,
      anyDisabled,
      anyMaintenance,
      connections,
    });
  } catch (err) {
    const safeMessage = extractSafeError(err);
    return res.status(500).json({ ok: false, error: safeMessage });
  }
});

// ------------- LIST USER ACCOUNTS (Trading Accounts) -------------
app.get("/api/snaptrade/user-accounts", async (req, res) => {
  console.log("[QuickTrade] /api/snaptrade/user-accounts");

  try {
    if (!CLIENT_ID || !CONSUMER_KEY || !USER_ID || !USER_SECRET) {
      return res.status(500).json({
        ok: false,
        error: "Missing SNAPTRADE_CLIENT_ID / CONSUMER_KEY / USER_ID / USER_SECRET",
      });
    }

    const resp = await snaptrade.accountInformation.listUserAccounts({
      userId: USER_ID,
      userSecret: USER_SECRET,
    });

    const list = resp.data || resp || [];

    const accounts = (Array.isArray(list) ? list : []).map((a) => ({
      id: a.id,
      name: a.name,
      number: a.number,
      sync_status: a.sync_status?.status,
    }));

    return res.json({
      ok: true,
      accounts,
    });
  } catch (err) {
    const safeMessage = extractSafeError(err);
    return res.status(500).json({ ok: false, error: safeMessage });
  }
});

// ------------- HELPERS -------------

function ensureEnv() {
  if (!CLIENT_ID || !CONSUMER_KEY || !USER_ID || !USER_SECRET || !ACCOUNT_ID) {
    throw new Error(
      "Backend missing SnapTrade env vars. Check SNAPTRADE_* values in .env (including SNAPTRADE_ACCOUNT_ID)."
    );
  }
}

function mapOrderType(frontType) {
  const t = String(frontType || "").toLowerCase();
  if (t === "market") return "Market";
  if (t === "limit") return "Limit";
  if (t === "stop") return "Stop";
  if (t === "stop_limit" || t === "stoplimit") return "StopLimit";
  return "Market";
}

function extractSafeError(err) {
  console.error("[QuickTrade] RAW ERROR FROM SNAPTRADE:", err);

  // 🔍 Special handling for SnapTrade "inactive security" (code 1146)
  try {
    const body =
      err?.responseBody ||
      err?.body ||
      err?.response?.data ||
      null;

    const code = body?.code || body?.status_code || err?.code;
    const detail = body?.detail || err?.message || "";

    // If SnapTrade says the underlying security is inactive
    if (String(code) === "1146" || /inactive as of/i.test(detail || "")) {
      return (
        "QT_BACKEND_ERROR: This symbol is currently marked INACTIVE " +
        "on SnapTrade's side, so QuickTrade can't send live orders for it. " +
        "You may still be able to trade it directly in your broker's app. " +
        (detail ? ` (${detail}) (code 1146)` : " (code 1146)")
      );
    }

    // Generic SnapTrade error with detail
    if (body && detail) {
      let safeMessage = `QT_BACKEND_ERROR: ${detail}`;
      if (code) {
        safeMessage += ` (code ${code})`;
      }
      return safeMessage;
    }

    if (err && typeof err === "object") {
      if (err.message) return err.message;
    }
  } catch (e) {
    console.warn("[QuickTrade] extractSafeError secondary error:", e);
  }

  return "Unknown error while placing order.";
}
// ---------- MONEY FORMATTER ----------
function formatMoney(num) {
  const v = Number(num);
  if (!isFinite(v)) return 0;
  return Number(v.toFixed(2));
}


// ---------------- MARKET CLOCK (EASTERN TIME) ----------------

/**
 * Convert "now" to America/New_York and figure out:
 * - is regular session open
 * - is pre-market / post-market
 * - recommended order type for frontend (market vs limit)
 *
 * We assume US equities for now (NYSE/NASDAQ):
 *  - Regular: 09:30–16:00 ET
 *  - Pre:     08:00–09:30 ET
 *  - Post:    16:00–20:00 ET
 */
function getMarketClock() {
  const now = new Date();
  const nyString = now.toLocaleString("en-US", {
    timeZone: "America/New_York",
  });
  const nyNow = new Date(nyString);

  const day = nyNow.getDay(); // 0=Sun, 6=Sat
  const isWeekday = day >= 1 && day <= 5;

  const hours = nyNow.getHours();
  const minutes = nyNow.getMinutes();
  const totalMin = hours * 60 + minutes;

  const PRE_OPEN = 8 * 60; // 08:00
  const REG_OPEN = 9 * 60 + 30; // 09:30
  const REG_CLOSE = 16 * 60; // 16:00
  const POST_CLOSE = 20 * 60; // 20:00

  let session = "CLOSED";
  let isOpenRegular = false;
  let isExtended = false;

  if (isWeekday) {
    if (totalMin >= REG_OPEN && totalMin < REG_CLOSE) {
      session = "REGULAR";
      isOpenRegular = true;
    } else if (totalMin >= PRE_OPEN && totalMin < REG_OPEN) {
      session = "PRE";
      isExtended = true;
    } else if (totalMin >= REG_CLOSE && totalMin < POST_CLOSE) {
      session = "POST";
      isExtended = true;
    }
  }

  const isClosed = !isOpenRegular && !isExtended;

  // Frontend can use this to auto-toggle order type:
  //  - REGULAR  -> market + limit allowed
  //  - PRE/POST -> limit recommended
  const recommendedOrderType = isOpenRegular ? "market" : "limit";

  return {
    exchange: "US_EQUITIES",
    timeZone: "America/New_York",
    isoNow: nyNow.toISOString(),
    session, // "REGULAR" | "PRE" | "POST" | "CLOSED"
    isOpenRegular,
    isExtended,
    isClosed,
    recommendedOrderType,
  };
}

// For the overlay to poll and auto-switch the order type UI
app.get("/api/market-clock", (req, res) => {
  try {
    const clock = getMarketClock();
    res.json({ ok: true, clock });
  } catch (err) {
    console.error("[QuickTrade] /api/market-clock error:", err);
    res.status(500).json({ ok: false, error: "Failed to compute market clock." });
  }
});

// ---------------- SYMBOL RESOLVER (SMART VERSION USING SNAPTRADE) ----------------

/**
 * Resolve what we actually send to SnapTrade.
 *
 * Uses SnapTrade Reference Data "Search symbols" endpoint to find the proper
 * trading symbol (e.g. SIS → SIS.TO, VAB → VAB.TO, etc).
 */
const symbolCache = new Map();

// Resolve ticker -> SnapTrade SYMBOL ID (UUID) for quotes
const quoteSymbolCache = new Map();

async function resolveQuoteSymbolId(rawSymbol) {
  const upper = String(rawSymbol || "").toUpperCase().trim();
  if (!upper) throw new Error("Missing symbol");

  if (quoteSymbolCache.has(upper)) {
    return quoteSymbolCache.get(upper);
  }

  // Strip .US or .TO for the SnapTrade lookup, as getSymbols doesn't recognize them
  const searchBase = upper.split('.')[0];

  const resp = await snaptrade.referenceData.getSymbols({
    substring: searchBase,
  });

  const list = resp.data || [];

  if (!Array.isArray(list) || list.length === 0) {
    throw new Error(`No SnapTrade symbol found for ${upper}`);
  }

  // Prefer exact raw_symbol match on North American exchanges
  const validExchanges = ["NASDAQ", "NYSE", "AMEX", "BATS", "TSX", "TSXV", "CSE"];
  let best = list.find((s) => {
    if (!s || !s.raw_symbol) return false;
    const isMatch = String(s.raw_symbol).toUpperCase() === searchBase;
    const exchangeCode = s.exchange && s.exchange.code ? String(s.exchange.code).toUpperCase() : "";
    
    // If user explicitly typed .US or .TO, strictly enforce the exchange location
    if (upper.endsWith(".US")) {
      return isMatch && ["NASDAQ", "NYSE", "AMEX", "BATS"].includes(exchangeCode);
    } else if (upper.endsWith(".TO") || upper.endsWith(".V")) {
      return isMatch && ["TSX", "TSXV", "CSE"].includes(exchangeCode);
    }

    return isMatch && validExchanges.includes(exchangeCode);
  });

  if (!best) {
    best = list.find((s) => String(s.raw_symbol || "").toUpperCase() === searchBase);
  }
  if (!best) best = list[0];

  if (!best || !best.id) {
    throw new Error(`Symbol ID missing for ${upper}`);
  }

  quoteSymbolCache.set(upper, best.id);
  return best.id;
}

async function resolveEffectiveSymbol(rawSymbol) {
  if (!rawSymbol) {
    throw new Error("Missing symbol for resolution.");
  }

  const upper = String(rawSymbol).toUpperCase().trim();

  // If it already has a suffix (e.g. "SIS.TO", "AAPL", "AMZN"), just uppercase & use it.
  if (upper.includes(".")) {
    return upper;
  }

  if (symbolCache.has(upper)) {
    return symbolCache.get(upper);
  }

  try {
    const resp = await snaptrade.referenceData.getSymbols({
      substring: upper,
    });

    const list = resp.data || resp || [];

    if (Array.isArray(list) && list.length > 0) {
      // Prioritize North American exchanges since user trades stocks on Wealthsimple
      const validExchanges = ["NASDAQ", "NYSE", "AMEX", "BATS", "TSX", "TSXV", "CSE"];
      let best = list.find((s) => {
        if (!s || !s.raw_symbol) return false;
        const isMatch = String(s.raw_symbol).toUpperCase() === upper;
        const exchangeCode = s.exchange && s.exchange.code ? String(s.exchange.code).toUpperCase() : "";
        return isMatch && validExchanges.includes(exchangeCode);
      });

      // Fallback if no NA exchange match
      if (!best) {
        best = list.find((s) => {
          if (!s || !s.raw_symbol) return false;
          return String(s.raw_symbol).toUpperCase() === upper;
        });
      }

      if (!best) best = list[0];

      const eff =
        best && (best.symbol || best.raw_symbol)
          ? String(best.symbol || best.raw_symbol)
          : upper;

      const finalSymbol = eff.toUpperCase();
      symbolCache.set(upper, finalSymbol);

      console.log("[QuickTrade] Resolved symbol:", {
        input: rawSymbol,
        resolved: finalSymbol,
      });

      return finalSymbol;
    }

    console.warn(
      "[QuickTrade] resolveEffectiveSymbol: no matches from SnapTrade for",
      upper
    );
    return upper;
  } catch (e) {
    console.error(
      "[QuickTrade] resolveEffectiveSymbol error (falling back to raw):",
      e
    );
    return upper;
  }
}


// ---------------- placeEquityOrder ----------------

async function placeEquityOrder({
  action, // "buy" | "sell"
  symbol,
  qty,
  orderType, // "market" | "limit" | "stop" | "stop_limit"
  limitPrice,
  stopPrice,
  accountId,
}) {
  ensureEnv();

  if (!symbol || typeof symbol !== "string") {
    throw new Error("Missing or invalid symbol.");
  }

  if (!action || !["buy", "sell", "BUY", "SELL"].includes(String(action))) {
    throw new Error("Invalid action. Must be 'buy' or 'sell'.");
  }

  const units = Number(qty);
  if (!units || units <= 0) {
    throw new Error("Invalid share amount.");
  }

  const snapAction = String(action).toUpperCase() === "BUY" ? "BUY" : "SELL";
  const snapOrderType = mapOrderType(orderType);

  // 🔔 Market clock: block Market orders outside regular hours
  const clock = getMarketClock();
  if (!clock.isOpenRegular && snapOrderType === "Market") {
    throw new Error(
      "Market orders are only allowed during regular hours (09:30–16:00 ET). Switch to a Limit order."
    );
  }

  const isMarket = snapOrderType === "Market";

  const px =
    limitPrice !== undefined && limitPrice !== null
      ? Number(limitPrice)
      : null;
  const sp =
    stopPrice !== undefined && stopPrice !== null ? Number(stopPrice) : null;

  if (snapOrderType === "Limit" && (px === null || isNaN(px) || px <= 0)) {
    throw new Error("Limit order requires a valid limit price.");
  }
  if (snapOrderType === "Stop" && (sp === null || isNaN(sp) || sp <= 0)) {
    throw new Error("Stop order requires a valid stop price.");
  }
  if (snapOrderType === "StopLimit") {
    if (px === null || isNaN(px) || px <= 0) throw new Error("StopLimit requires a valid limit price.");
    if (sp === null || isNaN(sp) || sp <= 0) throw new Error("StopLimit requires a valid stop price.");
  }

  // Resolve symbol to UUID (automatically filtered for North American exchanges)
  const symbolId = await resolveQuoteSymbolId(symbol);

  // Simple mapping for trading_session:
  //  - REGULAR -> "REGULAR"
  //  - PRE/POST -> still "REGULAR" for now (SnapTrade will route if supported)
  const tradingSession = clock.isOpenRegular ? "REGULAR" : "REGULAR";

  const finalAccountId = accountId || ACCOUNT_ID;

  const payload = {
    userId: USER_ID,
    userSecret: USER_SECRET,
    account_id: finalAccountId,
    action: snapAction,
    universal_symbol_id: symbolId,
    order_type: snapOrderType,
    time_in_force: "Day",
    trading_session: tradingSession,
    units,
  };

  if (snapOrderType === "Limit" || snapOrderType === "StopLimit") {
    payload.price = px;
  }
  if (snapOrderType === "Stop" || snapOrderType === "StopLimit") {
    payload.stop = sp;
  }

  console.log("[QuickTrade] placeEquityOrder payload:", JSON.stringify(payload, null, 2));

  const orderResponse = await snaptrade.trading.placeForceOrder(payload);

  console.log(
    "[QuickTrade] SnapTrade order success:",
    orderResponse.data || orderResponse
  );

  return orderResponse.data || orderResponse;
}
// ------------- SNAPTRADE USER REGISTRATION -------------

app.post("/api/snaptrade/register-user", async (req, res) => {
  try {
    const userId = (req.body && req.body.userId) || USER_ID || "QTrader12345";
    console.log("[QuickTrade] Registering SnapTrade user:", userId);

    const response = await snaptrade.authentication.registerSnapTradeUser({
      userId: userId,
    });

    console.log("[QuickTrade] Register response:", response.data);

    res.json({
      ok: true,
      userId: response.data.userId || userId,
      userSecret: response.data.userSecret,
    });
  } catch (err) {
    console.error("[QuickTrade] Register error:", err.response?.data || err.message);
    const safeMessage = extractSafeError(err);
    res.status(500).json({ ok: false, error: safeMessage });
  }
});

app.post("/api/snaptrade/reset-user-secret", async (req, res) => {
  try {
    const userId = (req.body && req.body.userId) || USER_ID || "QTrader12345";
    const userSecret = (req.body && req.body.userSecret) || USER_SECRET;
    console.log("[QuickTrade] Resetting user secret for:", userId);

    const response = await snaptrade.authentication.resetSnapTradeUserSecret({
      userId: userId,
      userSecret: userSecret,
    });

    console.log("[QuickTrade] Reset secret response:", response.data);

    res.json({
      ok: true,
      userId: response.data.userId || userId,
      userSecret: response.data.userSecret,
    });
  } catch (err) {
    console.error("[QuickTrade] Reset secret error:", err.response?.data || err.message);
    const safeMessage = extractSafeError(err);
    res.status(500).json({ ok: false, error: safeMessage });
  }
});

// ------------- SNAPTRADE PORTAL (no broker hard-coded) -------------

app.get("/api/snaptrade/connect-webull", async (req, res) => {
  try {
    if (!CLIENT_ID || !CONSUMER_KEY || !USER_ID || !USER_SECRET) {
      return res.status(500).json({ ok: false, error: "Missing config" });
    }
    const response = await snaptrade.authentication.loginSnapTradeUser({
      userId: USER_ID,
      userSecret: USER_SECRET,
      connectionType: "trade"
    });
    console.log("[QuickTrade] Created Webull portal:", response.data);
    res.json({ ok: true, redirectURI: response.data.redirectURI, sessionId: response.data.sessionId });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.get("/api/snaptrade/connect-portal", async (req, res) => {
  try {
    if (!CLIENT_ID || !CONSUMER_KEY || !USER_ID || !USER_SECRET) {
      return res.status(500).json({
        ok: false,
        error:
          "Missing SNAPTRADE_CLIENT_ID / CONSUMER_KEY / USER_ID / USER_SECRET",
      });
    }

    // Allow version override via ?version=v3 or ?version=v4 for testing
    const portalVersion = req.query.version || "v3";

    const response = await snaptrade.authentication.loginSnapTradeUser({
      userId: USER_ID,
      userSecret: USER_SECRET,
      connectionType: "trade",
      broker: "WEALTHSIMPLETRADE",
    });

    console.log("[QuickTrade] Created connection portal:", response.data);

    res.json({
      ok: true,
      redirectURI: response.data.redirectURI,
      sessionId: response.data.sessionId,
    });
  } catch (err) {
    console.error(
      "[QuickTrade] Error creating connect link:",
      err.response?.data || err
    );
    const safeMessage = extractSafeError(err);
    res.status(500).json({ ok: false, error: safeMessage });
  }
});

// alias route specifically called by your frontend / manual tests
app.get("/api/snaptrade/connect-wealthsimple", async (req, res) => {
  try {
    if (!CLIENT_ID || !CONSUMER_KEY || !USER_ID || !USER_SECRET) {
      return res.status(500).json({
        ok: false,
        error:
          "Missing SNAPTRADE_CLIENT_ID / CONSUMER_KEY / USER_ID / USER_SECRET",
      });
    }

    if (!BROKERAGE_AUTH_ID) {
      return res.status(500).json({
        ok: false,
        error:
          "Missing SNAPTRADE_BROKERAGE_AUTH_ID in .env (use brokerage_authorization from /api/snaptrade/accounts)",
      });
    }

    const response = await snaptrade.authentication.loginSnapTradeUser({
      userId: USER_ID,
      userSecret: USER_SECRET,
      reconnect: BROKERAGE_AUTH_ID,
      connectionType: "trade",
      darkMode: true,
      showCloseButton: true,
      connectionPortalVersion: "v4",
    });

    console.log(
      "[QuickTrade] Created trading RECONNECT portal (wealthsimple):",
      response.data
    );

    res.json({
      ok: true,
      redirectURI: response.data.redirectURI,
      sessionId: response.data.sessionId,
    });
  } catch (err) {
    console.error(
      "[QuickTrade] Error creating wealthsimple reconnect link:",
      err.response?.data || err
    );
    const safeMessage = extractSafeError(err);
    res.status(500).json({ ok: false, error: safeMessage });
  }
});

// ------------- DELETE STALE CONNECTION -------------

app.delete("/api/snaptrade/connection", async (req, res) => {
  console.log("[QuickTrade] DELETE /api/snaptrade/connection");

  try {
    if (!CLIENT_ID || !CONSUMER_KEY || !USER_ID || !USER_SECRET) {
      return res.status(500).json({
        ok: false,
        error: "Missing SnapTrade credentials in .env",
      });
    }

    // Use the auth ID from the request body, or fall back to the .env value
    const authId = (req.body && req.body.authorizationId) || BROKERAGE_AUTH_ID;

    if (!authId) {
      return res.status(400).json({
        ok: false,
        error: "No authorizationId provided and SNAPTRADE_BROKERAGE_AUTH_ID not set.",
      });
    }

    await snaptrade.connections.removeBrokerageAuthorization({
      userId: USER_ID,
      userSecret: USER_SECRET,
      authorizationId: authId,
    });

    console.log("[QuickTrade] Deleted brokerage connection:", authId);

    res.json({ ok: true, deleted: authId });
  } catch (err) {
    console.error("[QuickTrade] Error deleting connection:", err.response?.data || err);
    const safeMessage = extractSafeError(err);
    res.status(500).json({ ok: false, error: safeMessage });
  }
});

// ------------- REAL HOLDINGS ENDPOINT -------------

app.get("/api/holdings", async (req, res) => {
  console.log("[QuickTrade] /api/holdings (REAL)");

  try {
    ensureEnv();

    // Pre-check: make sure we have an active connection before querying holdings
    try {
      const connResp = await snaptrade.connections.listBrokerageAuthorizations({
        userId: USER_ID,
        userSecret: USER_SECRET,
      });
      const connList = Array.isArray(connResp.data) ? connResp.data : [];
      const activeConns = connList.filter(c => !c.disabled);

      if (activeConns.length === 0) {
        return res.json({
          ok: false,
          connected: false,
          error: "No active brokerage connection. Click Connect to link your broker.",
        });
      }
    } catch (connErr) {
      console.warn("[QuickTrade] Could not check connections:", connErr.message);
      // Continue anyway — the holdings call will fail with a clear error if needed
    }

    const resp = await snaptrade.accountInformation.getUserHoldings({
      userId: USER_ID,
      userSecret: USER_SECRET,
      accountId: ACCOUNT_ID,
    });

    const data = resp.data || resp;

    const balances = data.balances || [];
    const positions = data.positions || [];
    const orders = data.orders || [];

    const cashByCurrency = {};
    let cashTotal = 0;

    for (const b of balances) {
      if (!b) continue;

      const currency =
        (b.currency && (b.currency.code || b.currency)) ||
        b.currency ||
        "UNKNOWN";

      const cashVal =
        typeof b.cash === "number"
          ? b.cash
          : typeof b.total === "number"
          ? b.total
          : 0;

      if (!cashByCurrency[currency]) {
        cashByCurrency[currency] = 0;
      }

      cashByCurrency[currency] += cashVal;
      cashTotal += cashVal;
    }

    const cashUsd = cashByCurrency.USD ? formatMoney(cashByCurrency.USD) : 0;

    const holdings = positions.map((pos) => {
      const sym = pos.symbol?.symbol;
      const rawSymbol =
        sym?.raw_symbol || sym?.symbol || pos.symbol?.local_id || "UNKNOWN";
      const desc =
        sym?.description ||
        pos.symbol?.description ||
        rawSymbol;

      const qty = Number(pos.units || 0);
      const price = Number(pos.price || 0);
      const value = qty * price;

      const avg = pos.average_purchase_price
        ? Number(pos.average_purchase_price)
        : null;

      let pnlPct = 0;
      if (avg && avg > 0 && qty > 0) {
        const cost = avg * qty;
        const pnlMoney = value - cost;
        pnlPct = (pnlMoney / cost) * 100;
      } else if (typeof pos.open_pnl === "number" && value > 0) {
        const pnlMoney = pos.open_pnl;
        const cost = value - pnlMoney;
        if (cost > 0) {
          pnlPct = (pnlMoney / cost) * 100;
        }
      }

      return {
        symbol: rawSymbol,
        name: desc,
        quantity: qty,
        price: formatMoney(price),
        value: formatMoney(value),
        pnlPct: formatMoney(pnlPct),
      };
    });

    let invested = holdings.reduce((sum, h) => sum + (h.value || 0), 0);
    let total = invested + cashTotal;

    const accountTotal = data.account?.balance?.total?.amount;
    if (typeof accountTotal === "number") {
      total = accountTotal;
    }

    const pendingStatuses = new Set([
      "PENDING",
      "ACCEPTED",
      "PARTIAL",
      "PARTIAL_CANCELED",
      "QUEUED",
      "TRIGGERED",
      "ACTIVATED",
      "REPLACE_PENDING",
      "CANCEL_PENDING",
    ]);

    const pendingOrders = (orders || [])
      .filter((o) => pendingStatuses.has(String(o.status || "").toUpperCase()))
      .map((o) => {
        const us = o.universal_symbol;
        const raw =
          us?.raw_symbol || us?.symbol || "UNKNOWN";
        return {
          symbol: raw,
          side: o.action || null,
          quantity: o.open_quantity || o.total_quantity || null,
          orderType: o.order_type || null,
          limitPrice: o.limit_price || null,
          status: o.status || null,
          accountId: ACCOUNT_ID,
          brokerageOrderId: o.brokerage_order_id || "",
        };
      });

    let recentActivity = [];
    if (orders && orders.length > 0) {
      const last = orders[orders.length - 1];
      const us = last.universal_symbol;
      const raw =
        us?.raw_symbol || us?.symbol || "UNKNOWN";
      recentActivity.push({
        symbol: raw,
        side: last.action || null,
        quantity: last.total_quantity || null,
        status: last.status || null,
      });
    }

    // Also attach current market clock so frontend can show status
    const clock = getMarketClock();

    res.json({
  ok: true,
  connected: true,
  total: formatMoney(total),
  cash: formatMoney(cashTotal),
  cashByCurrency: Object.fromEntries(
    Object.entries(cashByCurrency).map(([k, v]) => [k, formatMoney(v)])
  ),
  cashUsd,
  invested: formatMoney(invested),
  pnlMoney: 0,
  pnlPct: 0,
  holdings,
  pendingOrders,
  recentActivity,
  clock,
});

 } catch (err) {
  console.error("[QuickTrade] /api/holdings error:", err);
  const safeMessage = extractSafeError(err);

  // return 200 so the extension doesn't get stuck in "500 spam" loops
  return res.json({
    ok: false,
    connected: false,
    error: safeMessage,
  });
}

});

// ------------- QUOTE ENDPOINT -------------
//
// IMPORTANT:
// - Never throw 500 to the extension repeatedly (causes console spam + instability)
// - Return bid/ask/last when available so frontend can do "marketable limit" logic
// - Add a tiny cache to reduce SnapTrade spam + rate/rando failures
//
const quoteCache = new Map(); // key: SYMBOL, value: { ts, payload }
const QUOTE_CACHE_MS = 900;   // ~1s cache to avoid hammering SnapTrade

app.get("/api/quote/:symbol", async (req, res) => {
  const rawSymbol = String(req.params.symbol || "").trim();
  const symbol = rawSymbol.toUpperCase();

  if (!symbol) {
    return res.json({ ok: false, error: "Missing symbol" });
  }

  // cache hit
  const cached = quoteCache.get(symbol);
  const now = Date.now();
  if (cached && now - cached.ts < QUOTE_CACHE_MS) {
    return res.json(cached.payload);
  }

  console.log("[QuickTrade] Quote request for:", symbol);

  try {
    ensureEnv();

    // Resolve to what SnapTrade expects (handles SIS -> SIS.TO etc.)
    const symbolId = await resolveQuoteSymbolId(symbol);

const quotesResp = await snaptrade.trading.getUserAccountQuotes({
  userId: USER_ID,
  userSecret: USER_SECRET,
  accountId: ACCOUNT_ID,
  symbols: symbolId, // <-- UUID, not ticker
});


    const data = quotesResp.data || quotesResp;

    if (!Array.isArray(data) || data.length === 0) {
      const payload = { ok: false, error: "No quote found", symbol, effectiveSymbol };
      quoteCache.set(symbol, { ts: now, payload });
      return res.json(payload);
    }

    const q = data[0];

    // Try multiple possible field names to be robust
    const bid =
      q.bid_price ?? q.raw?.bid_price ?? q.bid ?? null;

    const ask =
      q.ask_price ?? q.raw?.ask_price ?? q.ask ?? null;

    const last =
      q.last_trade_price ??
      q.last ??
      q.price ??
      bid ??
      ask ??
      null;

    const currency =
      (q.symbol && q.symbol.currency && q.symbol.currency.code) ||
      q.currency ||
      "CAD";

    const payload = {
      ok: true,
      symbol,            // what frontend asked for
      effectiveSymbol,   // what we sent to SnapTrade
      bid,
      ask,
      last,
      currency,
      raw: q,
    };

    quoteCache.set(symbol, { ts: now, payload });
    return res.json(payload);
  } catch (err) {
    const safeMessage = extractSafeError(err);

    // DO NOT return HTTP 500 here — return ok:false with 200,
    // so the extension doesn't get spammed with "500 Internal Server Error"
    // every interval tick.
    const payload = { ok: false, error: safeMessage, symbol };
    quoteCache.set(symbol, { ts: now, payload });
    return res.json(payload);
  }
});

// ------------- ORDER ENDPOINT (panel) -------------
app.post("/api/order", async (req, res) => {
  const {
    symbol,
    side,
    qty,
    orderType,
    limitPrice,
    stopPrice,
    fillAggression, // 0–100 from slider
    accountId,      // Selected trading account ID
  } = req.body || {};

  // ---------------- DEBUG: /api/order ----------------
  const DEBUG_ORDERS = true;
  function dbg(label, obj) {
    if (!DEBUG_ORDERS) return;
    try {
      console.log(`[QT_DEBUG] ${label}:`, JSON.stringify(obj, null, 2));
    } catch {
      console.log(`[QT_DEBUG] ${label}:`, obj);
    }
  }

  dbg("REQ_BODY", req.body);

  // local helpers (kept inside this endpoint so we don't change other layout/code)
  function clamp(n, a, b) {
    return Math.max(a, Math.min(b, n));
  }

  // slider 0–100 -> bps (basis points)
  function sliderToBps(v) {
    const s = clamp(Number(v ?? 50), 0, 100);
    if (s <= 33) return 70;   // Chill  = 0.70%
    if (s <= 66) return 150;  // Normal = 1.50%
    return 300;               // Savage = 3.00%
  }

  // tick rounding: pennies for >= $1, 4dp for < $1
  function roundPx(px) {
    const p = Number(px);
    if (!isFinite(p)) return null;
    if (p >= 1) return Math.round(p * 100) / 100;     // 0.01
    return Math.round(p * 10000) / 10000;             // 0.0001
  }

  // normalize any user-entered limit to our tick rules
  function applyBrokerTick(px) {
    return roundPx(px);
  }

  // ✅ MARKETABLE AUTO LIMIT: BUY >= ASK+tick, SELL <= BID-tick
  async function computeAutoLimit({ rawSymbol, side, fillAggression }) {
    const symbolId = await resolveQuoteSymbolId(rawSymbol);

    const quotesResp = await snaptrade.trading.getUserAccountQuotes({
      userId: USER_ID,
      userSecret: USER_SECRET,
      accountId: ACCOUNT_ID,
      symbols: symbolId, // UUID
    });

    const data = quotesResp.data || quotesResp;
    const q = Array.isArray(data) && data.length > 0 ? data[0] : null;

    const bid = q?.bid_price ?? q?.raw?.bid_price ?? q?.bid ?? null;
    const ask = q?.ask_price ?? q?.raw?.ask_price ?? q?.ask ?? null;
    const last =
      q?.last_trade_price ??
      q?.last ??
      q?.price ??
      bid ??
      ask ??
      null;

    const isBuy = String(side).toUpperCase() === "BUY";

    // reference is executable side
    let ref = isBuy ? Number(ask) : Number(bid);
    if (!isFinite(ref) || ref <= 0) ref = Number(last);

    if (!isFinite(ref) || ref <= 0) {
      throw new Error("QT_BACKEND_ERROR: Could not compute AUTO limit (missing bid/ask/last).");
    }

    const bps = sliderToBps(fillAggression);
    const pct = bps / 10000;

    const tick = ref >= 1 ? 0.01 : 0.0001;
    const buffer = Math.max(ref * pct, tick);

    // initial buffer
    const rawPx = isBuy ? (ref + buffer) : (ref - buffer);

    // ✅ FORCE marketability by at least 1 tick past executable side
    const marketablePx = isBuy
      ? Math.max(rawPx, ref + tick)
      : Math.min(rawPx, ref - tick);

    const finalPx = roundPx(marketablePx);

    dbg("AUTO_LIMIT_INPUTS", {
      rawSymbol,
      side,
      fillAggression,
      bid,
      ask,
      last,
      ref,
      tick,
      buffer,
      rawPx,
      marketablePx,
      finalPx,
    });

    if (!finalPx || finalPx <= 0) {
      throw new Error("QT_BACKEND_ERROR: AUTO limit computation failed.");
    }

    return finalPx;
  }

  try {
    let limitToSend = limitPrice;
    let stopToSend = stopPrice;

    // ✅ If frontend sent AUTO, convert to a real numeric limit price here
    if (String(limitToSend).toUpperCase() === "AUTO") {
      const computed = await computeAutoLimit({
        rawSymbol: symbol,
        side,
        fillAggression,
      });
      limitToSend = computed;
      dbg("AUTO_LIMIT_COMPUTED", { computed: limitToSend });
    }

    // ✅ For limit BUY: ensure price >= real ask so it fills immediately
    // For limit SELL: ensure price <= real bid
    if (
      String(orderType).toLowerCase() === "limit" &&
      limitToSend != null &&
      String(limitToSend).toUpperCase() !== "AUTO"
    ) {
      try {
        const symbolId = await resolveQuoteSymbolId(symbol);
        const quotesResp = await snaptrade.trading.getUserAccountQuotes({
          userId: USER_ID,
          userSecret: USER_SECRET,
          accountId: ACCOUNT_ID,
          symbols: symbolId,
        });
        const qData = quotesResp.data || quotesResp;
        const q = Array.isArray(qData) && qData.length > 0 ? qData[0] : null;
        const realAsk = q?.ask_price ?? q?.raw?.ask_price ?? null;
        const realBid = q?.bid_price ?? q?.raw?.bid_price ?? null;

        if (side.toUpperCase() === "BUY" && realAsk && realAsk > 0) {
          const askPlus = +(realAsk * 1.002).toFixed(2); // ask + 0.2% cushion
          if (Number(limitToSend) < askPlus) {
            dbg("LIMIT_BUMPED_TO_ASK", { was: limitToSend, realAsk, newLimit: askPlus });
            limitToSend = askPlus;
          }
        } else if (side.toUpperCase() === "SELL" && realBid && realBid > 0) {
          const bidMinus = +(realBid * 0.998).toFixed(2);
          if (Number(limitToSend) > bidMinus) {
            dbg("LIMIT_LOWERED_TO_BID", { was: limitToSend, realBid, newLimit: bidMinus });
            limitToSend = bidMinus;
          }
        }
      } catch (qErr) {
        dbg("QUOTE_FETCH_SKIP", { error: qErr.message });
      }
    }

    // ---------------- NORMALIZE LIMIT PRICE TO BROKER TICK ----------------
    if (
      String(orderType).toLowerCase() === "limit" &&
      limitToSend != null &&
      String(limitToSend).toUpperCase() !== "AUTO"
    ) {
      const before = limitToSend;
      limitToSend = applyBrokerTick(limitToSend);
      dbg("LIMIT_TICK_NORMALIZED", { before, after: limitToSend });
    }

    dbg("ORDER_FINAL_INPUTS", {
      symbol,
      side,
      qty,
      orderType,
      limitPrice: limitToSend,
      stopPrice: stopToSend,
    });

    const order = await placeEquityOrder({
      action: side,
      symbol,
      qty,
      orderType,
      limitPrice: limitToSend,
      stopPrice: stopToSend,
      accountId,
    });

    dbg("SNAPTRADE_ORDER_RESPONSE", {
      id: order?.id ?? null,
      status: order?.status ?? null,
      filled_quantity: order?.filled_quantity ?? null,
      open_quantity: order?.open_quantity ?? null,
      order_type: order?.order_type ?? null,
      limit_price: order?.limit_price ?? null,
      stop_price: order?.stop_price ?? null,
      brokerage_order_id: order?.brokerage_order_id ?? null,
      raw: order,
    });

    res.json({ ok: true, order });
  } catch (err) {
    const safeMessage = extractSafeError(err);
    const body =
      err?.responseBody ||
      err?.body ||
      err?.response?.data ||
      null;

    res.status(500).json({
      ok: false,
      error: safeMessage,
      code: body?.code || body?.status_code || err?.code || null,
    });
  }
});




// ------------- LEGACY /api/trade -------------
app.post("/api/trade", async (req, res) => {
  const { action, symbol, shares, orderType, price } = req.body || {};

  console.log("[QuickTrade] Incoming /api/trade payload:", req.body);

  try 
  
  {
    const order = await placeEquityOrder({
      action,
      symbol,
      qty: shares,
      orderType,
      limitPrice: price,
      stopPrice: null,
    });

    res.json({ ok: true, order });
  } catch (err) {
    const safeMessage = extractSafeError(err);
    const body =
      err?.responseBody ||
      err?.body ||
      err?.response?.data ||
      null;

    res.status(500).json({
      ok: false,
      error: safeMessage,
      code: body?.code || body?.status_code || err?.code || null,
    });
  }
});

// ------------- DEBUG TEST ORDER -------------

app.get("/api/debug/test-order", async (req, res) => {
  try {
    const testSymbol = "AAPL"; // adjust while testing
    console.log("[QuickTrade DEBUG] Testing order for:", testSymbol);

    const result = await placeEquityOrder({
      action: "BUY",
      symbol: testSymbol,
      qty: 1,
      orderType: "market",
      limitPrice: null,
      stopPrice: null,
    });

    res.json({ ok: true, result });
  } catch (err) {
    const safeMessage = extractSafeError(err);
    res.status(500).json({ ok: false, error: safeMessage });
  }
});

// -------- START FINNHUB MARKET DATA STREAM --------
try {
  if (FINNHUB_KEY) {
    startFinnhubWS(FINNHUB_KEY, (msgs) => {
      for (const m of msgs) {
        if (m?.T === "q") onQuote(m);
        if (m?.T === "t") onTrade(m);
        if (m?.T === "b") onBar(m);
      }
    });

    // Default watchlist
    setWatchlist(["AAPL", "TSLA"]);
    finnhubSubscribe(["AAPL", "TSLA"]);

    // REST polling for quote data every 5 seconds (bid/ask, VWAP-like)
    setInterval(async () => {
      const wl = getWatchlist();
      for (const sym of wl) {
        const q = await fetchQuote(sym);
        if (q) onQuote(q);
      }
    }, 5000);

    console.log("[QuickTrade] Finnhub stream started.");
  } else {
    console.warn("[QuickTrade] No Finnhub key — signals disabled.");
  }
} catch (err) {
  console.warn("[QuickTrade] Finnhub stream failed (non-fatal):", err.message);
}



// ------------- START SERVER -------------

const PORT = 8000;

// -------- AUTOMATED DAILY DEBRIEF (4:05 PM EST) --------
app.post("/api/sleeper/scan", (req, res) => {
  const scriptPath = path.join(__dirname, "../QuickTradeExtension/backend/sleeper_agent.py");
  const command = `python "${scriptPath}"`;
  
  exec(command, { maxBuffer: 1024 * 1024 * 5, env: { ...process.env, PYTHONIOENCODING: "utf-8" } }, (error, stdout, stderr) => {
    if (error) {
      console.error(`[Sleeper AI Error]: ${error.message}`);
      return res.status(500).json({ ok: false, error: error.message });
    }
    
    try {
      const match = stdout.match(/WATCHLIST:\s*(.+)/);
      if (match && match[1]) {
        const tickers = match[1].split(',').map(t => t.trim());
        return res.json({ ok: true, tickers: tickers });
      }
      return res.status(500).json({ ok: false, error: "Failed to parse Google Gemini output" });
    } catch (e) {
      return res.status(500).json({ ok: false, error: e.message });
    }
  });
});

app.get("/api/sleeper/intel", (req, res) => {
  const intelPath = path.join(__dirname, "../QuickTradeExtension/backend/sleeper_intel.json");
  if (fs.existsSync(intelPath)) {
    res.json(JSON.parse(fs.readFileSync(intelPath)));
  } else {
    res.status(404).json({error: "No sleeper intel"});
  }
});

app.get("/api/dividend/intel", (req, res) => {
  const intelPath = path.join(__dirname, "../QuickTradeExtension/backend/dividend_intel.json");
  if (fs.existsSync(intelPath)) {
    res.json(JSON.parse(fs.readFileSync(intelPath)));
  } else {
    res.status(404).json({error: "No dividend intel"});
  }
});

app.get("/api/backtest/compare_intel", (req, res) => {
  const intelPath = path.join(__dirname, "../QuickTradeExtension/backend/backtest_comparison.json");
  if (fs.existsSync(intelPath)) {
    res.json(JSON.parse(fs.readFileSync(intelPath)));
  } else {
    res.status(404).json({error: "No backtest compare intel"});
  }
});

app.post("/api/backtest/compare", (req, res) => {
    const { exec } = require('child_process');
    const scriptPath = path.join(__dirname, "../QuickTradeExtension/backend/backtest_comparison.py");
    exec(`python "${scriptPath}"`, (error, stdout, stderr) => {
        if (error) {
            console.error(`exec error: ${error}`);
            return res.status(500).send("Error running backtest comparison");
        }
        res.status(200).send("Success");
    });
});


app.get("/api/webull/gainers", (req, res) => {
  const rankType = req.query.rank_type || "preMarket";
  const count = req.query.count || 30;
  
  const scriptPath = path.join(__dirname, "../QuickTradeExtension/backend/webull_scraper.py");
  const command = `python "${scriptPath}" --rank_type ${rankType} --count ${count}`;
  
  exec(command, { maxBuffer: 1024 * 1024, env: { ...process.env, PYTHONIOENCODING: "utf-8" } }, (error, stdout, stderr) => {
    if (error) {
      console.error(`[Webull Scraper Error]: ${error.message}`);
      return res.status(500).json({ ok: false, error: error.message });
    }
    
    try {
      // Find JSON array in the stdout (ignoring warning logs)
      const lines = stdout.split('\n');
      for (const line of lines) {
        if (line.trim().startsWith('{')) {
          const data = JSON.parse(line.trim());
          return res.json(data);
        }
      }
      return res.status(500).json({ ok: false, error: "Failed to parse JSON from scraper" });
    } catch (e) {
      return res.status(500).json({ ok: false, error: "Parse error: " + e.message });
    }
  });
});

setInterval(() => {
  const now = new Date();
  const estString = now.toLocaleString("en-US", { timeZone: "America/New_York" });
  const estDate = new Date(estString);
  
  // Check if it's exactly 16:05 (4:05 PM EST)
  if (estDate.getHours() === 16 && estDate.getMinutes() === 5 && estDate.getSeconds() === 0) {
    console.log("[QuickTrade] Market Closed. Running Automated AI Debrief...");
    const scriptPath = path.join(__dirname, "../QuickTradeExtension/backend", "daily_debrief.py");
    exec(`python "${scriptPath}"`, (error, stdout, stderr) => {
      if (error) {
        console.error("[QuickTrade] Debrief failed:", error.message);
      } else {
        console.log("[QuickTrade] Debrief completed:\n", stdout);
      }
    });
  }
}, 1000);

app.listen(PORT, () => {
  console.log(
    `✅ QuickTrade REAL MONEY backend running on http://localhost:${PORT}`
  );
});
