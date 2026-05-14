import os

path = r'c:\Users\Guest1\Desktop\Business\QuickTradeBackend\server.js'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()

if "const PYTHON_CMD" not in text:
    text = text.replace('const express = require', 'const PYTHON_CMD = process.platform === "win32" ? "python" : "python3";\nconst express = require')

text = text.replace('python fetch_history.py', '${PYTHON_CMD} fetch_history.py')
text = text.replace('`python "${scriptPath}', '`${PYTHON_CMD} "${scriptPath}')
text = text.replace('`python "${scraperPath}', '`${PYTHON_CMD} "${scraperPath}')

with open(path, 'w', encoding='utf-8') as f:
    f.write(text)
