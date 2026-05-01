// registerUser.js
require("dotenv").config();
const { Snaptrade } = require("snaptrade-typescript-sdk");

const clientId = process.env.SNAPTRADE_CLIENT_ID;
const consumerKey = process.env.SNAPTRADE_CONSUMER_KEY;

if (!clientId || !consumerKey) {
  console.error("❌ SNAPTRADE_CLIENT_ID or SNAPTRADE_CONSUMER_KEY is missing from .env");
  process.exit(1);
}

// NEW user ID so we get a fresh, known-good secret
const userId = "quicktrade_main_v2";

async function main() {
  try {
    const snaptrade = new Snaptrade({
      clientId,
      consumerKey,
    });

    console.log("📡 Registering SnapTrade user...");
    console.log("Client ID:", clientId);
    console.log("User ID:", userId);

    const response = await snaptrade.authentication.registerSnapTradeUser({
      userId,
    });

    console.log("✅ User registered successfully!");
    console.log("User ID:", userId);
    console.log("User Secret (SAVE THIS):", response.data.userSecret);
  } catch (err) {
    console.error("❌ SnapTrade error while registering user:");

    if (err.response) {
      console.error("Status:", err.response.status, err.response.statusText);
      console.error("Body:", err.response.data);
    } else {
      console.error(err);
    }
  }
}

main();
