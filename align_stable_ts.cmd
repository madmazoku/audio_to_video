@echo off
setlocal EnableExtensions

REM align_stable_ts_project.cmd
REM Usage:
REM   align_stable_ts_project.cmd G:\Git\ComfyUI\!BASHO\my_song
REM
REM Reads from project folder:
REM   lyrics.txt
REM   vocals.mp3  OR  audio.mp3
REM   language.txt optional, default: en
REM
REM Writes to project folder:
REM   alignment.json

if "%~1"=="" (
    echo Usage: %~nx0 project_dir
    exit /b 1
)

set "PROJECT=%~f1"
set "LYRICS=%PROJECT%\lyrics.txt"
set "VOCALS=%PROJECT%\vocals.mp3"
set "AUDIO=%PROJECT%\audio.mp3"
set "OUTJSON=%PROJECT%\alignment.json"
set "LANGFILE=%PROJECT%\language.txt"
set "LANG=en"

if exist "%LANGFILE%" set /p LANG=<"%LANGFILE%"

REM Edit if your path is different.
set "STABLE_TS_EXE=G:\Git\stable-ts\.venv\Scripts\stable-ts.exe"

REM Put ffmpeg into PATH if needed.
if exist "E:\Git\ffmpeg\bin\ffmpeg.exe" set "PATH=E:\Git\ffmpeg\bin;%PATH%"
if exist "G:\Git\ffmpeg\bin\ffmpeg.exe" set "PATH=G:\Git\ffmpeg\bin;%PATH%"

if not exist "%PROJECT%" (
    echo ERROR: project folder not found: "%PROJECT%"
    exit /b 2
)

if not exist "%LYRICS%" (
    echo ERROR: lyrics file not found: "%LYRICS%"
    exit /b 3
)

set "ALIGN_AUDIO="
if exist "%VOCALS%" set "ALIGN_AUDIO=%VOCALS%"
if not defined ALIGN_AUDIO if exist "%AUDIO%" set "ALIGN_AUDIO=%AUDIO%"

if not defined ALIGN_AUDIO (
    echo ERROR: no audio for alignment. Put "%VOCALS%" or "%AUDIO%"
    exit /b 4
)

if not exist "%STABLE_TS_EXE%" (
    echo ERROR: stable-ts not found: "%STABLE_TS_EXE%"
    exit /b 5
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo ERROR: ffmpeg not found in PATH.
    exit /b 6
)

echo [stage] stable-ts alignment
echo   project : "%PROJECT%"
echo   lyrics  : "%LYRICS%"
echo   audio   : "%ALIGN_AUDIO%"
echo   lang    : "%LANG%"
echo   output  : "%OUTJSON%"

"%STABLE_TS_EXE%" "%ALIGN_AUDIO%" ^
  --align "%LYRICS%" ^
  --language "%LANG%" ^
  -o "%OUTJSON%"

if errorlevel 1 (
    echo ERROR: stable-ts failed.
    exit /b 10
)

if not exist "%OUTJSON%" (
    echo ERROR: output JSON was not created: "%OUTJSON%"
    exit /b 11
)

echo Done: "%OUTJSON%"
endlocal
