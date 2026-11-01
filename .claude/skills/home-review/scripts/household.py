# -*- coding: utf-8 -*-
"""家庭知识 events/household.json：执事和 AI 共用的“这个家的固定情况”。

执事会用 static_objects（标过的静物区里没走过路的人形不算人，立即生效）；其余字段给 AI 复盘时参考。
每次修改都记进 changelog（时间、做了什么、谁提出的），可以随时撤回。

用法:
  show
  add-object  --name 石膏像 --roi 0.05,0.2,0.25,0.8 [--note ...]     静物区，roi 为 0~1 相对坐标
  add-lighting --time 15:00-16:30 [--roi ...] --note '阳光照到餐桌'
  add-fact    '大门不在画面里，有人回来时先出现在画面右侧' [--topic camera|routine|other]
  set         camera.covers_door false | routine.wake 06:30 | routine.sleep 22:00
  remove      --name 石膏像 | --fact 2 | --lighting 0
  公共参数：--events-dir events  --by owner|ai（默认 owner；AI 代为修改时写 ai，并且要先征得主人同意）
"""
import argparse, json, os, sys, time

EMPTY = {'static_objects': [], 'camera': {}, 'routine': {}, 'lighting': [], 'facts': [], 'changelog': []}


def load(path):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return {**EMPTY, **json.load(f)}
    return json.loads(json.dumps(EMPTY))


def save(path, d):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _roi(s):
    if not s:
        return None
    v = [float(x) for x in s.split(',')]
    if len(v) != 4 or not all(0 <= x <= 1 for x in v) or v[0] >= v[2] or v[1] >= v[3]:
        sys.exit('--roi 需要 x1,y1,x2,y2 四个 0~1 的数，且 x1<x2、y1<y2')
    return v


def _value(s):
    low = s.lower()
    if low in ('true', 'false'):
        return low == 'true'
    try:
        return float(s) if '.' in s else int(s)
    except ValueError:
        return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--events-dir', default=os.environ.get('EVENTS_DIR', 'events'))
    ap.add_argument('--by', default='owner', choices=('owner', 'ai'))
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('show')
    p = sub.add_parser('add-object'); p.add_argument('--name', required=True); p.add_argument('--roi', required=True); p.add_argument('--note', default='')
    p = sub.add_parser('add-lighting'); p.add_argument('--time', required=True); p.add_argument('--roi'); p.add_argument('--note', default='')
    p = sub.add_parser('add-fact'); p.add_argument('text'); p.add_argument('--topic', default='other')
    p = sub.add_parser('set'); p.add_argument('key'); p.add_argument('value')
    p = sub.add_parser('remove'); p.add_argument('--name'); p.add_argument('--fact', type=int); p.add_argument('--lighting', type=int)
    a = ap.parse_args(argv)

    path = os.path.join(a.events_dir, 'household.json')
    d = load(path)
    if a.cmd == 'show':
        print(json.dumps({k: v for k, v in d.items() if k != 'changelog'}, ensure_ascii=False, indent=1))
        if d['changelog']:
            print('最近修改：')
            for c in d['changelog'][-5:]:
                print('  %s [%s] %s' % (c['time'], c['by'], c['what']))
        return

    stamp = time.strftime('%Y-%m-%d %H:%M')
    if a.cmd == 'add-object':
        roi = _roi(a.roi)
        d['static_objects'] = [o for o in d['static_objects'] if o.get('name') != a.name]
        d['static_objects'].append({'name': a.name, 'roi': roi, 'note': a.note, 'added': stamp, 'by': a.by})
        what = '静物「%s」%s' % (a.name, roi)
    elif a.cmd == 'add-lighting':
        d['lighting'].append({'time': a.time, 'roi': _roi(a.roi), 'note': a.note, 'added': stamp, 'by': a.by})
        what = '光照规律 %s %s' % (a.time, a.note)
    elif a.cmd == 'add-fact':
        d['facts'].append({'text': a.text, 'topic': a.topic, 'added': stamp, 'by': a.by})
        what = '事实：%s' % a.text
    elif a.cmd == 'set':
        sect, _, key = a.key.partition('.')
        if sect not in ('camera', 'routine') or not key:
            sys.exit('set 只支持 camera.<键> 或 routine.<键>')
        d[sect][key] = _value(a.value)
        what = '%s = %s' % (a.key, a.value)
    else:
        if a.name:
            before = len(d['static_objects'])
            d['static_objects'] = [o for o in d['static_objects'] if o.get('name') != a.name]
            if len(d['static_objects']) == before:
                sys.exit('没有叫「%s」的静物' % a.name)
            what = '移除静物「%s」' % a.name
        elif a.fact is not None:
            what = '移除事实：%s' % d['facts'].pop(a.fact)['text']
        elif a.lighting is not None:
            what = '移除光照规律：%s' % d['lighting'].pop(a.lighting)['time']
        else:
            sys.exit('remove 需要 --name / --fact / --lighting')
    d['changelog'].append({'time': stamp, 'by': a.by, 'what': what})
    d['changelog'] = d['changelog'][-200:]
    save(path, d)
    print('已更新 %s：%s（执事一分钟内自动生效）' % (path, what))


if __name__ == '__main__':
    main()
