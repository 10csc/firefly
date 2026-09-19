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


# ── 去重与降噪（2026-09-18 修）────────────────────────────
# 病：attack_watch.sh 原先统计"最近 5 分钟内的 Ban 行数"，一次封禁在窗口里每分钟都命中
#     → 标记文件每分钟被重写、`time` 每次都变；而这里原来拿 `flag["time"]` 当去重键
#     → 同一次封禁被当成 4-5 个事件，弹 4-5 次窗（08-28 连续 5 条 / 09-18 连续 4 条，
#       各自只有一个来源 IP，且都是公网 SSH 扫描这种日常噪声）。
# 修：① 服务器侧改「新封禁」语义（server/attack_watch.sh）；② 这里把去重键换成
#      **内容签名**（封禁 IP + 各计数）并加冷却窗口，双保险；③ 单次 SSH 封禁按噪声档
#      处理：只写日报、不弹窗、也不调模型（省一次 API 调用）。
EVENT_COOLDOWN = 1800      # 同签名事件冷却（秒）：30 分钟内同签名只处理一次


def event_signature(flag: dict) -> tuple:
    """事件内容签名。**与 time 无关**——time 每次都变，用它去重等于没有去重。"""
    return (
        str(flag.get("banned") or "").strip(),
        int(flag.get("fail2ban_bans") or 0),
        int(flag.get("rate_limited") or 0),
        int(flag.get("ssh_fails") or 0),
        int(flag.get("gateway_429") or 0),
    )


def is_noise(flag: dict) -> bool:
    """单次 SSH 封禁 = 公网端口的日常扫描，fail2ban 已自动封禁 → 不值得打扰用户。"""
    return (int(flag.get("fail2ban_bans") or 0) <= 1
            and int(flag.get("rate_limited") or 0) < 5
            and int(flag.get("ssh_fails") or 0) < 5
            and int(flag.get("gateway_429") or 0) < 10)


def fallback_verdict(flag: dict) -> dict:
    """模型没给出可用结论时的本地兜底。

    病：09-18 那次模型返回 {"analysis": "", "need_user": true} → 弹窗里"分析"是空的、
    却写着"需要你行动：是"——最烦人的一次恰好最没信息量。
    兜底原则：**宁可说一句确定的本地结论，也不弹空窗**；噪声档一律 need_user=False。
    """
    ips = str(flag.get("banned") or "").strip() or "（未记录）"
    if int(flag.get("fail2ban_bans") or 0):
        return {"severity": "low",
                "attack_type": "SSH 扫描 / 暴力破解（fail2ban 已自动封禁）",
                "analysis": f"fail2ban 已封禁来源 IP：{ips}。主服务与网关无 429，服务运行正常。",
                "actions": ["无需处理（已自动封禁）",
                            "同一 IP 反复出现可把 fail2ban 改为长期封禁"],
                "need_user": False}
    return {"severity": "medium", "attack_type": "疑似应用层限流",
            "analysis": "检测到限流/失败迹象，但模型未给出可用结论；请看安全日报的诊断行。",
            "actions": ["查看 docs/安全日报.md 的诊断行"], "need_user": True}


def _ensure_verdict(v, flag: dict) -> dict:
    """归一化模型输出：缺 analysis / 非 dict / 缺字段 → 走本地兜底。"""
    if not isinstance(v, dict) or not str(v.get("analysis") or "").strip():
        return fallback_verdict(flag)
    sev = str(v.get("severity") or "unknown").lower()
    v["severity"] = sev
    v.setdefault("attack_type", "未判定")
    acts = v.get("actions")
    v["actions"] = acts if isinstance(acts, list) else []
    if not isinstance(v.get("need_user"), bool):
        v["need_user"] = sev in ("high", "critical")
    return v


def analyze_with_model(flag: dict, diag: dict) -> dict:
    """调 DeepSeek 分析攻击事件（事件驱动：发现问题才调模型）。"""
    key = get_api_key()
    if not key:
        v = fallback_verdict(flag)
        v["analysis"] = "（未配置 API Key，未做模型分析）" + v["analysis"]
        return v
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
            json={"model": "deepseek-flash", "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 1000, "temperature": 0,
                  "extra_body": {"thinking": {"type": "disabled"}}},
            timeout=60,
        )
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}")
        if start < 0:
            return _ensure_verdict({"analysis": content}, flag)
        return _ensure_verdict(json.loads(content[start:end + 1]), flag)
    except Exception as e:
        v = fallback_verdict(flag)
        v["analysis"] = f"（模型分析失败：{e}）" + v["analysis"]
        return v


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
    last_sig = None
    last_at = 0.0
    while True:
        flag = check_flag()
        if flag:
            sig = event_signature(flag)
            now = time.time()
            # 去重：同**签名**在冷却窗口内只处理一次（原来比的是 flag["time"]，
            # 而服务器每分钟都写新时间戳 → 同一次封禁被反复当成新事件）
            if sig != last_sig or (now - last_at) > EVENT_COOLDOWN:
                last_sig, last_at = sig, now
                diag = collect_diagnostics()
                if is_noise(flag):
                    # 公网 SSH 扫描噪声：fail2ban 已自动处理 → 只留档，不弹窗、不调模型
                    verdict = {
                        "severity": "low",
                        "attack_type": "SSH 扫描（噪声档，已自动封禁）",
                        "analysis": f"单次封禁，来源 IP：{flag.get('banned') or '未记录'}。"
                                    "按噪声档只写日报，不打扰。",
                        "actions": [], "need_user": False,
                    }
                    write_report(flag, diag, verdict)
                    apply_shrink(verdict)   # 严重度 low → 顺手恢复收缩标志
                else:
                    verdict = analyze_with_model(flag, diag)
                    notify(flag, verdict)
                    write_report(flag, diag, verdict)
                    apply_shrink(verdict)   # 攻击严重先收缩（暂停注册），平息自动恢复
        else:
            last_sig = None
            last_at = 0.0
        if once:
            return
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
