#!/bin/bash
# Firefly 服务器版 · 只读生产核实脚本（sanitized）
#
# 设计铁律：
#   1. 只读：仅 ls/stat/find/systemctl show/ss/curl 本地回环/sqlite3 SELECT COUNT 等
#   2. 零原文：不 cat 任何用户文件内容；不打印 token/Key/密码（仅输出"存在/长度/掩码前3位"）
#   3. 零破坏：无 rm/mv/cp/写入/重启/改权限
#   4. 输出仅聚合计数、路径存在性、权限、归属、版本号
#
# 用法（在本地执行，脚本经 stdin 送过去，不在服务器落盘）：
#   ssh -i C:\Users\FANGL\.ssh\id_rsa root@<公网IP> 'bash -s' < server_audit_readonly.sh
#
# 若某些路径不存在，命令会打印"缺失"而不是报错中断。

set -u
R=/opt/firefly
D=/opt/firefly-downloads
line(){ printf '\n===== %s =====\n' "$1"; }
yn(){ [ -e "$1" ] && echo "存在" || echo "缺失"; }

line "1. 线上版本与部署基线"
echo "-- /opt/firefly/version.json 的 tag --"
[ -f "$R/version.json" ] && grep -o '"tag"[^,]*' "$R/version.json" || echo "缺失"
echo "-- /opt/firefly-downloads/version.json 的 tag --"
[ -f "$D/version.json" ] && grep -o '"tag"[^,]*' "$D/version.json" || echo "缺失"
echo "-- version.json 时间戳 --"
for f in "$R/version.json" "$D/version.json"; do [ -f "$f" ] && stat -c '%n  mtime=%y' "$f"; done
echo "-- 下载目录两件套（大小+时间；不打印内容）--"
for f in "$D/firefly.apk" "$D/firefly-setup.exe"; do [ -f "$f" ] && stat -c '%n  %s bytes  mtime=%y' "$f" || echo "$f 缺失"; done
echo "-- 服务状态（active 与否）--"
systemctl is-active firefly-server firefly-downloads 2>&1
echo "-- 监听端口（公网/回环）--"
ss -lnt 2>/dev/null | awk 'NR==1 || /:(8765|8766|8787)/ {print}'
echo "-- 代码目录同步时间（判断部署新鲜度）--"
for f in "$R/app/modules/app_config.py" "$R/app/routes.py" "$R/app/orchestrator.py"; do
  [ -f "$f" ] && stat -c '%n  mtime=%y' "$f" || echo "$f 缺失"
done
echo "-- 线上是否含角色包体系（preset.json 计数 / pack 路由计数）--"
echo "bundled preset.json 数量: $(find "$R/app/assets/character" -maxdepth 2 -name preset.json 2>/dev/null | wc -l)"
echo "routes_pack.py: $(yn "$R/app/routes_pack.py")"
echo "pack_forge.py: $(yn "$R/app/modules/pack_forge.py")"
echo "主体 routes.py 中 '/pack-' 出现次数: $(grep -c '/pack-' "$R/app/routes.py" 2>/dev/null || echo 0)"
echo "-- 关键机制在位判定（只数命中行，不打印源码）--"
echo "orchestrator.py '_PIPELINE_LOG' 命中行数: $(grep -c '_PIPELINE_LOG' "$R/app/orchestrator.py" 2>/dev/null || echo 0)"
echo "app_config.py '_migrate_legacy_layout' 命中行数: $(grep -c '_migrate_legacy_layout' "$R/app/modules/app_config.py" 2>/dev/null || echo 0)"
echo "server_app.py 'X-API-Mode' 命中行数: $(grep -c 'X-API-Mode' "$R/server/server_app.py" 2>/dev/null || echo 0)"
echo "server_app.py 'DEEPSEEK_API_KEY' 命中行数: $(grep -c 'DEEPSEEK_API_KEY' "$R/server/server_app.py" 2>/dev/null || echo 0)"

line "2. 系统环境变量（仅变量名 + 值长度/掩码，绝不打印完整值）"
for v in DEEPSEEK_API_KEY FIREFLY_PROXY_KEY FIREFLY_ADMIN_TOKEN FIREFLY_SERVER FIREFLY_IMAGE_QUOTA_MB; do
  # 从 systemd 单元读（含 Environment= 行），不读进程 environ 全文
  raw=$(systemctl show firefly-server -p Environment 2>/dev/null | tr ' ' '\n' | grep "^$v=" | head -1)
  if [ -n "$raw" ]; then
    val=${raw#*=}
    echo "$v: 已设置，长度=${#val}，掩码=${val:0:3}***"
  else
    echo "$v: 未在 systemd Environment 中设置"
  fi
done

line "3. 多用户数据面（仅计数/权限，不读内容）"
echo "-- user_data 根权限与归属 --"
stat -c '%n  mode=%a  owner=%U:%G' "$R/user_data" 2>/dev/null || echo "user_data 缺失"
echo "-- 账号目录数量（纯数字目录名）--"
ls -1 "$R/user_data" 2>/dev/null | grep -cE '^[0-9]+$'
echo "-- 账号目录权限异常者（非 700/750）--"
for d in "$R/user_data"/*/; do
  [ -d "$d" ] || continue
  m=$(stat -c '%a' "$d" 2>/dev/null)
  case "$m" in 700|750|755|770) ;; *) echo "  $(basename "$d") mode=$m";; esac
done
echo "-- 各账号是否有自建包（计数，不列名）--"
cnt=0
for d in "$R/user_data"/*/; do
  [ -f "$d/character/preset.json" ] && cnt=$((cnt+1))
done
echo "带 character/preset.json 的账号数: $cnt"
echo "-- 各账号快照/备份计数汇总 --"
echo "snapshots/ 总 zip 数: $(find "$R/user_data" -maxdepth 3 -path '*/snapshots/*.zip' 2>/dev/null | wc -l)"
echo "backups/ 总 zip 数:   $(find "$R/user_data" -maxdepth 3 -path '*/backups/*.zip' 2>/dev/null | wc -l)"
echo "  其中 auto- 前缀:    $(find "$R/user_data" -maxdepth 3 -path '*/backups/auto-*.zip' 2>/dev/null | wc -l)"
echo "  其中 pre-restore-: $(find "$R/user_data" -maxdepth 3 -path '*/backups/pre-restore-*.zip' 2>/dev/null | wc -l)"
echo "backups/ 占用字节:    $(du -sb "$R/user_data"/*/backups 2>/dev/null | awk '{s+=$1} END {print s+0}')"
echo "-- 共享根是否残留旧布局（会触发启动迁移）--"
echo "user_data/character/ 存在: $(yn "$R/user_data/character")"
echo "user_data/data/ 存在:      $(yn "$R/user_data/data")"
echo "user_data/story/手账.md 存在: $(yn "$R/user_data/story/手账.md")"

line "4. 托管额度授权（DB 只读 SELECT COUNT）"
DBF="$R/user_data/firefly.db"
if [ -f "$DBF" ]; then
  echo "DB 存在，大小=$(stat -c '%s' "$DBF") bytes"
  echo "-- 账号数与配额分布（分桶计数，不列账号）--"
  sqlite3 -readonly "$DBF" "SELECT 'total_users', COUNT(*) FROM users;" 2>&1
  sqlite3 -readonly "$DBF" "SELECT 'quota>0 的账号数', COUNT(*) FROM users WHERE quota_api_proxy>0;" 2>&1
  sqlite3 -readonly "$DBF" "SELECT 'role=admin 的账号数', COUNT(*) FROM users WHERE role='admin';" 2>&1
  sqlite3 -readonly "$DBF" "SELECT 'schema_version', value FROM settings WHERE key='schema_version';" 2>&1
  echo "-- 托管池当日用量（仅总数）--"
  sqlite3 -readonly "$DBF" "SELECT day, SUM(calls) FROM proxy_usage GROUP BY day ORDER BY day DESC LIMIT 3;" 2>&1
else
  echo "firefly.db 缺失于 $DBF"
fi

line "5. 日志与观测（仅计数）"
echo "server_app.py 中 'RECENT_REQ' 命中行数: $(grep -c 'RECENT_REQ' "$R/server/server_app.py" 2>/dev/null || echo 0)"
echo "近 200 行 journal 中 '迁移'/'migrat' 命中数: $(journalctl -u firefly-server -n 200 --no-pager 2>/dev/null | grep -ciE '迁移|migrat' || echo 0)"
echo "近 200 行 journal 中 'ERROR'/'Traceback' 命中数: $(journalctl -u firefly-server -n 200 --no-pager 2>/dev/null | grep -ciE 'error|traceback' || echo 0)"

line "6. 免登录可达性（仅 HTTP 状态码，不取正文）"
code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$1" 2>/dev/null; }
echo "GET /health                      → $(code http://127.0.0.1:8765/health)"
echo "GET /config（应 401）            → $(code http://127.0.0.1:8765/config)"
echo "GET /pipeline（应 401）          → $(code http://127.0.0.1:8765/pipeline)"
echo "GET /requests（应 401）          → $(code http://127.0.0.1:8765/requests)"
echo "GET /assets/character/story/assets/avatar.png → $(code http://127.0.0.1:8765/assets/character/story/assets/avatar.png)"
echo "GET /assets/index（应 401）      → $(code http://127.0.0.1:8765/assets/index)"
echo "GET /modes（应 401）             → $(code http://127.0.0.1:8765/modes)"

line "7. 回滚资产"
echo "-- /opt/backups 清单（仅名+大小+时间）--"
ls -la /opt/backups 2>/dev/null | tail -n +2 | awk '{print $5, $6, $7, $8, $9}' | head -20 || echo "缺失"
echo "-- /opt/firefly 是否存在 pre_ 前缀备份 --"
find "$R" -maxdepth 2 -name 'pre_*' 2>/dev/null | head -5
echo "-- repo 的 .git 是否存在（决定服务器端是否有版本历史）--"
echo "$R/.git: $(yn "$R/.git")"
echo "-- systemd 单元文件路径 --"
systemctl show firefly-server -p FragmentPath 2>/dev/null
systemctl show firefly-downloads -p FragmentPath 2>/dev/null

line "完成"
echo "本脚本未修改任何文件/配置/数据；未输出任何用户原文、Key、token。"
