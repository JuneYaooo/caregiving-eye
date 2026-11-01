# -*- coding: utf-8 -*-
"""米执事 · Mi Butler

分层（上层只依赖下层，详见 docs/architecture.md）：

  duties/     值班任务：每项任务做什么、多久做一次（值班表在 duties/__init__.py）
  app.py      组装：加载模型、启动服务、把信号交给研判层、主循环
  brain/      调度与研判：调度器、拉流、警觉等级、事件研判、云台、保养、守护
  abilities/  能力：每个模块只做一种判断，返回结果，不直接发通知
  vision/     模型：YOLO 推理、跟踪器
  camera/     接入：米家登录、设备查询、局域网发现、ONVIF 云台
  butler.py   执事：值班模式、通报话术、推送、主人指令
  report.py   日报
  zones.py    区域标定
  config.py   读取 .env
"""
