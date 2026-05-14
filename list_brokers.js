require("dotenv").config();
const { Snaptrade } = require("snaptrade-typescript-sdk");

const snaptrade = new Snaptrade({
  clientId: process.env.SNAPTRADE_CLIENT_ID,
  consumerKey: process.env.SNAPTRADE_CONSUMER_KEY,
});

async function run() {
  try {
    const res = await snaptrade.referenceData.listAllBrokerages();
    const brokers = res.data || res;
    for (const b of brokers) {
      if (b.name.toLowerCase().includes('webull')) {
        console.log(`FOUND: ${b.name} -> Slug: ${b.slug}`);
      }
    }
  } catch (e) {
    console.error(e);
  }
}

run();
