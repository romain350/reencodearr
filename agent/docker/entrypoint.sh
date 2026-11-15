#!/bin/sh
# Agent Reencodearr en conteneur : s'enregistre une fois (code a usage unique), puis tourne.
set -e
D=/data
[ -n "$HUB_URL" ] || { echo "HUB_URL est requis"; exit 1; }
mkdir -p /opt/agent "$D"
# l'agent est toujours recupere sur le hub : il reste a la version du hub
curl -fsSL "$HUB_URL/agent/agent.py" -o /opt/agent/agent.py
if [ ! -f "$D/config.json" ]; then
  [ -n "$ENROLL_CODE" ] || { echo "ENROLL_CODE est requis au premier demarrage"; exit 1; }
  python3 /opt/agent/agent.py register --hub "$HUB_URL" --code "$ENROLL_CODE" \
      --name "${AGENT_NAME:-$(hostname)}" --ffmpeg /usr/local/bin/ffmpeg --dir "$D"
fi
exec python3 -u /opt/agent/agent.py run --dir "$D"
