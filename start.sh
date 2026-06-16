#!/usr/bin/env bash
# =============================================================================
# start.sh — Apex Scalper launcher cu health checks complete
# Versiune: 1.0.0
#
# Usage:
#   ./start.sh           — start normal
#   ./start.sh --restart — kill existing + start
#   ./start.sh --check   — doar ruleaza health checks, fara start
# =============================================================================
set -euo pipefail

# --------------------------------------------------------------------------- #
#  Configurare
# --------------------------------------------------------------------------- #
BOT_SCRIPT="run_bot.py"
LOG_FILE="logs/apex_scalper.log"
HEALTH_URL="http://localhost:8080/health"
HEALTH_TIMEOUT=15          # secunde asteptare health endpoint
HEALTH_RETRY_INTERVAL=1    # secunde intre retry-uri
STARTUP_WAIT=8             # secunde asteptare dupa pornire
COLOR_OK="\033[0;32m"      # verde
COLOR_WARN="\033[0;33m"    # galben
COLOR_ERR="\033[0;31m"     # rosu
COLOR_INFO="\033[0;36m"    # cyan
COLOR_RESET="\033[0m"

ok()   { echo -e "${COLOR_OK}  ✔ $*${COLOR_RESET}"; }
warn() { echo -e "${COLOR_WARN}  ⚠ $*${COLOR_RESET}"; }
err()  { echo -e "${COLOR_ERR}  ✘ $*${COLOR_RESET}"; }
info() { echo -e "${COLOR_INFO}  ▶ $*${COLOR_RESET}"; }

# --------------------------------------------------------------------------- #
#  Parse args
# --------------------------------------------------------------------------- #
DO_RESTART=false
CHECK_ONLY=false
for arg in "$@"; do
  case $arg in
    --restart) DO_RESTART=true ;;
    --check)   CHECK_ONLY=true ;;
  esac
done

# --------------------------------------------------------------------------- #
#  Banner
# --------------------------------------------------------------------------- #
echo ""
echo -e "${COLOR_INFO}======================================================${COLOR_RESET}"
echo -e "${COLOR_INFO}  ⚡ Apex Scalper — Start Script v1.0.0${COLOR_RESET}"
echo -e "${COLOR_INFO}======================================================${COLOR_RESET}"
echo ""

# --------------------------------------------------------------------------- #
#  CHECK 1: Python + venv
# --------------------------------------------------------------------------- #
info "CHECK 1: Python + virtualenv"
if [ -d "venv" ]; then
  ok "venv/ exista"
else
  err "venv/ lipsa — ruleaza: python3 -m venv venv && pip install -r requirements.txt"
  exit 1
fi

# Activeaza venv
# shellcheck source=/dev/null
source venv/bin/activate

PYTHON_VER=$(python --version 2>&1)
ok "Python: $PYTHON_VER"

# --------------------------------------------------------------------------- #
#  CHECK 2: run_bot.py exista
# --------------------------------------------------------------------------- #
info "CHECK 2: run_bot.py"
if [ -f "$BOT_SCRIPT" ]; then
  ok "$BOT_SCRIPT gasit"
else
  err "$BOT_SCRIPT lipsa!"
  exit 1
fi

# --------------------------------------------------------------------------- #
#  CHECK 3: .env / config
# --------------------------------------------------------------------------- #
info "CHECK 3: .env / variabile de mediu"
if [ -f ".env" ]; then
  ok ".env gasit"
  # shellcheck source=/dev/null
  set -a; source .env; set +a
else
  warn ".env lipsa — asteptam variabile din environment"
fi

MISSING_ENV=false
for var in BYBIT_API_KEY BYBIT_API_SECRET TELEGRAM_TOKEN TELEGRAM_CHAT_ID; do
  if [ -z "${!var:-}" ]; then
    warn "  $var nesetat"
    MISSING_ENV=true
  else
    ok "  $var setat"
  fi
done

if [ "$MISSING_ENV" = true ]; then
  warn "Unele variabile lipsesc — bot-ul poate functiona partial"
fi

# --------------------------------------------------------------------------- #
#  CHECK 4: dependinte Python
# --------------------------------------------------------------------------- #
info "CHECK 4: dependinte Python"
PACKAGES=("pybit" "websockets" "loguru" "telegram" "plotly" "dash" "aiohttp")
MISSING_PKGS=0
for pkg in "${PACKAGES[@]}"; do
  if python -c "import $pkg" 2>/dev/null; then
    ok "  $pkg"
  else
    err "  $pkg LIPSA — ruleaza: pip install $pkg"
    MISSING_PKGS=$((MISSING_PKGS + 1))
  fi
done

if [ "$MISSING_PKGS" -gt 0 ]; then
  err "$MISSING_PKGS pachete lipsa. Ruleaza: pip install -r requirements.txt"
  exit 1
fi

# --------------------------------------------------------------------------- #
#  CHECK 5: logs/ dir
# --------------------------------------------------------------------------- #
info "CHECK 5: director logs/"
mkdir -p logs
ok "logs/ ready"

# --------------------------------------------------------------------------- #
#  CHECK 6: port 8080 (health server)
# --------------------------------------------------------------------------- #
info "CHECK 6: port 8080 (health server)"
if lsof -i :8080 -sTCP:LISTEN -t >/dev/null 2>&1; then
  warn "Port 8080 ocupat — health server va folosi portul existent"
else
  ok "Port 8080 liber"
fi

# --------------------------------------------------------------------------- #
#  CHECK 7: bot deja pornit?
# --------------------------------------------------------------------------- #
info "CHECK 7: proces $BOT_SCRIPT existent"
EXISTING_PID=$(pgrep -f "$BOT_SCRIPT" || true)
if [ -n "$EXISTING_PID" ]; then
  if [ "$DO_RESTART" = true ]; then
    warn "Bot deja rulat (PID $EXISTING_PID) — oprire pentru restart..."
    kill "$EXISTING_PID" 2>/dev/null || true
    sleep 3
    ok "Proces oprit"
  elif [ "$CHECK_ONLY" = true ]; then
    ok "Bot rulat (PID $EXISTING_PID)"
  else
    warn "Bot deja rulat (PID $EXISTING_PID) — foloseste --restart pentru restart"
    # Continua la health check
  fi
else
  ok "Niciun proces existent"
fi

# --------------------------------------------------------------------------- #
#  Daca --check only, sarim la health check
# --------------------------------------------------------------------------- #
if [ "$CHECK_ONLY" = true ]; then
  info "Mod --check: sarim pornirea botului"
else
  # ----------------------------------------------------------------------- #
  #  PORNIRE BOT
  # ----------------------------------------------------------------------- #
  echo ""
  info "Pornire bot..."
  nohup python "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1 &
  BOT_PID=$!
  ok "Bot pornit cu PID $BOT_PID"
  echo "  Log: $LOG_FILE"
  echo ""
  info "Astept ${STARTUP_WAIT}s pentru initializare..."
  sleep "$STARTUP_WAIT"
fi

# --------------------------------------------------------------------------- #
#  CHECK 8: health endpoint
# --------------------------------------------------------------------------- #
info "CHECK 8: health endpoint ($HEALTH_URL)"
HEALTH_OK=false
for i in $(seq 1 "$HEALTH_TIMEOUT"); do
  RESPONSE=$(curl -sf "$HEALTH_URL" 2>/dev/null || true)
  if [ -n "$RESPONSE" ]; then
    HEALTH_OK=true
    break
  fi
  sleep "$HEALTH_RETRY_INTERVAL"
done

if [ "$HEALTH_OK" = true ]; then
  ok "Health endpoint raspunde"
  STATUS=$(echo "$RESPONSE" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")
  FEED_STALE=$(echo "$RESPONSE" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('feed_stale', '?'))" 2>/dev/null || echo "?")
  TRADING=$(echo "$RESPONSE" | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('trading_active', '?'))" 2>/dev/null || echo "?")
  TICK_AGE=$(echo "$RESPONSE" | python -c "import sys,json; d=json.load(sys.stdin); print(f\"{d.get('last_tick_age_s', '?'):.3f}s\")" 2>/dev/null || echo "?")
  echo ""
  echo -e "  ${COLOR_INFO}Health JSON:${COLOR_RESET}"
  echo "$RESPONSE" | python -c "import sys,json; print(json.dumps(json.load(sys.stdin), indent=4))" 2>/dev/null || echo "  $RESPONSE"
  echo ""
  if [ "$STATUS" = "ok" ]; then
    ok "Status: $STATUS"
  else
    warn "Status: $STATUS"
  fi
  if [ "$FEED_STALE" = "False" ] || [ "$FEED_STALE" = "false" ]; then
    ok "Feed: activ (tick_age=${TICK_AGE})"
  else
    warn "Feed: STALE (tick_age=${TICK_AGE})"
  fi
  if [ "$TRADING" = "True" ] || [ "$TRADING" = "true" ]; then
    ok "Trading: activ"
  else
    warn "Trading: INACTIV"
  fi
else
  err "Health endpoint NU raspunde dupa ${HEALTH_TIMEOUT}s"
  warn "Ultim log:"
  tail -20 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi

# --------------------------------------------------------------------------- #
#  CHECK 9: procese critice in log (erori la startup)
# --------------------------------------------------------------------------- #
info "CHECK 9: erori critice in log"
CRIT_ERRORS=$(grep -c "CRITICAL\|ERROR.*startup\|Traceback\|ModuleNotFoundError\|ImportError" "$LOG_FILE" 2>/dev/null | tail -1 || echo 0)
if [ "$CRIT_ERRORS" -gt 0 ]; then
  warn "$CRIT_ERRORS erori/critice in log (poate fi normal daca sunt vechi)"
  echo "  Ultimele erori:"
  grep -E "CRITICAL|ERROR.*startup|ModuleNotFoundError|ImportError" "$LOG_FILE" 2>/dev/null | tail -5 | sed 's/^/    /'
else
  ok "Nicio eroare critica in log"
fi

# --------------------------------------------------------------------------- #
#  CHECK 10: WS feed + MTF in log
# --------------------------------------------------------------------------- #
info "CHECK 10: WS feed + indicatori in log"
if grep -q "WS connected + subscribed" "$LOG_FILE" 2>/dev/null; then
  ok "WS feed conectat (din log)"
else
  warn "WS feed: nu gasit in log inca"
fi

if grep -q "Indicatori ready\|rsi_ready=True\|warmup complet" "$LOG_FILE" 2>/dev/null; then
  ok "Indicatori ready (din log)"
else
  warn "Indicatori: warmup in curs — normal la primul start"
fi

# --------------------------------------------------------------------------- #
#  Sumar final
# --------------------------------------------------------------------------- #
echo ""
echo -e "${COLOR_INFO}======================================================${COLOR_RESET}"
echo -e "${COLOR_OK}  ⚡ Apex Scalper pornit si functional${COLOR_RESET}"
echo -e "${COLOR_INFO}======================================================${COLOR_RESET}"
echo ""
echo "  Dashboard : http://localhost:8050"
echo "  Health    : $HEALTH_URL"
echo "  Log live  : tail -f $LOG_FILE"
echo "  Stop bot  : pkill -f $BOT_SCRIPT"
echo ""
