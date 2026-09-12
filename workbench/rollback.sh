#!/usr/bin/env bash
set -euo pipefail
/root/YuE/workbench/control.sh stop
cd /root/YuE
tar xzf /root/yue2-backups/20260912-workbench/original-code.tgz ./start.sh
cp /root/yue2-backups/20260912-workbench/autodl.sh /etc/autodl.sh
setsid nohup /root/YuE/start.sh >> /tmp/yue2-boot.log 2>&1 < /dev/null &
echo '已恢复原 Gradio 入口。工作台作品、数据库和加密密钥均保留。'
