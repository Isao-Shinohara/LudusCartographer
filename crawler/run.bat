@echo off
REM ============================================================
REM LudusCartographer — run.bat (Windows)
REM ============================================================
REM 使用方法:
REM   run.bat connect   — ADB Wi-Fi 接続（切断時の復旧）
REM   run.bat run       — scrcpy + auto_pilot 起動
REM   run.bat restart   — ADB 再接続 → 再起動
REM   run.bat stop      — auto_pilot / scrcpy 停止
REM   run.bat ss        — 現在のスクリーンショットを取得
REM ============================================================

REM ---- 変数定義 -----------------------------------------------
SET TARGET_IP=192.168.10.118
SET TARGET_PORT=5555
SET SERIAL=%TARGET_IP%:%TARGET_PORT%

SET VENV=venv
SET PYTHON=%VENV%\Scripts\python.exe

SET SCRCPY_FLAGS=-S --always-on-top --no-audio -m 800 --window-title "Madodora-Auto"

REM ---- コマンド分岐 -------------------------------------------
IF "%1"=="" GOTO help
IF /I "%1"=="connect" GOTO connect
IF /I "%1"=="run" GOTO run
IF /I "%1"=="restart" GOTO restart
IF /I "%1"=="stop" GOTO stop
IF /I "%1"=="ss" GOTO ss
GOTO help

:connect
echo ^>^>^> ADB connect %SERIAL%
adb connect %SERIAL%
adb -s %SERIAL% get-state
GOTO end

:run
CALL :connect
echo ^>^>^> scrcpy 起動中 (バックグラウンド)
start "" scrcpy -s %SERIAL% %SCRCPY_FLAGS%
timeout /t 2 /nobreak > nul
echo ^>^>^> auto_pilot 起動
SET ANDROID_UDID=%SERIAL%
%PYTHON% -u tools\auto_pilot.py
GOTO end

:restart
CALL :stop
CALL :run
GOTO end

:stop
echo ^>^>^> プロセス停止
taskkill /F /IM scrcpy.exe 2>nul
taskkill /F /FI "WINDOWTITLE eq auto_pilot*" 2>nul
FOR /F "tokens=2" %%P IN ('tasklist /FI "IMAGENAME eq python.exe" /FO LIST ^| findstr /I "auto_pilot"') DO taskkill /F /PID %%P 2>nul
echo ^>^>^> 停止完了
GOTO end

:ss
echo ^>^>^> スクリーンショット取得
adb -s %SERIAL% shell screencap -p /sdcard/ss.png
adb -s %SERIAL% pull /sdcard/ss.png %TEMP%\ss.png
echo ^>^>^> %TEMP%\ss.png に保存しました
start "" %TEMP%\ss.png
GOTO end

:help
echo.
echo  使用方法: run.bat [command]
echo.
echo  run.bat connect   — ADB Wi-Fi 接続復旧
echo  run.bat run       — auto_pilot 起動 (scrcpy 含む)
echo  run.bat restart   — 停止 → 再接続 → 再起動
echo  run.bat stop      — 全プロセス停止
echo  run.bat ss        — スクリーンショット取得
echo.
echo  IP 変更は run.bat 先頭の TARGET_IP 変数を編集してください。
echo.

:end
