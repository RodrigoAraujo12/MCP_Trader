@echo off
rem Inicia o Claude Code com Remote Control na pasta do MCP_Trader.
rem Coloque um atalho deste arquivo em shell:startup para abrir junto com o Windows.
rem Se o processo cair (rede, erro), espera 30 s e tenta de novo.

title Claude Remote Control - MCP_Trader
cd /d "%~dp0.."

where claude >nul 2>nul
if errorlevel 1 (
    echo Comando "claude" nao encontrado no PATH.
    pause
    exit /b 1
)

:loop
echo [%date% %time%] Iniciando claude remote-control em %cd%
claude remote-control
echo [%date% %time%] Encerrou. Reiniciando em 30 s (feche a janela para parar)...
timeout /t 30 /nobreak >nul
goto loop
