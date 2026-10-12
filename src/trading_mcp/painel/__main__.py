"""Abre o painel: python -m trading_mcp.painel [--porta 8765] [--sem-navegador] [--testar-alerta]

Escuta só em 127.0.0.1 (os dados da conta não saem do PC). Roda separado do servidor MCP: o Claude Desktop pode estar
aberto ou fechado; os dois leem o mesmo terminal MT5. Enquanto ele roda, confere os alertas de preço a cada segundo
(com o navegador aberto ou não).
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser

import uvicorn

from trading_mcp import alertas
from trading_mcp.config import load_settings
from trading_mcp.mt5_client import MT5Client
from trading_mcp.painel.app import create_app

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m trading_mcp.painel", description="Painel local do MCP_Trader.")
    parser.add_argument("--porta", type=int, default=DEFAULT_PORT, help=f"Porta local (padrão {DEFAULT_PORT})")
    parser.add_argument("--sem-navegador", action="store_true", help="Não abrir o navegador")
    parser.add_argument("--testar-alerta", action="store_true",
                        help="Só mostra uma notificação de teste do Windows (confere se os alertas vão aparecer)")
    args = parser.parse_args(argv)

    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.testar_alerta:
        ok = alertas.show_toast("Teste do MCP Trader", "Assim aparece um alerta de preço do painel.")
        print("Notificação enviada." if ok else "A notificação não saiu; veja o motivo acima.", file=sys.stderr)
        sys.exit(0 if ok else 1)

    settings = load_settings()
    for error in settings.errors:
        logging.warning("Configuração inválida: %s", error)
    mt5 = MT5Client(settings)
    store = alertas.AlertStore(settings.alertas_path)
    vigia = alertas.Vigia(store, mt5)
    app = create_app(settings, mt5, alerts=store, vigia=vigia)
    url = f"http://{HOST}:{args.porta}/"
    print(f"Painel em {url} (feche esta janela para parar; os alertas só são vigiados com ela aberta)", file=sys.stderr)
    if not args.sem_navegador:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    vigia.start()
    try:
        uvicorn.run(app, host=HOST, port=args.porta, log_level="warning")
    finally:
        vigia.stop()


if __name__ == "__main__":
    main()
