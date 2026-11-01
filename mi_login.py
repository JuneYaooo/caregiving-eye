# -*- coding: utf-8 -*-
"""米家账号登录 → 生成 mi_creds.json (userId/serviceToken/ssecurity/passToken)

用法:
  export MI_USER=你的账号 MI_PASS=你的密码
  python mi_login.py            # 有缓存的 passToken 时静默续期
  python mi_login.py --force    # 强制重新登录 (可能需要输入短信验证码)

说明:
  - 首次在新设备登录, 小米大概率要求二次验证 (securityStatus=16):
    脚本会自动走 identity API 发短信验证码, 交互输入即可。
  - 个别账号还会要求"确认安全手机"(JS 页面流程): 此时按脚本提示,
    用任意浏览器打开打印出的 notificationUrl 完成一次确认, 然后重跑本脚本。
  - 登录成功后 passToken 长期有效, 日常只需静默续期。
"""
import argparse, hashlib, json, os, sys
from urllib.parse import urlparse, parse_qs, unquote

import requests

UA = {'User-Agent': 'Android-7.1.1-1.0.0-ONEPLUS A3010-136-QNSQ/1.0.0 MICloud/1.0'}


def save_creds(creds):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, 'mi_creds.json'), 'w', encoding='utf-8') as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
    print('creds saved -> mi_creds.json (此文件在 .gitignore 中, 勿提交)')


def service_login(s, cookies=None):
    r = s.get('https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true',
              cookies=cookies)
    return json.loads(r.text.replace('&&&START&&&', ''))


def password_auth(s, user, pwd):
    d1 = service_login(s, cookies={'userId': user})
    data = {'sid': 'xiaomiio', '_json': 'true',
            'qs': '%3Fsid%3Dxiaomiio%26_json%3Dtrue',
            '_sign': d1['_sign'], 'user': user,
            'hash': hashlib.md5(pwd.encode()).hexdigest().upper()}
    r = s.post('https://account.xiaomi.com/pass/serviceLoginAuth2', data=data)
    return json.loads(r.text.replace('&&&START&&&', ''))


def chase_location(s, loc, max_hops=15):
    """手动逐跳跟随重定向, 收集每跳 Set-Cookie (serviceToken 藏在中间某跳)"""
    got = {}
    url = loc
    for _ in range(max_hops):
        r = s.get(url, allow_redirects=False)
        for c in r.cookies:
            got[c.name] = c.value
        nxt = r.headers.get('Location')
        if nxt:
            url = nxt
            continue
        body = r.text
        if '&&&START&&&' in body:
            try:
                j = json.loads(body.replace('&&&START&&&', ''))
                got.update({k: j[k] for k in ('userId', 'ssecurity') if j.get(k)})
                if j.get('location'):
                    url = j['location']
                    continue
            except Exception:
                pass
        q = parse_qs(urlparse(str(r.url)).query)
        followed = False
        for key in ('skipUrl', 'callback', '_redirectUrl'):
            if key in q:
                url = unquote(q[key][0])
                followed = True
                break
        if not followed:
            break
    return got


def two_factor(s, user, pwd):
    """securityStatus=16 流程: identity API 发短信 -> 交互输入验证码 -> verify"""
    d2 = password_auth(s, user, pwd)
    nurl = d2.get('notificationUrl')
    assert nurl, 'expected 2FA, got: %s' % {k: d2.get(k) for k in ('code', 'securityStatus')}
    d3 = json.loads(s.get(nurl.replace('/fe/service/identity/authStart', '/identity/list')
                          + ('&' if '?' in nurl else '?') + '_json=true'
                          ).text.replace('&&&START&&&', ''))
    flag = d3.get('flag')
    name = {4: 'Phone', 8: 'Email'}.get(flag)
    assert name, 'unexpected flag %s' % d3
    d4 = json.loads(s.get('https://account.xiaomi.com/identity/auth/verify%s?_flag=%s&_json=true'
                          % (name, flag)).text.replace('&&&START&&&', ''))
    print('验证目标:', d4.get('maskedPhone') or d4.get('maskedEmail'))
    # 注意: verify 参数必须放 query string, 放 form body 会卡进"确认手机"JS 死循环
    while True:
        input('回车发送验证码...')
        r = s.post('https://account.xiaomi.com/identity/auth/send%sTicket' % name,
                   data={'_json': 'true', 'icode': '', 'retry': '0'})
        d5 = json.loads(r.text.replace('&&&START&&&', ''))
        print('send ticket:', d5.get('code'), d5.get('reason') or d5.get('desc'))
        if d5.get('code') == 0:
            break
        print('(限流 code 20024 等 5 分钟再试)')
    ticket = input('输入收到的验证码: ').strip()
    r = s.post('https://account.xiaomi.com/identity/auth/verify%s'
               '?_flag=%s&ticket=%s&trust=false&_json=true' % (name, flag, ticket))
    d = json.loads(r.text.replace('&&&START&&&', ''))
    assert d.get('code') == 0, d
    return d.get('location')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='忽略缓存的 passToken')
    args = ap.parse_args()
    user = os.environ.get('MI_USER')
    pwd = os.environ.get('MI_PASS')
    assert user and pwd, '请设置 MI_USER / MI_PASS 环境变量'

    here = os.path.dirname(os.path.abspath(__file__))
    cached = {}
    cpath = os.path.join(here, 'mi_creds.json')
    if os.path.exists(cpath):
        cached = json.load(open(cpath, encoding='utf-8'))

    s = requests.Session()
    s.headers.update(UA)

    # 快速路径: passToken 续期 (无验证码)
    if cached.get('passToken') and not args.force:
        d = service_login(s, cookies={'userId': user, 'passToken': cached['passToken']})
        if d.get('code') == 0 and d.get('ssecurity'):
            svc = s.cookies.get('serviceToken') or ''
            if not svc and d.get('location'):
                chase_location(s, d['location'])
                svc = s.cookies.get('serviceToken')
            save_creds({'userId': str(d.get('userId', user)),
                        'serviceToken': svc or cached.get('serviceToken', ''),
                        'ssecurity': d['ssecurity'],
                        'passToken': d.get('passToken') or cached['passToken']})
            print('passToken 续期成功 (securityStatus=%s)' % d.get('securityStatus'))
            return
        print('passToken 失效, 走密码登录...')

    d2 = password_auth(s, user, pwd)
    loc = d2.get('location')
    if not loc and d2.get('securityStatus') == 16:
        print('需要二次验证...')
        loc = two_factor(s, user, pwd)
        # 验证成功后重新密码登录拿 location
        d2 = password_auth(s, user, pwd)
        loc = d2.get('location') or loc
    assert loc, ('登录未完成: securityStatus=%s (可能需要浏览器完成确认安全手机, '
                 '打开上方 notificationUrl 处理后重跑)' % d2.get('securityStatus'))

    got = chase_location(s, loc)
    svt = s.cookies.get('serviceToken') or got.get('serviceToken')
    uid = str(got.get('userId') or s.cookies.get('userId'))
    assert svt and uid, '未拿到 serviceToken'
    save_creds({'userId': uid, 'serviceToken': svt,
                'ssecurity': got.get('ssecurity', ''),
                'passToken': got.get('passToken', '')})
    print('LOGIN OK, userId=%s' % uid)


if __name__ == '__main__':
    main()
