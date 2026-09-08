#!/bin/bash
# Start/Stop der Mock-Infrastruktur.  Aufruf: mocks.sh start <llm-mode> [sink-mode]   |   mocks.sh stop
PY=/home/kpafi/maildigest-coldtest/venv/bin/python
W=/home/kpafi/maildigest-coldtest/work
cd "$W" || exit 1
case "$1" in
  stop)
    for f in "$W"/logs/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null; rm -f "$f"; done
    sleep 0.4
    ;;
  start)
    LLMMODE=${2:-nice}; SINKMODE=${3:-ok}
    nohup "$PY" scripts/mock_llm.py 8932 "$LLMMODE" > logs/llm.out 2>&1 &
    echo $! > logs/llm.pid
    nohup "$PY" scripts/sink_server.py 8931 "$SINKMODE" > logs/sink.out 2>&1 &
    echo $! > logs/sink.pid
    if ! pgrep -f "imap_server[.]py" > /dev/null; then
      nohup "$PY" scripts/imap_server.py 9930 "$W/maildir" > logs/imap.out 2>&1 &
      echo $! > logs/imap.pid
    fi
    sleep 1.2
    cat logs/llm.out logs/sink.out 2>/dev/null
    ;;
  *) echo "usage: mocks.sh start|stop"; exit 2;;
esac
