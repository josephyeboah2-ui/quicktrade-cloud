// market/signals.routes.js
const express = require("express");
const { getAll, setWatchlist, ensure } = require("./signalsStore");

function makeSignalsRouter({ onWatchlistChanged }) {
  const router = express.Router();

  router.get("/api/signals", (req, res) => {
    res.json({ ok: true, signals: getAll() });
  });

  router.post("/api/watchlist", (req, res) => {
    const symbols = req.body?.symbols || [];
    const wl = setWatchlist(symbols);
    // Pre-create state entries so they appear in /api/signals immediately
    wl.forEach(s => ensure(s));
    onWatchlistChanged?.(wl);
    res.json({ ok: true, watchlist: wl });
  });

  return router;
}

module.exports = { makeSignalsRouter };
