// snaptradeClient.js
// Initializes a reusable SnapTrade API client using axios

const axios = require("axios");
require("dotenv").config();

const SNAPTRADE_BASE_URL = "https://api.snaptrade.com/api/v1";

// Safety checks
if (!process.env.SNAPTRADE_CLIENT_ID || !process.env.SNAPTRADE_CONSUMER_KEY) {
  console.error("❌ Missing SNAPTRADE_CLIENT_ID or SNAPTRADE_CONSUMER_KEY in .env");
  process.exit(1);
}

// ✅ This is your initialized SnapTrade client
const snaptrade = axios.create({
  baseURL: SNAPTRADE_BASE_URL,
  headers: {
    "Content-Type": "application/json",
    "Client-Id": process.env.SNAPTRADE_CLIENT_ID,
    "Consumer-Key": process.env.SNAPTRADE_CONSUMER_KEY,
  },
});

// Helper for cleaner error logs
function logSnaptradeError(err) {
  if (err.response) {
    console.error("❌ SnapTrade error:", err.response.status, err.response.statusText);
    console.error("Response body:", err.response.data);
  } else {
    console.error("❌ SnapTrade error:", err.message);
  }
}

module.exports = {
  snaptrade,
  logSnaptradeError,
};
