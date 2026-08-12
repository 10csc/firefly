# -*- coding: utf-8 -*-
"""本地攻击哨兵（代码型，事件驱动唤醒模型）

服务器 attack_watch.sh（cron，每分钟）检测攻击迹象写标记文件（建立信号）；
本脚本（用户电脑常驻，每分钟）：
  无标记 → 完全静默（零输出零打扰）
  有标记 → ① 收集诊断数据（SSH）
          ② 调 DeepSeek 模型分析（攻击类型/严重程度/建议行动）
          ③ 弹窗展示模型结论 + 追加写入 docs/安全日报.md
          （同一攻击事件只处理一次，攻击平息后重置）

用法：
  python tools/attack_sentinel.py          # 常驻
  python tools/attack_sentinel.py --once   # 单次检测

依赖：paramiko、requests
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import paramiko
import requests

HOST = "101.200.14.126"
KEY_FILE = r"C:\Users\FANGL\.ssh\id_rsa"
FLAG_FILE = "/opt/firefly/user_data/attack_flag.json"
INTERVAL = 60
REPORT = Path(r"F:\CodeFile\firefly\docs\安全日报.md")

_ssh = None


def ssh() -> paramiko.SSHClient:
    global _ssh
    if _ssh is None:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(HOST, username="root", key_filename=KEY_FILE, timeout=10)
        _ssh = c
    return _ssh


def run(cmd: str, timeout: int = 10) -> str:
    try:
        _, out, err = ssh().exec_command(cmd, timeout=timeout)
        return (out.read().decode("utf-8", errors="replace") or "").strip()
    except Exception:
        return ""


def get_api_key() -> str:
    k = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if k:
        return k
    try:
        cfg = json.loads(Path(r"F:\CodeFile\firefly\user_data\config.json").read_text(encoding="utf-8"))
        return (cfg.get("api_key") or "").strip()
    except Exception:
        return ""


def check_flag() -> dict | None:
    data = run(f"cat {FLAG_FILE} 2>/dev/null")
    if not data:
        return None
    try:
        return json.loads(data)
    except Exception:
        return None


def collect_diagnostics() -> dict:
    """收集攻击诊断数据（代码能查的原始信息，喂给模型分析）。"""
    return {
        "fail2ban": run("fail2ban-client status sshd 2>/dev/null | head -10"),
        "gateway_429_30m": run("journalctl -u firefly-downloads --since '30 minutes ago' --no-pager 2>/dev/null | grep -c '429'"),
        "api_429_30m": run("journalctl -u firefly-server --since '30 minutes ago' --no-pager 2>/dev/null | grep -c '429\\|过于频繁'"),
        "ssh_fails_30m": run("journalctl -u ssh --since '30 minutes ago' --no-pager 2>/dev/null | grep -c 'Failed password'"),
        "recent_errors": run("journalctl -u firefly-server --since '30 minutes ago' --no-pager 2>/dev/null | grep -E 'Traceback|Exception' | tail -3"),
        "services": run("systemctl is-active firefly-server firefly-downloads fail2ban 2>&1"),
    }


def analyze_with_model(flag: dict, diag: dict) -> dict:
    """调 DeepSeek 分析攻击事件（事件驱动：发现问题才调模型）。"""
    key = get_api_key()
    if not key:
        return {"severity": "unknown", "attack_type": "未知", "analysis": "（未配置 API Key，无法调模型分析）",
                "actions": ["配置 DEEPSEEK_API_KEY 后哨兵可自动分析"], "need_user": True}
    prompt = (
        "你是流萤服务器的安全分析师。服务器刚检测到攻击事件，数据如下：\n"
        f"攻击标记: {json.dumps(flag, ensure_ascii=False)}\n"
        f"诊断数据: {json.dumps(diag, ensure_ascii=False)}\n"
        "请输出 JSON（不要其他内容）："
        '{"severity":"low|medium|high|critical","attack_type":"判断的攻击类型",'
        '"analysis":"2-3句分析","actions":["建议行动，最多3条"],"need_user":true/false}'
    )
    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 1000, "temperature": 0,
                  "extra_body": {"thinking": {"type": "disabled"}}},
            timeout=60,
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}")
        return json.loads(content[start:end + 1]) if start >= 0 else {"analysis": content, "need_user": True}
    except Exception as e:
        return {"severity": "unknown", "attack_type": "未知", "analysis": f"模型分析失败: {e}",
                "actions": ["手动查看服务器日志"], "need_user": True}


def notify(flag: dict, verdict: dict):
    """弹窗展示模型分析结论（PowerShell MsgBox，EncodedCommand 防转义）。"""
    actions = "\n".join(f"· {a}" for a in verdict.get("actions", [])) or "· 无"
    brief = (
        f"⚠️ 流萤服务器检测到攻击！\n"
        f"时间：{flag.get('time', '?')}\n"
        f"严重程度：{verdict.get('severity', '?')}\n"
        f"攻击类型：{verdict.get('attack_type', '?')}\n"
        f"分析：{verdict.get('analysis', '?')}\n"
        f"建议：\n{actions}\n"
        f"需要你行动：{'是' if verdict.get('need_user') else '否'}"
    )
    ps = (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"[Microsoft.VisualBasic.Interaction]::MsgBox('{brief.replace(chr(39), chr(39)+chr(39))}','Exclamation','流萤安全警报')"
    )
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    subprocess.Popen(["powershell", "-NoProfile", "-EncodedCommand", encoded],
                     creationflags=subprocess.CREATE_NO_WINDOW)


def write_report(flag: dict, diag: dict, verdict: dict):
    """事件详情追加写入安全日报（供 21:00 巡检与用户查阅）。"""
    try:
        section = (
            f"\n### 攻击事件 {flag.get('time', '?')}\n"
            f"- 标记: {json.dumps(flag, ensure_ascii=False)}\n"
            f"- 模型分析: {json.dumps(verdict, ensure_ascii=False)}\n"
            f"- 诊断: fail2ban=[{diag.get('fail2ban', '')}] 网关429=[{diag.get('gateway_429_30m', '')}] "
            f"API429=[{diag.get('api_429_30m', '')}] SSH失败=[{diag.get('ssh_fails_30m', '')}]\n"
            f"- 服务: {diag.get('services', '')}\n"
        )
        with REPORT.open("a", encoding="utf-8") as f:
            f.write(section)
    except Exception:
        pass


def apply_shrink(verdict: dict) -> None:
    """攻击应对（收缩服务）：severity high/critical → 服务器暂停注册/发码；
    其余（含 unknown，避免误伤）→ 恢复。收缩标志 = /opt/firefly/.shrink。"""
    sev = str(verdict.get("severity", "unknown")).lower()
    if sev in ("high", "critical"):
        run("touch /opt/firefly/.shrink")
    else:
        run("rm -f /opt/firefly/.shrink")


def main():
    once = "--once" in sys.argv
    last_time = None
    while True:
        flag = check_flag()
        if flag:
            t = flag.get("time")
            if t != last_time:          # 同一攻击不重复处理
                diag = collect_diagnostics()
                verdict = analyze_with_model(flag, diag)
                notify(flag, verdict)
                write_report(flag, diag, verdict)
                apply_shrink(verdict)   # 攻击严重先收缩（暂停注册），平息自动恢复
                last_time = t
        else:
            last_time = None
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
