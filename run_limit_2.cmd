@echo off
setlocal
cd /d "%~dp0"
python.exe .\aligned_song_video_runner.py --limit 2
pause
