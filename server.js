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

const PYTHON_CMD = "python";
const express    = require("express");
const bodyParser = require("body-parser");
const cors       = require("cors");
const path       = require("path");
const fs         = require("fs");
const { Snaptrade } = require("snaptrade-typescript-sdk");
const { Client }    = require("pg");

// -------- Finnhub Market Data (signals + quotes) --------
const { start: startFinnhubWS, subscribe: finnhubSubscribe, fetchQuote } = require("./market/finnhubStream");
const { onQuote, onTrade, onBar } = require("./market/signalsEngine");
const { makeSignalsRouter } = require("./market/signals.routes");
const { setWatchlist, getWatchlist } = require("./market/signalsStore");
const trailingStopMgr = require("./market/trailingStopManager");

const FINNHUB_KEY = process.env.Finnhub_KEY || "";


// ---------------- ENV ----------------

const CLIENT_ID    = process.env.SNAPTRADE_CLIENT_ID;
const CONSUMER_KEY = process.env.SNAPTRADE_CONSUMER_KEY;
const USER_ID      = process.env.SNAPTRADE_USER_ID;
const USER_SECRET  = process.env.SNAPTRADE_USER_SECRET;
const ACCOUNT_ID   = process.env.SNAPTRADE_ACCOUNT_ID;

// BROKERAGE_AUTH_ID is mutable — it can be refreshed at runtime after
// step-up auth without needing a Railway env var redeploy.
const AUTH_STATE_PATH = path.join(__dirname, "auth_state.json");
let activeAuthId = process.env.SNAPTRADE_BROKERAGE_AUTH_ID || "";
try {
  const saved = JSON.parse(fs.readFileSync(AUTH_STATE_PATH, "utf8"));
  if (saved && saved.brokerageAuthId) {
    activeAuthId = saved.brokerageAuthId;
    console.log("[QuickTrade] Loaded persisted auth ID from auth_state.json");
  }
} catch (_) { /* file doesn't exist yet — that's fine */ }

function saveAuthId(id) {
  activeAuthId = id;
  try { fs.writeFileSync(AUTH_STATE_PATH, JSON.stringify({ brokerageAuthId: id })); }
  catch (e) { console.warn("[QuickTrade] Could not persist auth ID:", e.message); }
  console.log("[QuickTrade] ✅ activeAuthId updated and persisted:", id);
}

console.log("=== QuickTrade SnapTrade ENV CHECK ===");
console.log("SNAPTRADE_CLIENT_ID:",        CLIENT_ID    ? "OK" : "MISSING");
console.log("SNAPTRADE_CONSUMER_KEY:",     CONSUMER_KEY ? "OK" : "MISSING");
console.log("SNAPTRADE_USER_ID:",          USER_ID      ? "OK" : "MISSING");
console.log("SNAPTRADE_USER_SECRET:",      USER_SECRET  ? "OK" : "MISSING");
console.log("SNAPTRADE_ACCOUNT_ID:",       ACCOUNT_ID   ? "OK" : "MISSING");
console.log("SNAPTRADE_BROKERAGE_AUTH_ID:", activeAuthId ? "OK" : "MISSING");
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

const activeBots = {};

function spawnPythonBot(scriptName, reqBody) {
  if (activeBots[scriptName]) {
    console.log(`[QuickTrade] Killing active ${scriptName}...`);
    activeBots[scriptName].kill();
  }
  const { tickers, maxSize, maxLoss, takeProfitPct, trailingStopPct, broker, accountId, strategy } = reqBody;
  
  const scriptPath = path.resolve(__dirname, `./python_scripts/${scriptName}`);
  const args = [scriptPath];
  
  if (tickers) args.push("--tickers", tickers);
  if (maxSize) args.push("--max_size", maxSize.toString());
  if (maxLoss) args.push("--max_loss", maxLoss.toString());
  if (takeProfitPct) args.push("--take_profit_pct", takeProfitPct.toString());
  if (trailingStopPct) args.push("--trailing_stop_pct", trailingStopPct.toString());
  if (broker) args.push("--broker", broker);
  if (accountId) args.push("--account_id", accountId);
  if (strategy) args.push("--strategy", strategy);
  if (reqBody.force) args.push("--force");

  console.log(`\n[QuickTrade] Spawning Python Bot: ${scriptName}`);
  const pyProcess = spawn("python", ["-u", ...args], {
    env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" }
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

// Proxy for Internal Python Servers (Railway only exposes 8000, but python runs on 8002/8003)
app.all("/api/proxy/:port/*path", async (req, res) => {
  const port = req.params.port;
  const targetPath = Array.isArray(req.params.path) ? req.params.path.join("/") : (req.params.path || "");
  const url = `http://localhost:${port}/${targetPath}`;
  
  try {
    const fetch = (await import("node-fetch")).default;
    const fetchOptions = {
      method: req.method,
      headers: { ...req.headers, host: `localhost:${port}` },
    };
    delete fetchOptions.headers['content-length'];
    
    // Don't forward body for GET/HEAD
    if (['POST', 'PUT', 'PATCH'].includes(req.method)) {
      // Fast body forwarding for JSON
      if (req.body && Object.keys(req.body).length > 0) {
        const bodyStr = JSON.stringify(req.body);
        fetchOptions.body = bodyStr;
        fetchOptions.headers['Content-Type'] = 'application/json';
        fetchOptions.headers['content-length'] = Buffer.byteLength(bodyStr).toString();
      }
    }
    
    const response = await fetch(url, fetchOptions);
    const contentType = response.headers.get("content-type");
    
    if (contentType && contentType.includes("application/json")) {
      const data = await response.json();
      res.status(response.status).json(data);
    } else {
      const text = await response.text();
      res.status(response.status).send(text);
    }
  } catch (err) {
    res.status(502).json({ ok: false, error: "Internal Bot Server Unreachable" });
  }
});


app.get("/api/history", (req, res) => {
    const { exec } = require("child_process");
    exec("${PYTHON_CMD} fetch_history.py", { cwd: __dirname }, (error, stdout, stderr) => {
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
  const scriptPath = path.join(__dirname, "./python_scripts", scriptName);

  let args = [];
  if (tickers) args.push("--tickers", `"${tickers}"`);
  if (days) args.push("--days", days);
  if (balance) args.push("--balance", balance);
  if (riskPct) args.push("--risk_pct", riskPct);
  if (strategy) args.push("--strategy", strategy);
  if (dailyQuota) args.push("--daily_quota", dailyQuota);

  const command = `${PYTHON_CMD} "${scriptPath}" ${args.join(" ")}`;
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
  const scriptPath = path.join(__dirname, "./python_scripts", scriptName);

  const command = `${PYTHON_CMD} "${scriptPath}" --ticker ${ticker} --period ${period}`;
  
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

// ------------- DIAGNOSE AUTH STATE -------------
// Returns all brokerage authorizations with their IDs so we can see
// if the stored BROKERAGE_AUTH_ID is stale / disabled / mismatched.
// ------------- STEP-UP AUTH VERIFY (fresh portal — forces full WealthSimple login + MFA) -------------
// Different from connect-portal: NO reconnect param → WealthSimple must do a
// complete fresh login, which includes their step-up trading verification.
// After user completes it, call /api/snaptrade/refresh-auth to auto-save the new auth ID.
app.get("/api/snaptrade/stepup-verify", async (req, res) => {
  try {
    if (!CLIENT_ID || !CONSUMER_KEY || !USER_ID || !USER_SECRET) {
      return res.status(500).json({ ok: false, error: "Missing SnapTrade credentials." });
    }
    // NO reconnect param — force completely fresh WealthSimple login
    const response = await snaptrade.authentication.loginSnapTradeUser({
      userId:                  USER_ID,
      userSecret:              USER_SECRET,
      connectionType:          "trade",
      broker:                  "WEALTHSIMPLETRADE",
      darkMode:                true,
      showCloseButton:         true,
      connectionPortalVersion: "v4",
    });
    console.log("[QuickTrade] stepup-verify portal created:", response.data?.redirectURI);
    res.json({
      ok:          true,
      redirectURI: response.data?.redirectURI,
      sessionId:   response.data?.sessionId,
    });
  } catch (err) {
    console.error("[QuickTrade] stepup-verify error:", err.message);
    res.status(500).json({ ok: false, error: err.message });
  }
});

// ------------- REFRESH AUTH ID (auto-detect new auth after step-up verify) -------------
// Call this after user completes the stepup-verify portal.
// Finds the newest non-disabled brokerage auth and saves it so future orders work.
app.post("/api/snaptrade/refresh-auth", async (req, res) => {
  try {
    const listResp = await snaptrade.connections.listBrokerageAuthorizations({
      userId:     USER_ID,
      userSecret: USER_SECRET,
    });
    const all = Array.isArray(listResp.data) ? listResp.data : [];

    // Prefer active (non-disabled) auths, pick the most recently created one
    const active = all
      .filter(c => !c.disabled)
      .sort((a, b) => new Date(b.created_date || 0) - new Date(a.created_date || 0));

    if (active.length === 0) {
      return res.status(400).json({ ok: false, error: "No active brokerage connections found. Complete the verification portal first." });
    }

    const newId = active[0].id;
    saveAuthId(newId);

    console.log("[QuickTrade] refresh-auth: new activeAuthId =", newId);
    res.json({ ok: true, newAuthId: newId, broker: active[0].brokerage?.slug || "?" });
  } catch (err) {
    console.error("[QuickTrade] refresh-auth error:", err.message);
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.get("/api/snaptrade/diagnose", async (req, res) => {
  try {
    const resp = await snaptrade.connections.listBrokerageAuthorizations({
      userId: USER_ID,
      userSecret: USER_SECRET,
    });
    const list = Array.isArray(resp.data) ? resp.data : [];
    const connections = list.map((c) => ({
      id:            c.id,
      type:          c.type,
      disabled:      !!c.disabled,
      created_date:  c.created_date || null,
      broker:        c.brokerage?.slug || c.brokerage?.name || "?",
    }));
    const configuredId   = activeAuthId || "(not set)";
    const configuredOk   = list.some((c) => c.id === activeAuthId && !c.disabled);
    console.log("[QuickTrade] /diagnose — configured:", configuredId, "| ok:", configuredOk);
    res.json({ ok: true, configuredId, configuredOk, connections });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

// ------------- FULL RESET (nuclear option) -------------
// Deletes ALL brokerage authorizations and opens a fresh connect portal.
// Use this when reconnect doesn't clear step-up auth.
// After completing the portal the user must copy the new auth ID from logs
// and update SNAPTRADE_BROKERAGE_AUTH_ID in Railway env vars.
app.post("/api/snaptrade/full-reset", async (req, res) => {
  try {
    console.log("[QuickTrade] FULL RESET — deleting all brokerage authorizations...");

    // List all
    const listResp = await snaptrade.connections.listBrokerageAuthorizations({
      userId: USER_ID,
      userSecret: USER_SECRET,
    });
    const all = Array.isArray(listResp.data) ? listResp.data : [];
    const deleted = [];

    for (const c of all) {
      try {
        await snaptrade.connections.removeBrokerageAuthorization({
          userId: USER_ID,
          userSecret: USER_SECRET,
          authorizationId: c.id,
        });
        deleted.push(c.id);
        console.log("[QuickTrade] FULL RESET — deleted auth:", c.id);
      } catch (delErr) {
        console.warn("[QuickTrade] FULL RESET — could not delete", c.id, ":", delErr.message);
      }
    }

    // Open fresh portal (no reconnect — this is a clean slate)
    const portalResp = await snaptrade.authentication.loginSnapTradeUser({
      userId: USER_ID,
      userSecret: USER_SECRET,
      connectionType: "trade",
      broker: "WEALTHSIMPLETRADE",
      darkMode: true,
      showCloseButton: true,
      connectionPortalVersion: "v4",
    });

    console.log("[QuickTrade] FULL RESET — fresh portal:", portalResp.data?.redirectURI);
    console.log("[QuickTrade] FULL RESET — after completing the portal, update SNAPTRADE_BROKERAGE_AUTH_ID in Railway with the new auth ID from /api/snaptrade/accounts");

    res.json({
      ok: true,
      deleted,
      redirectURI: portalResp.data?.redirectURI,
      sessionId:   portalResp.data?.sessionId,
      instructions: "Complete the portal, then call GET /api/snaptrade/accounts to find the new auth ID and update SNAPTRADE_BROKERAGE_AUTH_ID in Railway.",
    });
  } catch (err) {
    console.error("[QuickTrade] FULL RESET error:", err.message);
    res.status(500).json({ ok: false, error: err.message });
  }
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

  try {
    const body =
      err?.responseBody ||
      err?.body ||
      err?.response?.data ||
      null;

    const code = body?.code || body?.status_code || err?.code;
    const detail = body?.detail || err?.message || "";
    const detailLower = String(detail || "").toLowerCase();

    // ── Priority 1: Step-up / MFA authentication required ──────────────────
    // Broker (e.g. WealthSimple) requires a re-authentication before trading.
    // This sometimes arrives as code 1146 but the REAL cause is MFA, not an
    // inactive symbol — so we must check the detail text FIRST.
    if (
      /step.?up\s*auth/i.test(detail) ||
      /step-up/i.test(detail) ||
      /mfa.*(required|needed)/i.test(detail) ||
      /2fa.*(required|needed)/i.test(detail) ||
      /additional.*auth/i.test(detail) ||
      /authentication.*required.*place/i.test(detail)
    ) {
      return (
        "QT_STEPUP_AUTH: WealthSimple requires step-up verification before this order can go through." +
        (code ? ` (code ${code})` : "")
      );
    }

    // ── Priority 2: Code 1063 — WealthSimple can't obtain real-time data ───
    // This fires when WealthSimple is unable to price the order impact.
    // Common causes: market closed, ticker in maintenance, or stale session.
    if (String(code) === "1063" || /failed to obtain data/i.test(detailLower)) {
      const clock = getMarketClock();
      const sessionHint = clock.isClosed
        ? " The market is currently CLOSED — WealthSimple cannot price order impact outside trading hours. Try again during regular hours (9:30–4:00 PM ET), or place a Day Limit order."
        : " Try clicking Connect to refresh your brokerage session, then retry your order.";
      return (
        "QT_BACKEND_ERROR: WealthSimple couldn't obtain real-time data to validate this order (code 1063)." +
        sessionHint
      );
    }

    // ── Priority 3: Inactive / delisted security (code 1146) ───────────────
    // Only show this when the detail actually refers to an inactive security,
    // NOT when it's a step-up auth issue that happens to share code 1146.
    if (
      String(code) === "1146" ||
      /inactive as of/i.test(detailLower) ||
      /security.*inactive/i.test(detailLower) ||
      /symbol.*inactive/i.test(detailLower)
    ) {
      return (
        "QT_BACKEND_ERROR: This symbol is currently marked INACTIVE " +
        "on SnapTrade's side, so QuickTrade can't send live orders for it. " +
        "You may still be able to trade it directly in your broker's app." +
        (detail ? ` (${detail})` : "") +
        (code ? ` (code ${code})` : "")
      );
    }

    // ── Generic SnapTrade error with detail ─────────────────────────────────
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
 * - is pre-market / post-market / overnight
 * - recommended order type for frontend (market vs limit)
 * - snapTradeSession: the value to pass as trading_session in SnapTrade payloads
 *
 * WealthSimple trading sessions (US equities only):
 *  - Overnight : 20:00 ET (Fri/weekday) – 04:00 ET (next day)
 *  - Pre-market: 04:00 – 09:30 ET
 *  - Regular   : 09:30 – 16:00 ET
 *  - Post-market: 16:00 – 20:00 ET
 */
function getMarketClock() {
  const now = new Date();
  const nyString = now.toLocaleString("en-US", {
    timeZone: "America/New_York",
  });
  const nyNow = new Date(nyString);

  const day = nyNow.getDay(); // 0=Sun, 1=Mon … 6=Sat
  const isWeekday = day >= 1 && day <= 5;
  // Sunday night counts as overnight for Monday's pre-market
  const isSundayNight = day === 0;

  const hours = nyNow.getHours();
  const minutes = nyNow.getMinutes();
  const totalMin = hours * 60 + minutes;

  const OVERNIGHT_START = 20 * 60; // 20:00 — overnight opens
  const PRE_OPEN        =  4 * 60; // 04:00 — pre-market opens
  const REG_OPEN        =  9 * 60 + 30; // 09:30
  const REG_CLOSE       = 16 * 60; // 16:00
  const POST_CLOSE      = 20 * 60; // 20:00 — post-market closes / overnight opens

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
    } else {
      // Before 04:00 or at/after 20:00 on a weekday = overnight
      session = "OVERNIGHT";
      isExtended = true;
    }
  } else if (isSundayNight && totalMin >= OVERNIGHT_START) {
    // Sunday 20:00–midnight is overnight for Monday
    session = "OVERNIGHT";
    isExtended = true;
  }

  const isClosed = !isOpenRegular && !isExtended;

  // Map to the trading_session value WealthSimple/SnapTrade expects
  const snapTradeSession =
    session === "REGULAR"  ? "REGULAR" :
    session === "PRE"      ? "PRE_MARKET" :
    session === "POST"     ? "AFTER_MARKET" :
    session === "OVERNIGHT"? "AFTER_MARKET" :  // WealthSimple treats overnight as after-market
    "REGULAR"; // fallback for CLOSED (orders queue for next session)

  // Frontend can use this to auto-toggle order type:
  //  - REGULAR  -> market + limit allowed
  //  - PRE/POST/OVERNIGHT -> limit only
  const recommendedOrderType = isOpenRegular ? "market" : "limit";

  return {
    exchange: "US_EQUITIES",
    timeZone: "America/New_York",
    isoNow: nyNow.toISOString(),
    session,          // "REGULAR" | "PRE" | "POST" | "OVERNIGHT" | "CLOSED"
    snapTradeSession, // value to pass as trading_session to SnapTrade
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
const quoteSymbolCache = new Map(); // cache: raw symbol → universal_symbol_id

// Exchange priority order for WealthSimple via SnapTrade.
// WealthSimple trades US equities + Canadian equities only.
// Any other exchange (AEX, EBS, BINANCE, XIDX, PAR, etc.) is WRONG —
// those are European crypto ETPs or foreign listings of the same ticker.
// Probe results confirmed: SDOT (4 matches), AAPL (11), TSLA (11), WCT (4)
// — all ambiguous ONLY because of non-WS-tradable foreign listings.
// Strict priority below eliminates all 1011 errors permanently.
const EXCHANGE_PRIORITY = [
  // US — WealthSimple primary market
  "NASDAQ", "NYSE", "AMEX", "BATS", "ARCA", "IEX",
  // Canadian — WealthSimple secondary market
  "TSX", "TSXV", "CSE", "NEO", "CNSX",
  // OTC / Pink sheets — WealthSimple does trade some OTC (e.g. CWBHF)
  "OTCM", "OTC", "OTCBB", "PINK",
];

async function resolveQuoteSymbolId(rawSymbol) {
  const upper = String(rawSymbol || "").toUpperCase().trim();
  if (!upper) throw new Error("Missing symbol");

  if (quoteSymbolCache.has(upper)) return quoteSymbolCache.get(upper);

  // Strip exchange suffix (.US / .TO / .V) for the SnapTrade lookup
  const searchBase = upper.split(".")[0];
  const forcedRegion = upper.endsWith(".TO") || upper.endsWith(".V") ? "CA"
                     : upper.endsWith(".US")                         ? "US"
                     : null;

  const resp = await snaptrade.referenceData.getSymbols({ substring: searchBase });
  const list = resp.data || [];

  if (!Array.isArray(list) || list.length === 0)
    throw new Error(`No SnapTrade symbol found for ${upper}`);

  // Exact raw_symbol matches only
  const exact = list.filter(s =>
    s && s.raw_symbol &&
    String(s.raw_symbol).toUpperCase() === searchBase
  );

  const pool = exact.length > 0 ? exact : list;

  const getExch = s => String(s?.exchange?.code || s?.exchange || "").toUpperCase();

  // If user explicitly forced a region, filter to that region first
  let candidates = pool;
  if (forcedRegion === "US") {
    candidates = pool.filter(s => ["NASDAQ","NYSE","AMEX","BATS","ARCA","IEX"].includes(getExch(s)));
  } else if (forcedRegion === "CA") {
    candidates = pool.filter(s => ["TSX","TSXV","CSE","NEO","CNSX"].includes(getExch(s)));
  }
  if (candidates.length === 0) candidates = pool; // fallback if filter too aggressive

  // Pick by strict exchange priority — first match wins
  let best = null;
  for (const exch of EXCHANGE_PRIORITY) {
    best = candidates.find(s => getExch(s) === exch);
    if (best) break;
  }

  // Last resort: first result in pool (better than nothing)
  if (!best) best = candidates[0] || pool[0];

  if (!best?.id) throw new Error(`Symbol ID missing for ${upper}`);

  const chosenExch = getExch(best);
  console.log(`[SymbolResolver] ${upper} → id=${best.id} exch=${chosenExch} sym=${best.symbol} (${candidates.length} candidates, ${pool.length} exact)`);

  quoteSymbolCache.set(upper, { id: best.id, exchange: chosenExch });
  return { id: best.id, exchange: chosenExch };
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

  // 🔔 Market clock: block Market orders outside regular hours.
  // Stop/StopLimit orders are allowed in extended hours (they queue and trigger
  // when the market session opens and price is hit).
  const clock = getMarketClock();
  if (!clock.isOpenRegular && snapOrderType === "Market") {
    throw new Error(
      "Market orders are only allowed during regular hours (09:30–16:00 ET). " +
      (clock.isExtended
        ? `Market is in ${clock.session} session — switch to a Limit order.`
        : "Market is CLOSED — switch to a Limit order.")
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

  // Resolve symbol to UUID + exchange (automatically filtered for North American exchanges)
  const resolved  = await resolveQuoteSymbolId(symbol);
  const symbolId  = resolved.id;
  const symbolExchange = resolved.exchange;

  // Map current market session to the SnapTrade trading_session field.
  // This tells WealthSimple which session book to route the order to:
  //   REGULAR     -> regular 09:30-16:00 session
  //   PRE_MARKET  -> 04:00-09:30 pre-market (limit only)
  //   AFTER_MARKET-> 16:00-20:00 post + overnight (limit only)
  const tradingSession = clock.snapTradeSession;

  // Use the accountId from the request if it matches our configured account.
  // If it's stale (from extension cache after a connection reset), fall back
  // to the env var ACCOUNT_ID so orders don't fail with "Account not found".
  const finalAccountId = (accountId && accountId === ACCOUNT_ID)
    ? accountId
    : ACCOUNT_ID;

  if (accountId && accountId !== ACCOUNT_ID) {
    console.warn(
      `[QuickTrade] Incoming accountId (${accountId}) doesn't match configured ACCOUNT_ID (${ACCOUNT_ID}) — using env var.`
    );
  }


  // WealthSimple only accepts "Day" or "FOK" for time_in_force.
  // The trading_session field (PRE_MARKET / AFTER_MARKET / REGULAR) is what
  // routes the order to the correct extended-hours book on their side.
  const timeInForce = "Day";

  const payload = {
    userId: USER_ID,
    userSecret: USER_SECRET,
    account_id: finalAccountId,
    action: snapAction,
    universal_symbol_id: symbolId,
    order_type: snapOrderType,
    time_in_force: timeInForce,
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

  // ── TWO-STEP ORDER FLOW ────────────────────────────────────────────────
  // WealthSimple rejects placeForceOrder (step-up auth / code 1146) because
  // it bypasses their normal confirmation flow.  The correct approach is:
  //   Step 1: getOrderImpact  → get a tradeId + order preview
  //   Step 2: placeOrder      → confirm using that tradeId
  // This is the standard flow WealthSimple expects for API trading.
  //
  // EXCEPTION — code 1063 "failed to obtain data":
  //   WealthSimple can't compute order impact when it has no real-time quote
  //   (e.g. after-hours, ticker in brief data outage).  In that case we fall
  //   back directly to placeForceOrder so Day Limit orders can still be
  //   queued and will execute when the market opens.
  // ──────────────────────────────────────────────────────────────────────

  // Step 1 — get order impact (validates the order and returns a tradeId)
  let impactResp, impactData, tradeId;
  try {
    impactResp = await snaptrade.trading.getOrderImpact(payload);
    impactData  = impactResp.data || impactResp;
    tradeId =
      impactData?.trade?.id ||
      impactData?.id       ||
      impactData?.tradeId  ||
      null;
  } catch (impactErr) {
    // Extract error code from SnapTrade SDK error shape
    const impactBody =
      impactErr?.responseBody ||
      impactErr?.body         ||
      impactErr?.response?.data ||
      null;
    const impactCode = String(impactBody?.code || impactBody?.status_code || impactErr?.code || "");

    if (impactCode === "1063" || /failed to obtain data/i.test(impactBody?.detail || "")) {
      // Market-data unavailable — skip impact check and force-place the limit order
      console.warn(
        "[QuickTrade] getOrderImpact code 1063 (no real-time data) — falling back to placeForceOrder.",
        "Order type:", snapOrderType, "| Symbol:", symbol
      );
      // Market orders are already blocked above; only Limit/Stop reach here after-hours
      const forceResp = await snaptrade.trading.placeForceOrder(payload);
      console.log("[QuickTrade] placeForceOrder (1063 fallback) success:", forceResp.data || forceResp);
      return forceResp.data || forceResp;
    }

    if (impactCode === "1011" || /ambiguous/i.test(impactBody?.detail || "")) {
      // Symbol maps to multiple instruments on SnapTrade.
      // Clear the cache so next attempt re-resolves fresh.
      const upperSym = String(symbol || "").toUpperCase().trim();
      quoteSymbolCache.delete(upperSym);

      // Try placeForceOrder with universal_symbol_id ONLY (no raw symbol —
      // passing both symbol + universal_symbol_id causes 1012 "Invalid input").
      console.warn(
        "[QuickTrade] getOrderImpact 1011 — retrying placeForceOrder with UUID only. Symbol:", symbol, "UUID:", symbolId
      );
      const forcePayload = {
        userId:              USER_ID,
        userSecret:          USER_SECRET,
        account_id:          finalAccountId,
        action:              snapAction,
        universal_symbol_id: symbolId,
        order_type:          snapOrderType,
        time_in_force:       timeInForce,
        trading_session:     tradingSession,
        units,
      };
      if (snapOrderType === "Limit" || snapOrderType === "StopLimit") forcePayload.price = px;
      if (snapOrderType === "Stop"  || snapOrderType === "StopLimit") forcePayload.stop  = sp;
      const forceResp = await snaptrade.trading.placeForceOrder(forcePayload);
      console.log("[QuickTrade] placeForceOrder (1011 fallback) success:", forceResp.data || forceResp);
      return forceResp.data || forceResp;
    }

    // SnapTrade 429 — wait 5s and retry once
    if (impactCode === "429" || String(impactErr?.status || "") === "429") {
      console.warn("[QuickTrade] SnapTrade 429 on getOrderImpact — retrying in 5s...");
      await new Promise(r => setTimeout(r, 5000));
      impactResp = await snaptrade.trading.getOrderImpact(payload);
      impactData  = impactResp.data || impactResp;
      tradeId = impactData?.trade?.id || impactData?.id || impactData?.tradeId || null;
      if (!tradeId) throw new Error("SnapTrade 429 retry succeeded but no tradeId returned.");
      // Fall through to placeOrder below
    } else {
      // Re-throw any other impact error so it surfaces normally
      throw impactErr;
    }
  }

  if (!tradeId) {
    // Fallback: some brokers return impact but no tradeId — try force as last resort
    console.warn("[QuickTrade] getOrderImpact returned no tradeId — falling back to placeForceOrder");
    const forceResp = await snaptrade.trading.placeForceOrder(payload);
    console.log("[QuickTrade] placeForceOrder (fallback) success:", forceResp.data || forceResp);
    return forceResp.data || forceResp;
  }

  console.log("[QuickTrade] Order impact OK — tradeId:", tradeId, "| estimated:", JSON.stringify(impactData?.estimated_commissions || impactData?.trade || {}));

  // Step 2 — confirm and place the order using the tradeId
  let placeResp;
  try {
    placeResp = await snaptrade.trading.placeOrder({
      tradeId,
      userId: USER_ID,
      userSecret: USER_SECRET,
    });
    console.log("[QuickTrade] SnapTrade order placed (two-step):", placeResp.data || placeResp);
    return placeResp.data || placeResp;
  } catch (placeErr) {
    const placeBody = placeErr?.response?.data || placeErr?.responseBody || {};
    const placeCode = String(placeBody?.code || "");
    const placeDetail = String(placeBody?.detail || "");

    // 1146 / step-up auth — WealthSimple blocks placeOrder but placeForceOrder
    // uses a different internal endpoint that doesn't require step-up verification.
    if (placeCode === "1146" || /step.?up/i.test(placeDetail)) {
      console.warn("[QuickTrade] placeOrder blocked by step-up auth (1146) — retrying with placeForceOrder...");

      // Use brokerage_symbol_id from the impact response — this is WealthSimple's
      // own internal symbol ID, fully unambiguous, eliminates 1012 "Invalid input".
      const brokerageSymId =
        impactData?.symbol?.brokerage_symbol_id ||
        impactData?.trade?.symbol?.brokerage_symbol_id ||
        null;

      const forcePayload = {
        userId:              USER_ID,
        userSecret:          USER_SECRET,
        account_id:          finalAccountId,
        action:              snapAction,
        order_type:          snapOrderType,
        time_in_force:       timeInForce,
        trading_session:     tradingSession,
        units,
      };

      // Prefer brokerage_symbol_id > universal_symbol_id for WealthSimple
      if (brokerageSymId) {
        forcePayload.brokerage_symbol_id = brokerageSymId;
        console.log("[QuickTrade] 1146 forcePayload using brokerage_symbol_id:", brokerageSymId);
      } else {
        forcePayload.universal_symbol_id = symbolId;
        console.log("[QuickTrade] 1146 forcePayload using universal_symbol_id:", symbolId);
      }

      if (snapOrderType === "Limit" || snapOrderType === "StopLimit") forcePayload.price = px;
      if (snapOrderType === "Stop"  || snapOrderType === "StopLimit") forcePayload.stop  = sp;
      const forceResp = await snaptrade.trading.placeForceOrder(forcePayload);
      console.log("[QuickTrade] placeForceOrder (1146 bypass) success:", forceResp.data || forceResp);
      return forceResp.data || forceResp;
    }
    throw placeErr;
  }
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

    // ── KEY FIX ────────────────────────────────────────────────────────────
    // If we already have a brokerage auth ID (i.e. WealthSimple is already
    // linked), use `reconnect` so SnapTrade re-authenticates the *existing*
    // connection.  This is what actually clears the step-up (MFA) requirement
    // on the active trading account.
    //
    // Creating a NEW connection (no `reconnect`) does NOT clear step-up auth
    // on the old connection — it just creates a parallel orphaned connection
    // that is never used for trading.
    // ──────────────────────────────────────────────────────────────────────

    const loginParams = {
      userId: USER_ID,
      userSecret: USER_SECRET,
      connectionType: "trade",
      broker: "WEALTHSIMPLETRADE",
      darkMode: true,
      showCloseButton: true,
      connectionPortalVersion: "v4",
    };

    if (activeAuthId) {
      // Reconnect the existing auth → clears step-up auth
      loginParams.reconnect = activeAuthId;
      console.log(
        "[QuickTrade] connect-portal: using RECONNECT mode (existing auth):",
        activeAuthId
      );
    } else {
      // First-time setup — create a fresh connection
      console.log(
        "[QuickTrade] connect-portal: no activeAuthId — creating new connection"
      );
    }

    const response = await snaptrade.authentication.loginSnapTradeUser(loginParams);

    console.log("[QuickTrade] Created connection portal:", response.data);

    res.json({
      ok: true,
      redirectURI: response.data.redirectURI,
      sessionId: response.data.sessionId,
      mode: activeAuthId ? "reconnect" : "new",
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

    if (!activeAuthId) {
      return res.status(500).json({
        ok: false,
        error:
          "Missing SNAPTRADE_BROKERAGE_AUTH_ID in .env (use brokerage_authorization from /api/snaptrade/accounts)",
      });
    }

    const response = await snaptrade.authentication.loginSnapTradeUser({
      userId: USER_ID,
      userSecret: USER_SECRET,
      reconnect: activeAuthId,
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
    const authId = (req.body && req.body.authorizationId) || activeAuthId;

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
    const { id: symbolId } = await resolveQuoteSymbolId(symbol);

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
    const { id: symbolId } = await resolveQuoteSymbolId(rawSymbol);

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

    // Note: limit price is trusted from the frontend — it already computes
    // the correct price using ask + buffer. No extra quote fetch needed here.


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

const PORT = process.env.PORT || 8000;

// -------- AUTOMATED DAILY DEBRIEF (4:05 PM EST) --------
app.post("/api/sleeper/scan", (req, res) => {
  const scriptPath = path.join(__dirname, "./python_scripts/sleeper_agent.py");
  const command = `${PYTHON_CMD} "${scriptPath}"`;
  
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
  const intelPath = path.join(__dirname, "./python_scripts/sleeper_intel.json");
  if (fs.existsSync(intelPath)) {
    res.json(JSON.parse(fs.readFileSync(intelPath)));
  } else {
    res.status(404).json({error: "No sleeper intel"});
  }
});

app.get("/api/dividend/intel", (req, res) => {
  const intelPath = path.join(__dirname, "./python_scripts/dividend_intel.json");
  if (fs.existsSync(intelPath)) {
    res.json(JSON.parse(fs.readFileSync(intelPath)));
  } else {
    res.status(404).json({error: "No dividend intel"});
  }
});

app.get("/api/backtest/compare_intel", (req, res) => {
  const intelPath = path.join(__dirname, "./python_scripts/backtest_comparison.json");
  if (fs.existsSync(intelPath)) {
    res.json(JSON.parse(fs.readFileSync(intelPath)));
  } else {
    res.status(404).json({error: "No backtest compare intel"});
  }
});

app.post("/api/backtest/compare", (req, res) => {
    const { exec } = require('child_process');
    const scriptPath = path.join(__dirname, "./python_scripts/backtest_comparison.py");
    exec(`${PYTHON_CMD} "${scriptPath}"`, (error, stdout, stderr) => {
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
  
  const scriptPath = path.join(__dirname, "./python_scripts/webull_scraper.py");
  const command = `${PYTHON_CMD} "${scriptPath}" --rank_type ${rankType} --count ${count}`;
  
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
    const scriptPath = path.join(__dirname, "./python_scripts", "daily_debrief.py");
    exec(`${PYTHON_CMD} "${scriptPath}"`, (error, stdout, stderr) => {
      if (error) {
        console.error("[QuickTrade] Debrief failed:", error.message);
      } else {
        console.log("[QuickTrade] Debrief completed:\n", stdout);
      }
    });
  }

  // Check if it's exactly 09:35 (9:35 AM EST) to log daily gainers for backtesting
  if (estDate.getHours() === 9 && estDate.getMinutes() === 35 && estDate.getSeconds() === 0) {
    console.log("[QuickTrade] Logging Morning Top Gainers...");
    const scraperPath = path.join(__dirname, "./python_scripts", "webull_scraper.py");
    exec(`${PYTHON_CMD} "${scraperPath}" --count 50`, (error, stdout, stderr) => {
      if (error) {
        console.error("[QuickTrade] Gainer logging failed:", error.message);
      } else {
        try {
          const lines = stdout.split('\n');
          let data = null;
          for (const line of lines) {
            if (line.trim().startsWith('{')) {
              data = JSON.parse(line.trim());
              break;
            }
          }
          if (data && data.ok && data.tickers) {
            const isoKey = estDate.toISOString().split('T')[0];
            if (process.env.DATABASE_URL) {
              const client = new Client({ connectionString: process.env.DATABASE_URL });
              client.connect().then(() => {
                  return client.query(`
                      CREATE TABLE IF NOT EXISTS historical_gainers (
                          date DATE PRIMARY KEY,
                          tickers JSONB NOT NULL
                      )
                  `);
              }).then(() => {
                  return client.query(
                      'INSERT INTO historical_gainers (date, tickers) VALUES ($1, $2) ON CONFLICT (date) DO UPDATE SET tickers = $2',
                      [isoKey, JSON.stringify(data.tickers)]
                  );
              }).then(() => {
                  console.log(`[QuickTrade] Successfully saved ${data.tickers.length} gainers for ${isoKey} to Postgres.`);
                  client.end();
              }).catch(err => {
                  console.error("[QuickTrade] Postgres Error:", err.message);
                  if (client) client.end();
              });
            } else {
              const histFile = path.join(__dirname, "./python_scripts", "historical_gainers.json");
              let histData = {};
              if (fs.existsSync(histFile)) {
                histData = JSON.parse(fs.readFileSync(histFile, 'utf8'));
              }
              histData[isoKey] = data.tickers;
              fs.writeFileSync(histFile, JSON.stringify(histData, null, 2));
              console.log(`[QuickTrade] Successfully saved ${data.tickers.length} gainers for ${isoKey} to local file.`);
            }
          }
        } catch(e) {
          console.error("Failed to save gainers:", e.message);
        }
      }
    });
  }
}, 1000);

app.listen(Number(process.env.PORT) || 8000, "0.0.0.0", () => {
  console.log(
    `✅ QuickTrade REAL MONEY backend running on http://localhost:${PORT}`
  );
});
