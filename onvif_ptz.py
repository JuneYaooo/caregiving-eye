# -*- coding: utf-8 -*-
"""ONVIF 云台控制 —— 经 xiaomi-onvif 桥 -> go2rtc -> 摄像头
重要: 云台命令必须在视频流活跃时发 (有 RTSP 消费者在拉流), 否则桥报 "no active connection"
用法: python onvif_ptz.py right 1.0   (left/right/up/down + 秒数; stop; test)
"""
import sys, time, re, os
import requests
from common_config import cfg

BASE = cfg('ONVIF_BASE', 'http://127.0.0.1:8891')


def soap(service, body, action):
    env = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           '<s:Body>%s</s:Body></s:Envelope>' % body)
    r = requests.post(BASE + service, data=env, headers={
        'Content-Type': 'application/soap+xml; charset=utf-8; action="%s"' % action}, timeout=8)
    return r.text


def get_profile_token():
    t = soap('/onvif/media_service', '<GetProfiles xmlns="http://www.onvif.org/ver20/media/wsdl"/>',
             'http://www.onvif.org/ver20/media/wsdl/GetProfiles')
    m = re.search(r'token="([^"]+)"', t)
    return m.group(1) if m else None


def continuous_move(token, x=0.0, y=0.0):
    body = ('<ContinuousMove xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            '<ProfileToken>%s</ProfileToken>'
            '<Velocity><PanTilt x="%.3f" y="%.3f" xmlns="http://www.onvif.org/ver10/schema"/></Velocity>'
            '</ContinuousMove>' % (token, x, y))
    return soap('/onvif/ptz_service', body, 'http://www.onvif.org/ver20/ptz/wsdl/ContinuousMove')


def stop(token):
    body = ('<Stop xmlns="http://www.onvif.org/ver20/ptz/wsdl">'
            '<ProfileToken>%s</ProfileToken><PanTilt>true</PanTilt><Zoom>false</Zoom>'
            '</Stop>' % token)
    return soap('/onvif/ptz_service', body, 'http://www.onvif.org/ver20/ptz/wsdl/Stop')


def move(action, duration=1.0, speed=0.5):
    """带兜底停止的封装; action: left/right/up/down"""
    tok = get_profile_token()
    dx = {'left': -speed, 'right': speed}.get(action, 0)
    dy = {'up': speed, 'down': -speed}.get(action, 0)
    continuous_move(tok, dx, dy)
    try:
        time.sleep(duration)
    finally:
        stop(tok)
    return True


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'test'
    tok = get_profile_token()
    print('profile token:', tok)
    if action == 'test':
        print(continuous_move(tok, 0.5, 0)[:160])
        time.sleep(0.8)
        print(stop(tok)[:160])
    elif action == 'stop':
        print(stop(tok)[:160])
    elif action in ('left', 'right', 'up', 'down'):
        dur = float(sys.argv[2]) if len(sys.argv) > 2 else 0.8
        move(action, dur)
        print('moved', action, dur)
