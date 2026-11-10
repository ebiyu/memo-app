@echo off
powershell -NoProfile -Command "Start-Process uv -ArgumentList 'run','--quiet','memo-server' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
powershell -NoProfile -Command "Start-Process uv -ArgumentList 'run','--quiet','memo-desktop' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
