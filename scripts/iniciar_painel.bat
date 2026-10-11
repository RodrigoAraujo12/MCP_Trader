@echo off
rem Abre o painel do MCP_Trader no navegador (http://127.0.0.1:8765).
rem Precisa do MetaTrader 5 aberto. O Claude Desktop pode estar aberto ou fechado.
rem Feche esta janela para parar o painel.

title Painel MCP_Trader
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo Ambiente .venv nao encontrado em %cd%. Veja a secao Instalacao do README.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m trading_mcp.painel %*
if errorlevel 1 pause
