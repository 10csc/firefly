#!/usr/bin/env bash
# 流萤服务器数据备份：打包 user_data（含 firefly.db、各账号对话/记忆/手账）→ 保留最近 7 份。
# 服务器不落 API Key（内存即弃），备份不含任何用户 Key。
# 部署：scp 到服务器 /opt/firefly/tools/，然后：
#   crontab -e  加一行：  30 4 * * * /opt/firefly/tools/backup_server.sh >> /var/log/firefly-backup.log 2>&1
set -euo pipefail

DATA_DIR=/opt/firefly/user_data
BACKUP_DIR=/opt/firefly-backups
KEEP=7                      # 保留最近 N 份
TS=$(date +%Y%m%d-%H%M%S)

mkdir -p "$BACKUP_DIR"
if [ ! -d "$DATA_DIR" ]; then
    echo "$(date) 跳过：$DATA_DIR 不存在"
    exit 0
fi

tar -czf "$BACKUP_DIR/firefly-userdata-$TS.tar.gz" -C /opt/firefly user_data

# 清理：只保留最近 KEEP 份
ls -1t "$BACKUP_DIR"/firefly-userdata-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "$(date) 备份完成：firefly-userdata-$TS.tar.gz"
