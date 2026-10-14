#!/bin/sh
# Runs a hook script with whatever Python 3 is installed: hooks/run.sh <path of the .py>. exec replaces this shell, so the
# script runs exactly once and gets the hook input on stdin. Prints nothing and exits 0 if there is no Python.
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) PY=$(command -v python || command -v python3) ;;  # python3 on Windows may be the Store stub
  *) PY=$(command -v python3 || command -v python) ;;
esac
[ -n "$PY" ] || exit 0
exec "$PY" "$@"
