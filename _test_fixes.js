const fs = require('fs');
const stream = fs.readFileSync('./market/finnhubStream.js', 'utf8');
const srv    = fs.readFileSync('./server.js', 'utf8');

// Scope the 1011 check to just that block
const idx1011     = srv.indexOf('1011 (ambiguous symbol)');
const block1011   = idx1011 >= 0 ? srv.slice(idx1011, idx1011 + 900) : '';

const results = [
  ['finnhubStream - no Finnhub WS URL',                           !stream.includes('wss://ws.finnhub.io')],
  ['finnhubStream - uses scanner /api/price',                     stream.includes('SCANNER_URL') && stream.includes('/api/price')],
  ['finnhubStream - no 24h (86400) sleep',                        !stream.includes('86400')],
  ['finnhubStream - 2s poll interval',                            stream.includes('POLL_INTERVAL_MS = 2000')],
  ['server.js - 1011 handler present',                            srv.includes('1011 (ambiguous symbol)')],
  ['server.js - 1011 block uses forcePayload with raw symbol',    block1011.includes('forcePayload') && block1011.includes('symbol.toUpperCase()')],
  ['server.js - 1011 block NOT using old universal_symbol_id',    !block1011.includes('placeForceOrder(payload)')],
  ['server.js - SnapTrade 429 retry handler',                     srv.includes('SnapTrade 429 on getOrderImpact')],
  ['server.js - 5s retry delay',                                  srv.includes('setTimeout(r, 5000)')],
];

let pass = 0, fail = 0;
results.forEach(([name, ok]) => {
  console.log((ok ? '[PASS]' : '[FAIL]') + ' ' + name);
  ok ? pass++ : fail++;
});
console.log('');
console.log(pass + '/' + results.length + ' passed' + (fail ? ' -- ' + fail + ' FAILURES' : ' -- ALL CLEAR'));
