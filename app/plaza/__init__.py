# -*- coding: utf-8 -*-
"""app/plaza — 角色卡共创平台（M1 起）

- card_format.py  角色卡归档 card.zip 的构建与校验（契约见 docs/设计/角色卡共创平台/01）
- card_payload.py 前端 payload、data URL 与文件/图片 patch 的格式转换
- http_transport.py 响应、查询、本地代理与当前用户解析
- store.py        （M1）服务器端广场数据根与卡库读写
- api.py          （M1）广场端点与既有 helper 的兼容 re-export

模块铁律：本目录只处理**角色卡**（character/ 子树），永不读写用户数据
（data/ journal/ images/）——见 docs/设计/角色卡共创平台/01 §五。
"""
