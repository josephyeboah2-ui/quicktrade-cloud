// testLoginUser.js
require("dotenv").config();
const { Snaptrade } = require("snaptrade-typescript-sdk");

const clientId = process.env.SNAPTRADE_CLIENT_ID;
const consumerKey = process.env.SNAPTRADE_CONSUMER_KEY;
const userId = process.env.SNAPTRADE_USER_ID;
const userSecret = process.env.SNAPTRADE_USER_SECRET;

console.log("clientId:", clientId);
console.log("userId:", userId);
console.log("userSecret (first 8 chars):", userSecret ? userSecret.slice(0, 8) : null);

if (!clientId || !consumerKey || !userId || !userSecret) {
  console.error("❌ Missing env vars");
  process.exit(1);
}

async function main() {
  try {
    const snaptrade = new Snaptrade({
      clientId,
      consumerKey,
    });

    console.log("📡 Calling loginSnapTradeUser to test user credentials...");

    const resp = await snaptrade.authentication.loginSnapTradeUser({
      userId,
      userSecret,
      body: {
        broker: "WEALTHSIMPLETRADE",
        connectionType: "trade",
      },
    });

    console.log("✅ SUCCESS:");
    console.log(resp.data);
  } catch (err) {
    console.error("❌ ERROR:");
    if (err.response) {
      console.error("Status:", err.response.status, err.response.statusText);
      console.error("Body:", err.response.data);
    } else if (err.responseBody) {
      console.error("Body:", err.responseBody);
    } else {
      console.error(err);
    }
  }
}

main();
