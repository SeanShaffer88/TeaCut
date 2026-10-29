#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install --quiet --disable-pip-version-check --user -r requirements.txt 2>/dev/null
exec python3 steeper.py "$@"
