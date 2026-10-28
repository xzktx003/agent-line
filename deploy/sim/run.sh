#!/usr/bin/env bash
set -euo pipefail
deploy_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
agent_line_root="$(cd -- "$deploy_dir/../.." && pwd)"
# Put the command first so global Compose flags cannot hide a start operation.
case "${1:-}" in
  init)
    if [[ "$#" != 1 ]]; then
      echo 'Sim init 不接受额外参数。' >&2
      exit 2
    fi
    exec python3 "$deploy_dir/prepare.py" initialize
    ;;
  -*)
    echo '请先指定 Compose 命令，再传递该命令的参数。' >&2
    exit 2
    ;;
  up|start|restart|create|run|watch|scale|unpause)
    python3 "$deploy_dir/prepare.py" check-start
    ;;
  *) python3 "$deploy_dir/prepare.py" ;;
esac
compose_files=(-f "$deploy_dir/compose.yaml")
if [[ -n "${AGENT_LINE_SIM_TRIGGER_CRON_DIR:-}" ]]; then
  if [[ "$AGENT_LINE_SIM_TRIGGER_CRON_DIR" != /* ]]; then
    echo 'AGENT_LINE_SIM_TRIGGER_CRON_DIR must be an absolute prepared directory.' >&2
    exit 1
  fi
  python3 "$deploy_dir/trigger_cron.py" --verify "$AGENT_LINE_SIM_TRIGGER_CRON_DIR" >&2
  # Private bind mounts retain the preparer's UID/GID; do not make tokens public.
  export AGENT_LINE_SIM_TRIGGER_CRON_OWNER="$(id -u):$(id -g)"
  compose_files+=(-f "$deploy_dir/triggers.compose.yaml")
fi
exec docker compose --project-name agent-line --env-file "$agent_line_root/var/sim/sim.env" "${compose_files[@]}" "$@"
