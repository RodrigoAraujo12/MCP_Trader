"""Abre o painel: python -m trading_mcp.painel [--porta 8765] [--sem-navegador]

Escuta só em 127.0.0.1 (os dados da conta não saem do PC). Roda separado do servidor MCP: o Claude Desktop pode estar
aberto ou fechado; os dois leem o mesmo terminal MT5.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser

import uvicorn

from trading_mcp.config import load_settings
from trading_mcp.mt5_client import MT5Client
from trading_mcp.painel.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m trading_mcp.painel", description="Painel local do MCP_Trader.")
    parser.add_argument("--porta", type=int, default=DEFAULT_PORT, help=f"Porta local (padrão {DEFAULT_PORT})")
    parser.add_argument("--sem-navegador", action="store_true", help="Não abrir o navegador")
    args = parser.parse_args(argv)

    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    for error in settings.errors:
        logging.warning("Configuração inválida: %s", error)
    app = create_app(settings, MT5Client(settings))
    url = f"http://{HOST}:{args.porta}/"
    print(f"Painel em {url} (feche esta janela para parar)", file=sys.stderr)
    if not args.sem_navegador:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=HOST, port=args.porta, log_level="warning")


if __name__ == "__main__":
    main()
