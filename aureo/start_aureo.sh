#!/bin/bash
# Prende a ÁUREO completo: terminal MT5 (Wine) + operador demo + vigía + estudio.
# Idempotente: no relanza lo que ya está vivo. Uso: ./start_aureo.sh
cd "$(dirname "$0")" || exit 1
export WINEPREFIX="$PWD/data/mt5/wineprefix" WINEDEBUG=-all DISPLAY="${DISPLAY:-:0}"
PY="$PWD/.venv/bin/python"
TERM="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"

launch() {  # launch <patrón-pgrep> <descripción> <comando...>
  if pgrep -f "$1" >/dev/null; then
    echo "ya corriendo: $2"
  else
    "${@:3}" >/dev/null 2>&1 &
    echo "prendido: $2 (pid $!)"
  fi
}

# 1) terminal MT5 persistente (para operar/aprender rápido)
if pgrep -f terminal64.exe >/dev/null; then echo "ya corriendo: terminal MT5"
else nohup wine "$TERM" /portable >/tmp/mt5_terminal.log 2>&1 & echo "prendido: terminal MT5 (pid $!)"; sleep 25; fi

# 2) operador demo (opera cada hora, registra y aprende)
launch "demo_trader --daemon" "operador demo" nohup "$PY" -m core.demo_trader --daemon --poll 300
# 3) vigía de precios (alertas Telegram)
launch "price_watch --daemon" "vigía de precios" nohup "$PY" -m analysis.price_watch --daemon --poll 180
# 4) estudio constante (aprendizaje + alertas de edge)
launch "study_loop --daemon" "estudio constante" nohup "$PY" -m research.study_loop --daemon --poll 43200

echo "---"; echo "ÁUREO andando. Parar todo: pkill -f 'demo_trader|study_loop|price_watch|terminal64.exe'"
