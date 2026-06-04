import os

path = r'c:\Users\Guest1\Desktop\Business\QuickTradeBackend\server.js'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

target = """    // Don't forward body for GET/HEAD
    if (['POST', 'PUT', 'PATCH'].includes(req.method)) {
      // Fast body forwarding for JSON
      if (Object.keys(req.body).length > 0) {
        fetchOptions.body = JSON.stringify(req.body);
        fetchOptions.headers['Content-Type'] = 'application/json';
      }
    }"""

replacement = """    // Don't forward body for GET/HEAD
    if (['POST', 'PUT', 'PATCH'].includes(req.method)) {
      // Fast body forwarding for JSON
      if (req.body && Object.keys(req.body).length > 0) {
        const bodyStr = JSON.stringify(req.body);
        fetchOptions.body = bodyStr;
        fetchOptions.headers['Content-Type'] = 'application/json';
        fetchOptions.headers['content-length'] = Buffer.byteLength(bodyStr).toString();
      }
    }"""

if target in text:
    text = text.replace(target, replacement)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print("Replaced successfully")
else:
    print("Target not found")
