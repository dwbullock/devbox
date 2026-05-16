#!/bin/bash
# Idle shutdown — checks SSH sessions, CPU load, and GPU activity.
# Runs every 5 min via cron. Shuts down after IDLE_THRESHOLD_MIN idle.

IDLE_THRESHOLD_MIN=${IDLE_THRESHOLD_MIN:-20}
LOG=/var/log/idle-shutdown.log
STATE_FILE=/var/run/idle-since

# Count established SSH connections (subtract 1 for the listening socket if shown)
ACTIVE_SSH=$(ss -tn state established '( sport = :22 )' 2>/dev/null | tail -n +2 | wc -l)

# Load average over last minute
LOAD=$(awk '{print $1}' /proc/loadavg)
LOAD_HIGH=$(echo "$LOAD > 0.5" | bc 2>/dev/null || echo 0)

# GPU utilization (if present)
GPU_BUSY=0
if command -v nvidia-smi &>/dev/null; then
  GPU_UTIL=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1)
  if [ -n "$GPU_UTIL" ] && [ "$GPU_UTIL" -gt 5 ]; then
    GPU_BUSY=1
  fi
fi

# Check for VS Code remote server processes (catches idle SSH but active dev)
VSCODE_ACTIVE=0
if pgrep -f "vscode-server" >/dev/null 2>&1; then
  # Only counts if it's been touched recently
  if find /home/*/.vscode-server -mmin -10 -type f 2>/dev/null | grep -q .; then
    VSCODE_ACTIVE=1
  fi
fi

TIMESTAMP=$(date -Iseconds)

if [ "$ACTIVE_SSH" -gt 0 ] || [ "$LOAD_HIGH" = "1" ] || [ "$GPU_BUSY" = "1" ] || [ "$VSCODE_ACTIVE" = "1" ]; then
  rm -f "$STATE_FILE"
  echo "$TIMESTAMP active ssh=$ACTIVE_SSH load=$LOAD gpu=$GPU_BUSY vscode=$VSCODE_ACTIVE" >> "$LOG"
  exit 0
fi

if [ ! -f "$STATE_FILE" ]; then
  date +%s > "$STATE_FILE"
  echo "$TIMESTAMP idle (start tracking)" >> "$LOG"
  exit 0
fi

IDLE_SINCE=$(cat "$STATE_FILE")
NOW=$(date +%s)
IDLE_MIN=$(( (NOW - IDLE_SINCE) / 60 ))

echo "$TIMESTAMP idle for ${IDLE_MIN}min" >> "$LOG"

if [ "$IDLE_MIN" -ge "$IDLE_THRESHOLD_MIN" ]; then
  echo "$TIMESTAMP SHUTDOWN triggered" >> "$LOG"
  /sbin/shutdown -h +1 "Idle shutdown after ${IDLE_MIN} minutes"
fi
