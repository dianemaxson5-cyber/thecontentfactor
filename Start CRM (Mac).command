#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python is not installed. Download it from https://www.python.org/downloads/"
  read -r -p "Press Return to close."
  exit 1
fi
echo "Getting things ready. The first time takes about a minute..."
python3 -m pip install --quiet --disable-pip-version-check --user -r requirements.txt 2>/dev/null \
  || python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt
python3 app.py
