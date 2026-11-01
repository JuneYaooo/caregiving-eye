# -*- coding: utf-8 -*-
"""共用配置：读取仓库根目录的 .env（不提交），模板见 .env.example"""
import os
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根目录
load_dotenv(os.path.join(ROOT, '.env'))


def cfg(key, default=''):
    return os.environ.get(key, default)


def subnets():
    return [s.strip() for s in cfg('SUBNETS', '192.168.1').split(',') if s.strip()]


def creds_path():
    return os.path.join(ROOT, 'mi_creds.json')
