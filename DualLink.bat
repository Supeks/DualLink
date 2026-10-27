@echo off
rem Starts DualLink in the tray. It asks for admin rights so it can make the
rem phone tether the main link.
cd /d "%~dp0"
start "" pythonw -m duallink
