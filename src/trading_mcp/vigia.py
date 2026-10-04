"""Vigia de preço (só leitura): termina quando a cotação chega a um nível, para acordar quem está esperando.

Uso: python -m trading_mcp.vigia SIMBOLO [--acima PRECO] [--abaixo PRECO] [--intervalo S] [--max-min M]

Imprime uma linha por evento e sai com:
  0 = tocou um nível ("TOQUE ACIMA ..." / "TOQUE ABAIXO ..."),
  2 = acabou o tempo sem toque ("FIM_SEM_TOQUE"),
  3 = sem cotação do MT5 em leituras seguidas ou símbolo inexistente ("ERRO_MT5 ...").
Lê só a cotação (MT5Client.quote, mesma conexão e conta do servidor); não envia ordens.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from trading_mcp.config import load_settings
from trading_mcp.mt5_client import MT5Client, MT5Error, SymbolNotFoundError

TOQUE, SEM_TOQUE, ERRO = 0, 2, 3
_MAX_FALHAS = 10  # leituras seguidas com erro do MT5 antes de desistir


def nivel_tocado(bid: float, acima: float | None, abaixo: float | None) -> str | None:
    """"acima" se o bid chegou ao nível de cima, "abaixo" se chegou ao de baixo; None se nenhum."""
    if acima is not None and bid >= acima:
        return "acima"
    if abaixo is not None and bid <= abaixo:
        return "abaixo"
    return None


def vigiar(
    client: Any,
    simbolo: str,
    acima: float | None,
    abaixo: float | None,
    *,
    intervalo_s: float = 60.0,
    max_min: float = 115.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    out: Callable[[str], None] = print,
) -> int:
    """Consulta a cotação a cada intervalo até tocar um nível ou passar de max_min minutos.

    Cotação que não está "atual" (mercado fechado, atraso) não conta como toque nem como erro.
    """
    fim = monotonic() + max_min * 60
    falhas = 0
    out(f"vigiando {simbolo}: acima de {acima} / abaixo de {abaixo}, a cada {intervalo_s:g}s por até {max_min:g} min")
    while True:
        cotacao = None
        try:
            cotacao = client.quote(simbolo)
            falhas = 0
        except SymbolNotFoundError as exc:
            out(f"ERRO_MT5 {exc}")
            return ERRO
        except MT5Error as exc:
            falhas += 1
            if falhas >= _MAX_FALHAS:
                out(f"ERRO_MT5 {falhas} leituras seguidas sem cotação: {exc}")
                return ERRO
        if cotacao is not None and cotacao["estado"] == "atual":
            lado = nivel_tocado(cotacao["bid"], acima, abaixo)
            if lado:
                nivel = acima if lado == "acima" else abaixo
                out(
                    f"TOQUE {lado.upper()} {cotacao['simbolo']} bid={cotacao['bid']} (nível {nivel}) "
                    f"às {cotacao['horario']['sao_paulo']} de São Paulo"
                )
                return TOQUE
        if monotonic() >= fim:
            out("FIM_SEM_TOQUE")
            return SEM_TOQUE
        sleep(intervalo_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trading_mcp.vigia", description=__doc__.splitlines()[0])
    parser.add_argument("simbolo")
    parser.add_argument("--acima", type=float, help="avisa quando o bid chegar a este preço ou passar dele")
    parser.add_argument("--abaixo", type=float, help="avisa quando o bid chegar a este preço ou cair abaixo dele")
    parser.add_argument("--intervalo", type=float, default=60.0, help="segundos entre leituras (padrão 60)")
    parser.add_argument("--max-min", type=float, default=115.0, help="minutos até desistir (padrão 115)")
    args = parser.parse_args(argv)
    if args.acima is None and args.abaixo is None:
        parser.error("informe --acima e/ou --abaixo")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    client = MT5Client(load_settings())
    return vigiar(
        client, args.simbolo, args.acima, args.abaixo,
        intervalo_s=args.intervalo, max_min=args.max_min, out=lambda linha: print(linha, flush=True),
    )


if __name__ == "__main__":
    sys.exit(main())
