@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=
set PYW=
where py >nul 2>nul && set PY=py -3
where pyw >nul 2>nul && set PYW=pyw -3
if "%PY%"=="" where python >nul 2>nul && set PY=python
if "%PYW%"=="" where pythonw >nul 2>nul && set PYW=pythonw
if "%PY%"=="" goto semPython

echo Conferindo as bibliotecas (so demora na primeira vez)...
%PY% -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto erro

%PY% -c "import tkinter" 2>nul
if errorlevel 1 goto semTk

echo Abrindo o programa...
if "%PYW%"=="" (
  %PY% ppp_gui.py
) else (
  start "" %PYW% ppp_gui.py
)
exit /b

:semTk
echo.
echo Este Python nao tem o Tkinter (a parte das janelas).
echo Reinstale o Python marcando "tcl/tk and IDLE", ou use a linha de comando:
echo   %PY% ppp_sessoes.py tudo --email seu@email
goto fim

:semPython
echo Python nao encontrado neste computador.
echo Instale abrindo o PowerShell e digitando:  winget install Python.Python.3.12
echo Depois feche esta janela e clique de novo em rodar.bat.
goto fim

:erro
echo.
echo Nao consegui instalar as bibliotecas. Confira a internet e tente de novo.

:fim
pause
