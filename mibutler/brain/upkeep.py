# -*- coding: utf-8 -*-
"""长期运行的保养：磁盘、日志、心跳

  retention   快照 / 录像 / 日报图片保留 RETAIN_DAYS 天，总量超过 MAX_EVENTS_GB 时从最旧的删起。
              活动统计（json / npy，很小）保留 400 天，用于将来做作息对比。
              主人标过“没事 / 属实”的事件，其快照和录像是坏例证据，不按天数删，
              单独受 CASES_MAX_GB 限制（超了从最旧的删），以后调阈值时可以拿来回放验证。
  rotate_log  alerts.jsonl / incidents.jsonl 超过 20 MB 改名归档。
  write_status 每 30 秒写一次 events/status.json：模式、警觉等级、各任务耗时、服务健康、未结事件。
              外部机器人或面板读这个文件即可知道执事是否在岗。
"""
import glob, json, os, time

MEDIA_PREFIX = ('snap_', 'clip_', 'report_heatmap_', 'base_')
STATS_PREFIX = ('activity_', 'heatmap_')


def _walk(root):
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            yield p, f, st.st_mtime, st.st_size


def _read_jsonl(path):
    out = []
    try:
        for line in open(path, encoding='utf-8'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def labeled_media(events_dir):
    """已标注（误报 / 属实）事件引用的快照、录像文件名"""
    labeled = {l.get('incident') for l in _read_jsonl(os.path.join(events_dir, 'labels.jsonl'))
               if l.get('kind') in ('false', 'true')}
    names = set()
    for p in glob.glob(os.path.join(events_dir, 'incidents*.jsonl')):
        for inc in _read_jsonl(p):
            if inc.get('acked') in ('false', 'true') or inc.get('id') in labeled:
                names.update(os.path.basename(x) for x in (inc.get('snapshot'), inc.get('clip')) if x)
    return names


def retention(events_dir, keep_days=7, max_gb=5.0, stats_days=400, cases_max_gb=2.0):
    now = time.time()
    media, cases, removed = [], [], 0
    keep = labeled_media(events_dir)
    for p, f, mt, sz in _walk(events_dir):
        if f in keep:
            cases.append((mt, sz, p))
        elif f.startswith(MEDIA_PREFIX):
            if now - mt > keep_days * 86400:
                os.remove(p)
                removed += 1
            else:
                media.append((mt, sz, p))
        elif f.startswith(STATS_PREFIX) and now - mt > stats_days * 86400:
            os.remove(p)
            removed += 1
    left = 0
    for files, gb in ((media, max_gb), (cases, cases_max_gb)):
        total = sum(m[1] for m in files)
        for mt, sz, p in sorted(files):
            if total <= gb * 1024 ** 3:
                break
            try:
                os.remove(p)
                total -= sz
                removed += 1
            except OSError:
                pass
        left += total
    return {'removed': removed, 'media_mb': round(left / 1024 ** 2, 1),
            'cases_mb': round(sum(m[1] for m in cases if os.path.exists(m[2])) / 1024 ** 2, 1)}


def rotate_log(path, max_mb=20):
    if os.path.exists(path) and os.path.getsize(path) > max_mb * 1024 ** 2:
        base, ext = os.path.splitext(path)
        os.replace(path, '%s_%s%s' % (base, time.strftime('%Y%m%d_%H%M%S'), ext))
        return True
    return False


def write_status(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
