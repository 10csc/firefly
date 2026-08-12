# -*- coding: utf-8 -*-
"""把已注册手机号提升为管理员

用法：python tools/make_admin.py 13800138000
（必须先注册成功；数据库在 user_data/firefly.db）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from modules import app_config as cfg
import db

if len(sys.argv) != 2:
    print("用法: python tools/make_admin.py <手机号>")
    sys.exit(1)

phone = sys.argv[1].strip()
db.init_db(cfg.USER_DIR / "firefly.db")
user = db.get_user_by_phone(phone)
if not user:
    print(f"[X] 手机号 {phone} 未注册")
    sys.exit(1)
if db.set_admin(user["id"]):
    print(f"[OK] {phone} 已提升为管理员（id={user['id']}）")
else:
    print("[X] 操作失败")
