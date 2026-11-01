# -*- coding: utf-8 -*-
"""把米执事一天的记录压成一页时间线：先读这个，再决定要不要看图。

用法:
  python day_digest.py [--events-dir events] [--day YYYYMMDD] [--from HH:MM] [--to HH:MM] [--json]

读取（都在 events/ 下，缺哪个跳过哪个）:
  alerts.jsonl      每条通报：时间、级别、类型、文字、快照、录像
  incidents.jsonl   结案事件：持续时长、主人是否确认（acked: false=误报 / true=属实 / ok=收到 / me=是我）
  activity_person_YYYYMMDD.json / activity_pet_...   全天活动统计（没有画面）
  status.json       执事当前状态：模式、画面是否在线

输出: 按时间排序的条目，同一件事的催办 / 重复通报合并成一行，并标出“已确认误报 / 属实 / 未确认”；
     末尾列出当天的快照和录像路径、执事离线时段，方便下一步按需看图。
"""
import argparse, glob, json, os, sys, time
from datetime import datetime

LEVEL_CN = {'CRITICAL': '急报', 'GUARD': '值守', 'PET': '宠物', 'INFO': '通报', 'MORNING': '晨报'}
NOISE = {'incident_close', 'stream_up', 'view_restored'}            # 只是收尾的通报
SYSTEM = {'mode_change', 'supervisor', 'stream_down', 'view_shift', 'daily_report'}
PRESENCE = {'arrival', 'departure'}
ACK_CN = {'false': '主人确认误报', 'true': '主人确认属实', 'ok': '主人已收到', 'me': '主人本人'}


def read_jsonl(path):
    out = []
    for p in sorted(glob.glob(path.replace('.jsonl', '*.jsonl'))):   # 含轮转后的归档
        try:
            for line in open(p, encoding='utf-8'):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
    return out


def in_window(ts, day, t_from, t_to):
    if not ts or not ts.startswith(day):
        return False
    hm = ts[11:16]
    return (not t_from or hm >= t_from) and (not t_to or hm <= t_to)


def kind_of(t):
    if t in PRESENCE:
        return 'presence'
    if t in SYSTEM:
        return 'system'
    return 'event'


def digest(events_dir, day, t_from=None, t_to=None):
    date = '%s-%s-%s' % (day[:4], day[4:6], day[6:8])
    alerts = [a for a in read_jsonl(os.path.join(events_dir, 'alerts.jsonl'))
              if in_window(a.get('ts'), date, t_from, t_to) and a.get('type') not in NOISE]
    incidents = [i for i in read_jsonl(os.path.join(events_dir, 'incidents.jsonl'))
                 if in_window(i.get('opened'), date, t_from, t_to)]

    # 事件的确认结果：按“开始时间 + 类型”关联回通报
    verdicts = []
    for i in incidents:
        t0 = datetime.strptime(i['opened'], '%Y-%m-%d %H:%M:%S').timestamp()
        verdicts.append((t0, t0 + i.get('duration_sec', 0) + 60, set(i.get('types') or []), i))

    def verdict(a):
        t = datetime.strptime(a['ts'], '%Y-%m-%d %H:%M:%S').timestamp()
        for t0, t1, types, i in verdicts:
            if t0 - 5 <= t <= t1 and a.get('type') in types:
                return i
        return None

    # 合并：同类型、间隔 < 15 分钟的连续通报算一件事（催办、重复检测）
    rows = []
    for a in sorted(alerts, key=lambda x: x['ts']):
        t = a.get('type') or a.get('level')
        last = rows[-1] if rows else None
        ts = datetime.strptime(a['ts'], '%Y-%m-%d %H:%M:%S').timestamp()
        if last and last['type'] == t and kind_of(t) == 'event' and ts - last['_t_end'] < 900:
            last['count'] += 1
            last['end'] = a['ts'][11:19]
            last['_t_end'] = ts
            for k in ('snapshot', 'clip'):
                if a.get(k) and a[k] not in last['evidence']:
                    last['evidence'].append(a[k])
            continue
        inc = verdict(a)
        rows.append({'start': a['ts'][11:19], 'end': a['ts'][11:19], '_t_end': ts, 'type': t,
                     'kind': kind_of(t), 'level': LEVEL_CN.get(a.get('level'), a.get('level')),
                     'text': (a.get('text') or '').split(' ', 1)[-1], 'count': 1,
                     'evidence': [x for x in (a.get('snapshot'), a.get('clip')) if x],
                     'verdict': ACK_CN.get(inc.get('acked')) if inc and inc.get('acked') else
                                ('未确认' if kind_of(t) == 'event' and a.get('level') in ('CRITICAL', 'GUARD') else None)})
    for r in rows:
        r.pop('_t_end', None)

    activity = {}
    for kind in ('person', 'pet'):
        p = os.path.join(events_dir, 'activity_%s_%s.json' % (kind, day))
        if os.path.exists(p):
            try:
                s = json.load(open(p, encoding='utf-8'))
                activity[kind] = {k: s.get(k) for k in ('sightings', 'first_seen', 'last_seen',
                                                        'morning_first', 'zones', 'longest_still_sec')}
                activity[kind]['active_min'] = round(s.get('active_sec', 0) / 60)
                activity[kind]['seen_min'] = round(sum(s.get('hourly_seen') or []) / 60)
                activity[kind]['hourly_active_min'] = [round(v / 60, 1) for v in s.get('hourly_active') or []]
            except Exception:
                pass

    media = sorted(os.path.basename(p) for p in glob.glob(os.path.join(events_dir, 'snap_*%s_*.jpg' % day)))
    clips = sorted(os.path.basename(p) for p in glob.glob(os.path.join(events_dir, 'clips', 'clip_*%s*.mp4' % day)))
    status = None
    sp = os.path.join(events_dir, 'status.json')
    if os.path.exists(sp):
        try:
            st = json.load(open(sp, encoding='utf-8'))
            status = {k: st.get(k) for k in ('time', 'mode_cn', 'level', 'level_share', 'uptime_h')}
            status['frames_connected'] = (st.get('frames') or {}).get('connected')
        except Exception:
            pass
    events = [r for r in rows if r['kind'] == 'event']
    return {
        'day': date, 'window': [t_from or '00:00', t_to or '23:59'],
        'counts': {'events': len(events),
                   'confirmed_false': sum(1 for r in events if r['verdict'] == '主人确认误报'),
                   'confirmed_true': sum(1 for r in events if r['verdict'] == '主人确认属实'),
                   'unconfirmed_serious': sum(1 for r in events if r['verdict'] == '未确认'),
                   'presence': sum(1 for r in rows if r['kind'] == 'presence')},
        'timeline': rows, 'activity': activity, 'status': status,
        'snapshots': media, 'clips': clips,
    }


def to_text(d):
    L = ['# %s %s–%s' % (d['day'], *d['window'])]
    c = d['counts']
    L.append('事件 %d 件（确认误报 %d，确认属实 %d，未确认的值守/急报 %d）；进出 %d 次'
             % (c['events'], c['confirmed_false'], c['confirmed_true'], c['unconfirmed_serious'], c['presence']))
    if d['status']:
        s = d['status']
        L.append('执事状态（%s）：%s，警觉 %s，画面%s' % (s.get('time'), s.get('mode_cn'), s.get('level'),
                                                '在线' if s.get('frames_connected') else '离线'))
    L.append('')
    L.append('## 时间线')
    if not d['timeline']:
        L.append('（窗口内没有任何通报）')
    for r in d['timeline']:
        span = r['start'][:5] if r['start'] == r['end'] else '%s–%s' % (r['start'][:5], r['end'][:5])
        extra = []
        if r['count'] > 1:
            extra.append('共 %d 条' % r['count'])
        if r['verdict']:
            extra.append(r['verdict'])
        if r['evidence']:
            extra.append('证据 ' + ', '.join(os.path.basename(x) for x in r['evidence'][:3]))
        L.append('- %s [%s/%s] %s%s' % (span, r['kind'], r['level'], r['text'][:80],
                                         '（%s）' % '；'.join(extra) if extra else ''))
    for kind, label in (('person', '家人'), ('pet', '宠物')):
        a = d['activity'].get(kind)
        if not a:
            continue
        L += ['', '## %s活动（统计，无画面）' % label,
              '出现 %s 次，画面中约 %s 分钟，活跃 %s 分钟，最早 %s，最晚 %s'
              % (a['sightings'], a['seen_min'], a['active_min'], a['first_seen'], a['last_seen'])]
        hours = [h for h, v in enumerate(a['hourly_active_min']) if v > 0]
        if hours:
            L.append('有活动的小时：' + ' '.join('%02d' % h for h in hours))
        if a.get('zones'):
            L.append('区域：' + '，'.join('%s %s 次' % (k, v.get('visits')) for k, v in a['zones'].items()))
    L += ['', '## 证据文件', '快照 %d 张，录像 %d 段' % (len(d['snapshots']), len(d['clips']))]
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--events-dir', default=os.environ.get('EVENTS_DIR', 'events'))
    ap.add_argument('--day', default=time.strftime('%Y%m%d'))
    ap.add_argument('--from', dest='t_from')
    ap.add_argument('--to', dest='t_to')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    if not os.path.isdir(a.events_dir):
        sys.exit('找不到事件目录 %s（用 --events-dir 指定，通常是仓库根目录的 events/）' % a.events_dir)
    d = digest(a.events_dir, a.day.replace('-', ''), a.t_from, a.t_to)
    print(json.dumps(d, ensure_ascii=False, indent=1) if a.json else to_text(d))


if __name__ == '__main__':
    main()
