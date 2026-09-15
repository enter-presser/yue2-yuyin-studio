#!/usr/bin/env bash
set -euo pipefail
APP=/root/YuE/workbench
DATA=/root/autodl-tmp/yue2-studio
PID="$DATA/service.pid"
mkdir -p "$DATA"
running() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null && [ "$(readlink "/proc/$(cat "$PID")/cwd")" = "$APP" ] && tr '\0' ' ' < "/proc/$(cat "$PID")/cmdline" | grep -q 'uvicorn server:app'; }
case "${1:-status}" in
start)
  if running; then echo "工作台已运行 PID $(cat "$PID")"; exit 0; fi
  setsid nohup "$APP/run.sh" >> "$DATA/service.log" 2>&1 < /dev/null &
  echo "$!" > "$PID"
  echo "工作台已启动 PID $(cat "$PID")，端口 ${STUDIO_PORT:-6006}"
  ;;
stop)
  if running; then
    kill -TERM "$(cat "$PID")"
    for n in $(seq 1 30); do running || break; sleep 1; done
    if running; then echo '服务仍在关闭，请检查日志；未强制终止。'; exit 1; fi
  fi
  rm -f "$PID"
  echo '工作台已停止。未完成任务会在重启后标记为中断。'
  ;;
restart) "$0" stop; "$0" start ;;
status) if running; then echo "运行中 PID $(cat "$PID")"; else echo '未运行'; fi ;;
*) echo '用法: control.sh start|stop|restart|status'; exit 2 ;;
esac
