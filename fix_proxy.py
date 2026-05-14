import os

path = r'c:\Users\Guest1\Desktop\Business\QuickTradeBackend\server.js'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

target = "headers: { ...req.headers, host: `localhost:${port}` },"
replacement = "headers: { ...req.headers, host: `localhost:${port}` },\n      };\n      delete fetchOptions.headers['content-length'];"

if "delete fetchOptions.headers['content-length'];" not in text:
    text = text.replace(target, replacement)

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)
