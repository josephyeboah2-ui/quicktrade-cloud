// testSnaptradeKey.js
require("dotenv").config();
const { Snaptrade } = require("snaptrade-typescript-sdk");

const clientId = process.env.SNAPTRADE_CLIENT_ID;
const consumerKey = process.env.SNAPTRADE_CONSUMER_KEY;

console.log("Using clientId:", clientId);
console.log("Using consumerKey (first 8 chars):", consumerKey?.slice(0, 8));

if (!clientId || !consumerKey) {
  console.error("❌ Missing SNAPTRADE_CLIENT_ID or SNAPTRADE_CONSUMER_KEY in .env");
  process.exit(1);
}

async function main() {
  try {
    const snaptrade = new Snaptrade({
      clientId,
      consumerKey,
    });

    console.log("📡 Calling getPartnerInfo to test API key...");
    const res = await snaptrade.referenceData.getPartnerInfo();

    console.log("✅ API key works! Partner info:");
    console.log(res.data);
  } catch (err) {
    console.error("❌ SnapTrade error when testing API key:");

    if (err.response) {
      console.error("Status:", err.response.status, err.response.statusText);
      console.error("Body:", err.response.data);
    } else {
      console.error(err);
    }
  }
}

main();

