# -*- coding: utf-8 -*-
"""Login Mi Home cloud once, dump model/ip/token of every device.
Usage:  set MI_USER=xxx & set MI_PASS=yyy  (or export in bash)
Credentials are NOT saved to disk."""
import os, sys, json

def main():
    user = os.environ.get('MI_USER')
    pwd = os.environ.get('MI_PASS')
    if not user or not pwd:
        print('ERROR: MI_USER / MI_PASS not set')
        sys.exit(2)
    from micloud import MiCloud
    mc = MiCloud(user, pwd)
    try:
        mc.login()
    except Exception as e:
        print('LOGIN FAILED:', type(e).__name__, e)
        sys.exit(1)
    try:
        devices = mc.get_devices(False)
    except Exception:
        devices = mc.get_devices()
    out = []
    for d in devices:
        out.append({
            'name': d.get('name'),
            'model': d.get('model'),
            'ip': d.get('localip'),
            'mac': d.get('mac'),
            'token': d.get('token'),
            'devid': d.get('did'),
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))
    with open('mi_devices.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print('\nsaved to mi_devices.json (token included, keep local)')

if __name__ == '__main__':
    main()
