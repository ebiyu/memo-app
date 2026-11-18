@echo off
powershell -NoProfile -Command "Start-Process uv -ArgumentList 'run','--quiet','memo-desktop' -WorkingDirectory '%~dp0' -WindowStyle Hidden"
