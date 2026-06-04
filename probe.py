import urllib.request, json, time

CLOUD = 'https://quicktrade-cloud-production-a088.up.railway.app'
SCANNER = 'https://quicktradescanner-production.up.railway.app'

def get(url, timeout=8):
    r = urllib.request.urlopen(url, timeout=timeout)
    return json.loads(r.read()), r.status

print('=' * 60)
print('  Waiting for quicktrade-cloud to restart...')
print('=' * 60)

booted = False
for i in range(25):
    time.sleep(6)
    try:
        d, status = get(CLOUD + '/')
        print(f'  [{(i+1)*6}s] GET / -> status={status} body={d}')
        booted = True
        break
    except Exception as e:
        print(f'  [{(i+1)*6}s] restarting... {str(e)[:50]}')

print()
print('=' * 60)
print('  Testing quicktrade-cloud endpoints')
print('=' * 60)

endpoints = [
    '/',
    '/api/snaptrade/connect-portal',
    '/api/snaptrade/user-accounts',
]

for ep in endpoints:
    try:
        d, status = get(CLOUD + ep, timeout=15)
        ok = d.get('ok') if isinstance(d, dict) else '?'
        extra = ''
        if ep == '/api/snaptrade/user-accounts':
            accts = d.get('accounts', [])
            extra = f' | accounts={len(accts)}'
        elif ep == '/api/snaptrade/connect-portal':
            extra = f' | redirectURI={str(d.get("redirectURI",""))[:40]}'
        print(f'  [{"PASS" if status==200 and ok else "FAIL"}] {ep} -> ok={ok}{extra}')
    except Exception as e:
        err = str(e)[:70]
        print(f'  [FAIL] {ep} -> {err}')

print()
print('=' * 60)
print('  Testing scanner')
print('=' * 60)
try:
    d, _ = get(SCANNER + '/api/ping-config')
    live = d.get('live', [])
    total = d.get('total_configs', 0)
    print(f'  [PASS] Scanner alive | {total} configs | live={live[0][1] if live else "none"}')
except Exception as e:
    print(f'  [FAIL] Scanner unreachable: {e}')
