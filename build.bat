@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Checking dependencies...
python -c "import aiohttp, yaml" 2>nul
if errorlevel 1 (
    echo     Installing requirements...
    python -m pip install -r requirements.txt
    if errorlevel 1 goto fail
)

echo [2/3] Cleaning old build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist qq-multi-model.spec del /q qq-multi-model.spec

echo [3/3] Building single-file exe...
python -m PyInstaller --noconfirm --clean --onefile --console --name qq-multi-model --runtime-tmpdir "_rt" --add-data "config.example.yaml;." --collect-data certifi --collect-submodules app --hidden-import aiohttp --hidden-import yaml --hidden-import certifi main.py
if errorlevel 1 goto fail

copy /y config.example.yaml dist\config.example.yaml >nul

echo.
echo Done: dist\qq-multi-model.exe
echo Copy the exe anywhere; first run creates config.yaml next to it.
goto :eof

:fail
echo.
echo Build failed. Check the errors above.
exit /b 1
