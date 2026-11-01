# -*- coding: utf-8 -*-
"""局域网小米设备发现: ping 扫描 + ARP 厂商匹配 + miio 广播 (UDP 54321)
用法: python main.py find-camera
"""
import subprocess, socket, time, re
import concurrent.futures as cf
from mibutler.config import subnets

XIAOMI_OUIS = {
    '28:6C:07','34:80:B3','34:CE:00','64:09:80','74:23:44','78:02:F8','78:11:DC',
    '88:C3:97','8C:BE:BE','A4:77:33','AC:C1:EE','D4:97:0B','EC:D0:9F','F0:B4:29',
    'F4:F5:D8','14:F6:5A','18:B4:30','0C:1D:AF','50:EC:50','04:CF:8C','58:44:98',
    '78:DF:72',  # IMILAB (小米生态摄像头)
}


def ping_one(ip):
    r = subprocess.run(['ping', '-n', '1', '-w', '250', ip], capture_output=True)
    return ip if (r.returncode == 0 or b'TTL' in r.stdout) else None


def sweep(subnet):
    ips = ['%s.%d' % (subnet, i) for i in range(1, 255)]
    alive = []
    with cf.ThreadPoolExecutor(64) as ex:
        for res in ex.map(ping_one, ips):
            if res:
                alive.append(res)
    return alive


def miio_discovery(broadcasts, timeout=7):
    hello = bytes.fromhex('21310020' + 'ff' * 28)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(('', 0))
    for b in broadcasts:
        try:
            s.sendto(hello, (b, 54321))
        except OSError as e:
            print('[miio] %s: %s' % (b, e))
    found = {}
    end = time.time() + timeout
    while time.time() < end:
        s.settimeout(max(0.5, end - time.time()))
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        if addr[0] not in found and len(data) >= 32:
            found[addr[0]] = data
            print('[miio] DEVICE %s  devid=%s' % (addr[0], data[8:12].hex()))
    return found


def parse_arp():
    out = subprocess.run(['arp', '-a'], capture_output=True).stdout.decode('gbk', 'ignore')
    return [(m.group(1), m.group(2).replace('-', ':').upper())
            for m in re.finditer(r'(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F\-]{17})\s+', out)]


def main(argv=None):
    nets = subnets()
    for sn in nets:
        print('[sweep] %s.0/24 ...' % sn)
        alive = sweep(sn)
        print('[sweep] %s alive: %d' % (sn, len(alive)))
    miio = miio_discovery([sn + '.255' for sn in nets] + ['255.255.255.255'])
    print('\n=== ARP ===')
    for ip, mac in parse_arp():
        oui = ':'.join(mac.split(':')[:3])
        tag = ' <-- XIAOMI OUI' if oui in XIAOMI_OUIS else ''
        mt = ' <-- MIIO' if ip in miio else ''
        print('%-16s %s%s%s' % (ip, mac, tag, mt))


if __name__ == '__main__':
    main()
