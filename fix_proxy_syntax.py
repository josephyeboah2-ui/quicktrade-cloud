import os

path = r'c:\Users\Guest1\Desktop\Business\QuickTradeBackend\server.js'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

target = """    try {
      const fetch = (await import("node-fetch")).default;
      const fetchOptions = {
        method: req.method,
        headers: { ...req.headers, host: `localhost:${port}` },
        };
      delete fetchOptions.headers['content-length'];
      };"""

replacement = """    try {
      const fetch = (await import("node-fetch")).default;
      const fetchOptions = {
        method: req.method,
        headers: { ...req.headers, host: `localhost:${port}` }
      };
      delete fetchOptions.headers['content-length'];"""

text = text.replace(target, replacement)

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)
