' 流萤攻击哨兵 · 开机常驻启动（无窗口）
' 平时完全静默；检测到攻击 → 调模型分析 → 弹窗提醒
CreateObject("Wscript.Shell").Run "D:\python_all\Python312\pythonw.exe F:\CodeFile\firefly\tools\attack_sentinel.py", 0, False
