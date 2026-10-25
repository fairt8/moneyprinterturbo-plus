#!/usr/bin/env bash
# Install this stack on an Oracle Cloud VM.
# It does not open host ports and it only manages the Compose project
# named moneyprinterturbo, so other bots on the same VM are left alone.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PROJECT="moneyprinterturbo"

echo "Compose project: ${PROJECT}"
echo "Machine: $(uname -m) (linux/arm64 and linux/amd64 images are both supported)"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Engine, then run this script again." >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required. The 'docker compose' command was not found." >&2
  exit 1
fi

mem_kb=0
swap_kb=0
if [[ -r /proc/meminfo ]]; then
  mem_kb="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  swap_kb="$(awk '/SwapTotal:/ {print $2}' /proc/meminfo)"
fi
avail_kb="$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
echo "Memory available: $((mem_kb / 1024)) MB. Swap: $((swap_kb / 1024)) MB. Disk free: $((avail_kb / 1024)) MB."

if [[ "${avail_kb}" -lt 2000000 ]]; then
  echo "Less than 2 GB of free disk. Free space before rendering videos." >&2
  exit 1
fi
if [[ "${mem_kb}" -gt 0 && "${mem_kb}" -lt 2500000 && "${swap_kb}" -lt 1500000 ]]; then
  cat <<'EOF'

This VM has little free RAM and little swap. A video render can be killed
by the kernel. Recommended 2 GB swap file (this script does not create it):

  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

Lower MPT_API_MEM_LIMIT in .env if other apps already use most of the RAM.
EOF
fi

if [[ ! -f config.toml ]]; then
  cp config.example.toml config.toml
  echo "Created config.toml from config.example.toml."
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example."
  echo "Edit .env (tunnel token, LLM, Telegram, PUBLIC_BASE_URL), then run this script again."
  exit 0
fi

if grep -Eq '^[[:space:]]*-[[:space:]]*"0\.0\.0\.0:' docker-compose.yml \
  || grep -Eq "^[[:space:]]*-[[:space:]]*'0\\.0\\.0\\.0:" docker-compose.yml; then
  echo "Refusing to start: docker-compose.yml publishes a port on 0.0.0.0." >&2
  exit 1
fi

profile=()
if grep -Eq '^CLOUDFLARE_TUNNEL_TOKEN=.+' .env; then
  profile=(--profile tunnel)
  echo "Cloudflare tunnel profile enabled. No Oracle security-list ports are required."
else
  echo "CLOUDFLARE_TUNNEL_TOKEN is empty. The API stays on 127.0.0.1:8080 only."
fi

export COMPOSE_PROJECT_NAME="${PROJECT}"
if grep -Eq '^MPT_IMAGE=.+' .env; then
  docker compose -p "${PROJECT}" "${profile[@]}" pull
  docker compose -p "${PROJECT}" "${profile[@]}" up -d --remove-orphans
else
  docker compose -p "${PROJECT}" "${profile[@]}" up -d --build --remove-orphans
fi

echo "Started ${PROJECT}. API: http://127.0.0.1:8080/ping"
echo "Other Compose projects were not stopped."
echo "To drop unused data for this project only, see the README. Do not run a bare 'docker system prune -a' on a shared VM."
