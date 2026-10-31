# -*- coding: utf-8 -*-
"""小米云 API 客户端 —— 纯 Python 实现(不依赖 pycryptodome)
协议: api.io.mi.com 请求用 RC4-drop1024 + SHA1 签名 (注意: 丢弃前1024字节密钥流)
"""
import json, base64, hashlib, time, random, os
import requests


def rc4_drop(key, data, drop=1024):
    S = list(range(256)); j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    i = j = 0
    out = bytearray()
    for _ in range(drop):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
    for b in data:
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out.append(b ^ S[(S[i] + S[j]) & 0xFF])
    return bytes(out)


def enc(password, value):
    return base64.b64encode(rc4_drop(base64.b64decode(password),
                                     value.encode('utf-8'))).decode()


def dec(password, payload):
    return rc4_drop(base64.b64decode(password), base64.b64decode(payload))


def gen_nonce():
    millis = int(round(time.time() * 1000))
    b = (random.getrandbits(64) - 2**63).to_bytes(8, 'big', signed=True)
    part2 = int(millis / 60000)
    b += part2.to_bytes(((part2.bit_length() + 7) // 8), 'big')
    return base64.b64encode(b).decode('utf-8')


def signed_nonce(ssecret, nonce):
    m = hashlib.sha256()
    m.update(base64.b64decode(ssecret))
    m.update(base64.b64decode(nonce))
    return base64.b64encode(m.digest()).decode()


def enc_signature(url, method, sn, params):
    sp = [str(method).upper(), url.split('com')[1].replace('/app/', '/')]
    for k, v in params.items():
        sp.append('%s=%s' % (k, v))
    sp.append(sn)
    return base64.b64encode(hashlib.sha1('&'.join(sp).encode('utf-8')).digest()).decode()


class MiApi:
    """小米云 API. creds: {userId, serviceToken, ssecurity}"""

    def __init__(self, creds, server='cn'):
        self.creds = creds
        self.base = 'https://%sapi.io.mi.com/app' % ('' if server == 'cn' else server + '.')
        self.s = requests.Session()
        self.s.trust_env = False  # 小米云是国内域, 不要走代理
        self.s.headers.update({'User-Agent': 'Android-7.1.1-1.0.0-ONEPLUS A3010-136-QNSQ/1.0.0 MICloud/1.0'})
        self.s.cookies.set('userId', creds['userId'], domain='.io.mi.com')
        self.s.cookies.set('serviceToken', creds['serviceToken'], domain='.io.mi.com')

    def post(self, path, params):
        url = self.base + path
        nonce = gen_nonce()
        sn = signed_nonce(self.creds['ssecurity'], nonce)
        p = dict(params)
        p['rc4_hash__'] = enc_signature(url, 'POST', sn, p)
        p = {k: enc(sn, v) for k, v in p.items()}
        p.update({'signature': enc_signature(url, 'POST', sn, p),
                  'ssecurity': self.creds['ssecurity'], '_nonce': nonce})
        r = self.s.post(url, data=p)
        raw = dec(sn, r.text)
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            try:
                return json.loads(raw.decode('gbk'))
            except Exception:
                return {'_raw': raw[:200], '_status': r.status_code}

    def device_list(self):
        j = self.post('/home/device_list', {
            'data': '{"getVirtualModel":true,"getHuamiDevices":1,'
                    '"get_split_device":false,"support_smart_home":true}'})
        return j['result']['list']

    def miss_get_vendor(self, did, app_pubkey_hex):
        """取 miss 会话的 device_public_key + sign (每次连接都要新取)"""
        return self.post('/v2/device/miss_get_vendor', {
            'data': json.dumps({'app_pubkey': app_pubkey_hex, 'did': str(did),
                                'support_vendors': 'TUTK_CS2_MTP'},
                               separators=(',', ':'))})


def load_creds(path=None):
    from common_config import creds_path
    path = path or creds_path()
    with open(path, encoding='utf-8') as f:
        return json.load(f)


if __name__ == '__main__':
    api = MiApi(load_creds())
    devices = api.device_list()
    print('devices:', len(devices))
    for d in devices:
        print('-', d.get('name'), '|', d.get('model'), '|', d.get('localip'))
