# -*- coding: utf-8 -*-
"""本地攻击哨兵（代码型，替代 ZCode 定时任务）

服务器侧 attack_watch.sh 每分钟检测攻击迹象并写标记文件（建立信号）；
本脚本（用户电脑常驻）每分钟读取标记——发现攻击立即弹窗提醒，平时完全静默。

用法：
  python tools/attack_sentinel.py          # 常驻运行（建议开机自启）
  python tools/attack_sentinel.py --once   # 单次检测（测试用，静默）

依赖：paramiko（已安装）
"""
import base64
import json
import subprocess
import sys
import time

import paramiko

HOST = "101.200.14.126"
KEY_FILE = r"C:\Users\FANGL\.ssh\id_rsa"
FLAG_FILE = "/opt/firefly/user_data/attack_flag.json"
INTERVAL = 60  # 秒


def check_flag() -> dict | None:
    """SSH 读取攻击标记。网络异常/无标记返回 None（静默等下一轮）。"""
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(HOST, username="root", key_filename=KEY_FILE, timeout=10)
        _, out, _ = c.exec_command(f"cat {FLAG_FILE} 2>/dev/null", timeout=10)
        data = out.read().decode("utf-8", errors="replace").strip()
        c.close()
        return json.loads(data) if data else None
    except Exception:
        return None


def notify(flag: dict):
    """Windows 弹窗提醒 + 提示音（PowerShell MsgBox，EncodedCommand 防转义问题）。"""
    brief = (
        f"流萤服务器可能遭受攻击！\n"
        f"时间：{flag.get('time', '?')}\n"
        f"fail2ban 封禁：{flag.get('fail2ban_bans', 0)} 次\n"
        f"限流触发：{flag.get('rate_limited', 0)} 次\n"
        f"SSH 爆破尝试：{flag.get('ssh_fails', 0)} 次\n"
        f"网关 429：{flag.get('gateway_429', 0)} 次"
    )
    ps = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"[Microsoft.VisualBasic.Interaction]::MsgBox('{brief.replace(chr(39), chr(39)+chr(39))}','Exclamation','流萤安全警报')"
    )
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def main():
    once = "--once" in sys.argv
    last_time = None
    while True:
        flag = check_flag()
        if flag:
            t = flag.get("time")
            if t != last_time:          # 同一攻击不重复提醒，新事件才弹
                notify(flag)
                last_time = t
        else:
            last_time = None            # 攻击平息，重置（下次攻击可再提醒）
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
