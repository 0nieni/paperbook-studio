@echo off
setlocal
pushd "%~dp0"
where python.exe >nul 2>nul
if errorlevel 1 goto :missing
python.exe -B -c "from PIL import Image; import pypdfium2, cv2" >nul 2>nul
if errorlevel 1 goto :missing
where ffmpeg.exe >nul 2>nul
if errorlevel 1 goto :missing
where pythonw.exe >nul 2>nul
if errorlevel 1 goto :console
start "" pythonw.exe -B server.py --open
popd
exit /b 0
:console
python.exe -B server.py --open
if errorlevel 1 pause
popd
exit /b
:missing
echo Python, Pillow, pypdfium2, OpenCV and FFmpeg are required. See the Markdown guide in this folder.
pause
popd
exit /b 1
