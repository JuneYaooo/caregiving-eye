# -*- coding: utf-8 -*-
"""共用配置加载: config.json (本地私有, gitignored), 模板见 config.example.json"""
import json, os

_CFG = None


def load_config():
    global _CFG
    if _CFG is None:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, 'config.json')
        if not os.path.exists(path):
            path = os.path.join(here, 'config.example.json')
            print('[config] WARNING: config.json 不存在, 使用 example 模板(功能不可用). '
                  '复制 config.example.json 为 config.json 并填入真实值')
        with open(path, encoding='utf-8') as f:
            _CFG = json.load(f)
    return _CFG


def creds_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mi_creds.json')
