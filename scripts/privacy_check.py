# -*- coding: utf-8 -*-
"""提交前隐私检查：拦住家里的数据、照片和凭据（.githooks/pre-commit 调用）

启用（每个克隆执行一次）：git config core.hooksPath .githooks
手动检查暂存区：python scripts/privacy_check.py

.gitignore 已经忽略了这些文件，但 `git add -f`、改名、换目录都能绕过它，这里再拦一道。
确实要提交被拦的内容时，可以用 `git commit --no-verify` 跳过（请先确认里面没有隐私）。
"""
import fnmatch, re, subprocess, sys

BLOCK_PATHS = [
    'events/*', '*/events/*', '*household*.json', 'labels*.jsonl', 'feedback*.jsonl', 'alerts*.jsonl',
    'incidents*.jsonl', 'furniture_cells*.json', 'status.json', '.env', '.env.*', 'mi_creds*.json',
    'mi_devices*.json', 'mi_login*.json', 'mi_2fa*.json', '.command_token', 'go2rtc.yaml', 'zones.json',
    '*_ref.jpg', 'snap_*', 'clip_*', 'base_*.jpg', 'heatmap_*', 'activity_*.json', 'report_*.md',
    '*.mp4', '*.npz', '*.npy', '*.pcapng', '*.etl',
]
ALLOW_PATHS = ['.env.example', 'docs/assets/*', 'zones.example.json']
IMAGE = re.compile(r'\.(jpe?g|png|bmp|webp|heic)$', re.I)
SECRETS = [
    (re.compile(r'(passToken|serviceToken|ssecurity)["\']?\s*[:=]\s*["\']?[A-Za-z0-9/+=_\-]{16,}'), '米家凭据'),
    (re.compile(r'access_token=[A-Za-z0-9]{20,}'), 'webhook 口令'),
    (re.compile(r'rtsp://[^:/\s]+:[^@/\s]+@'), '带密码的 RTSP 地址'),
    (re.compile(r'(?<![\d.])(192\.168|10\.\d{1,3}|172\.(1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])'), '局域网 IP'),
    (re.compile(r'[A-Za-z]:\\Users\\[^\\\s]+'), 'Windows 用户目录'),
]
PLACEHOLDER_IPS = {'192.168.1.123', '192.168.1.100', '192.168.1.1', '10.0.0.1'}


def staged():
    out = subprocess.run(['git', 'diff', '--cached', '--name-only', '--diff-filter=ACMR'],
                         capture_output=True, text=True).stdout
    return [p for p in out.splitlines() if p]


def blocked(path):
    name = path.split('/')[-1]
    if any(fnmatch.fnmatch(path, a) for a in ALLOW_PATHS):
        return None
    for pat in BLOCK_PATHS:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(name, pat):
            return '家里的运行数据 / 凭据文件（%s）' % pat
    if IMAGE.search(name):
        return '图片（可能拍到家里）；文档配图请放 docs/assets/'
    return None


def scan(path):
    blob = subprocess.run(['git', 'show', ':' + path], capture_output=True).stdout
    if b'\0' in blob[:4096]:
        return []
    text = blob.decode('utf-8', 'replace')
    hits = []
    for rx, what in SECRETS:
        for m in rx.finditer(text):
            if what == '局域网 IP' and m.group(0) in PLACEHOLDER_IPS:
                continue
            line = text.count('\n', 0, m.start()) + 1
            hits.append('%s:%d %s' % (path, line, what))
    return hits


def main():
    problems = []
    for p in staged():
        why = blocked(p)
        if why:
            problems.append('%s ← %s' % (p, why))
        elif p != 'scripts/privacy_check.py':
            problems += ['%s ← 疑似%s' % (h.split(' ')[0], h.split(' ', 1)[1]) for h in scan(p)]
    if problems:
        print('隐私检查没通过，以下内容不应提交到公开仓库：')
        for x in problems:
            print('  - ' + x)
        print('移出暂存区：git restore --staged <文件>；确认无隐私仍要提交：git commit --no-verify')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
