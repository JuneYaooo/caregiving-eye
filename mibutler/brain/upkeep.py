# -*- coding: utf-8 -*-
"""长期运行的保养：磁盘、日志、心跳

  retention   快照 / 录像 / 日报图片保留 RETAIN_DAYS 天，总量超过 MAX_EVENTS_GB 时从最旧的删起。
              活动统计（json / npy，很小）保留 400 天，用于将来做作息对比。
  rotate_log  alerts.jsonl / incidents.jsonl 超过 20 MB 改名归档。
  write_status 每 30 秒写一次 events/status.json：模式、警觉等级、各任务耗时、服务健康、未结事件。
              外部机器人或面板读这个文件即可知道执事是否在岗。
"""
import json, os, time

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


def retention(events_dir, keep_days=7, max_gb=5.0, stats_days=400):
    now = time.time()
    media, removed = [], 0
    for p, f, mt, sz in _walk(events_dir):
        if f.startswith(MEDIA_PREFIX):
            if now - mt > keep_days * 86400:
                os.remove(p)
                removed += 1
            else:
                media.append((mt, sz, p))
        elif f.startswith(STATS_PREFIX) and now - mt > stats_days * 86400:
            os.remove(p)
            removed += 1
    total = sum(m[1] for m in media)
    limit = max_gb * 1024 ** 3
    for mt, sz, p in sorted(media):
        if total <= limit:
            break
        try:
            os.remove(p)
            total -= sz
            removed += 1
        except OSError:
            pass
    return {'removed': removed, 'media_mb': round(total / 1024 ** 2, 1)}


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
