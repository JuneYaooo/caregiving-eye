# -*- coding: utf-8 -*-
"""值班表：执事每天要做的所有事，集中在这一张表里

  sense     看与听：看门狗、动静、检测跟踪、姿态与跌倒、声音事件
  safety    安全：火焰烟雾、入侵
  care      照护：突发不适、儿童危险、卫生间久滞 / 夜间离床、作息偏离、长时间无人
  pets      宠物：禁区、长时间没露面
  records   记录与保养：活动统计、心跳、清理、底图、云台复位与对齐、跌倒样本、日报

优先级：0 看门 / 1 看见 / 2 生命财产 / 3 照护研判 / 4 区域规则 / 6+ 统计（过载时可让路，见 brain/runtime.py）
触发：数字 = 每隔几秒；{等级: 秒} = 随警觉等级变化；None + 'detect' = 每次有新检测结果时
"""
from functools import partial
from mibutler.brain.runtime import Task
from mibutler.brain.gate import IDLE, WATCH, ALERT
from mibutler.butler import AWAY, HOME, PET, NIGHT
from mibutler.config import cfg
from . import sense, safety, care, pets, records

# 各警觉等级下的检测间隔（秒）
DETECT_EVERY = {IDLE: float(cfg('DETECT_IDLE_SEC', 1.5)),
                WATCH: 1 / float(cfg('DETECT_FPS', 3)),
                ALERT: 1 / float(cfg('DETECT_ALERT_FPS', 5))}
FIRE_EVERY = {IDLE: 5.0, WATCH: 3.0, ALERT: 2.0}


def roster(app):
    """返回本次启用的任务列表；可选能力没加载时对应任务不排班"""
    a = lambda fn: partial(fn, app)
    has_persons = lambda c: bool(c.persons)
    zones_ok = lambda c: c.zones_valid
    quiet = lambda c: c.level == IDLE and not c.tracks

    R = [
        #    名称           函数                       间隔          优先级 车道   触发      模式           条件
        Task('watchdog',    a(sense.watchdog),         10,           0, 'cpu', desc='断流 / 线程挂掉自动恢复'),
        Task('motion',      a(sense.motion),           0.2,          0, 'cpu', desc='帧差判断动静，决定警觉等级'),
        Task('detect',      a(sense.detect),           DETECT_EVERY, 1, 'cpu', desc='人 / 猫狗检测 + 跟踪'),
        Task('pose',        a(sense.pose),             None,         1, 'cpu', 'detect', when=has_persons,
             desc='有人才跑姿态：跌倒、久卧'),
        Task('audio',       a(sense.audio_events),     0.25,         2, 'cpu', desc='取出声音事件'),
        Task('intrusion',   a(safety.intrusion),       None,         2, 'cpu', 'detect', (AWAY, PET), has_persons),
        Task('illness',     a(care.illness),           None,         3, 'cpu', 'detect', (HOME, NIGHT), has_persons),
        Task('incidents',   lambda c: app.incidents.tick(), 5,       3, 'cpu', desc='催办与结案'),
        Task('zones',       a(care.person_zones),      None,         4, 'cpu', 'detect', when=zones_ok),
        Task('pet_zones',   a(pets.pet_zones),         None,         4, 'cpu', 'detect', when=zones_ok),
        Task('activity',    a(records.activity),       None,         6, 'cpu', 'detect', when=zones_ok,
             desc='活动统计与热力图，过载时让路'),
        Task('absence',     a(care.absence),           60,           7, 'cpu'),
        Task('pet_unseen',  a(pets.pet_unseen),        300,          7, 'cpu', modes=(PET,)),
        Task('samples',     a(records.samples_flush),  2,            8, 'io', when=lambda c: bool(app.samples.pending),
             desc='跌倒样本写盘'),
        Task('save',        a(records.save),           300,          8, 'io'),
        Task('status',      a(records.status),         30,           8, 'io', desc='心跳'),
        Task('base_image',  a(records.base_image),     600,          9, 'io', when=quiet),
        Task('retention',   a(records.retention),      3600,         9, 'io'),
    ]
    if app.fire:
        R.append(Task('fire', a(safety.fire), lambda c: 0.5 if app.fire.hot else FIRE_EVERY[c.level], 2, 'cpu',
                      desc='闲时 5 秒一次，有可疑立刻加密到 0.5 秒'))
    if app.child:
        R.append(Task('child', a(care.child), None, 3, 'cpu', 'detect',
                      when=lambda c: bool(c.persons) and c.zones_valid))
    if app.fusion:
        R.append(Task('fall_pending', a(sense.fall_pending), 1, 1, 'cpu',
                      when=lambda c: bool(app.fusion.pending) and not c.persons,
                      desc='跌倒待定的人离开画面后照样给出结论'))
    if app.routine:
        R.append(Task('routine', a(care.routine), 600, 7, 'cpu', desc='作息偏离：起床晚、活动少、夜里多动'))
    if any(app.zones.values()) or app.ptz.enabled:
        R.append(Task('view_check', a(records.view_check), 60, 8, 'cpu',
                      when=lambda c: quiet(c) and not app.ptz.busy,
                      desc='画面对齐检查：云台被转过时暂停区域规则'))
    if app.ptz.enabled:
        R.append(Task('ptz_return', a(records.ptz_return), 5, 9, 'io', desc='目标离开后云台复位'))
    return R


DAILY = [
    # 名称            时刻（.env）                  函数
    ('daily_report', lambda: cfg('REPORT_AT', '21:30'), records.daily_report),
]
