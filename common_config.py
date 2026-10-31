# -*- coding: utf-8 -*-
"""共用配置: 本地 .env (gitignored), 模板见 .env.example"""
import os
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, '.env'))


def cfg(key, default=''):
    return os.environ.get(key, default)


def subnets():
    return [s.strip() for s in cfg('SUBNETS', '192.168.1').split(',') if s.strip()]


def creds_path():
    return os.path.join(_HERE, 'mi_creds.json')
