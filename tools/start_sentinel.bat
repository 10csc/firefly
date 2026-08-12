@echo off
rem 流萤攻击哨兵 · 开机常驻启动（pythonw 无窗口运行）
rem 平时完全静默；检测到攻击 → 调模型分析 → 弹窗提醒
start "" "D:\python_all\Python312\pythonw.exe" "F:\CodeFile\firefly\tools\attack_sentinel.py"
