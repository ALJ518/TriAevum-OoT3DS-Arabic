@echo off
chcp 65001 >nul
if "%~1"=="" (
    "%~dp0build_custom_o3ro.exe"
) else (
    "%~dp0build_custom_o3ro.exe" "%~1"
)
echo.
pause
