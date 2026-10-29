@echo off
cd /d "%~dp0"
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
start "" pythonw steeper.py %*
