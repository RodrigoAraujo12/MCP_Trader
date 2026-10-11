"""Aplicação web do painel (Starlette): página estática e uma API JSON só de leitura.

Nada aqui envia, altera ou cancela ordens, nem grava no journal: a execução continua só pelo chat, com a aprovação na
janela do Windows. Um botão "Enviar" numa página local poderia ser acionado por qualquer programa do PC (inclusive o
próprio Claude, por um navegador ou curl), o que anularia a garantia da janela de aprovação.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import numpy as np
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from trading_mcp import smc, tempo
from trading_mcp.config import Settings
from trading_mcp.mt5_client import MT5Client, MT5Error, SymbolNotFoundError
from trading_mcp.painel import graficos

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
# Estrutura na barra lateral: do maior para o menor, como o Claude apresenta.
SIDEBAR_TIMEFRAMES = ("H4", "H1", "M15", "M5")
# Contra DNS rebinding: uma página de fora não consegue se passar pelo painel.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


def _clean(value: Any) -> Any:
    """JSON válido para o navegador: NaN/infinito viram null e tipos do numpy viram nativos."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json(content: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(_clean(content), status_code=status, headers={"Cache-Control": "no-store"})


def _symbol(request: Request) -> str:
    symbol = request.query_params.get("simbolo", "").strip()
    if not symbol:
        raise ValueError("Informe o símbolo (ex.: USTEC).")
    return symbol


def _timeframe(request: Request, default: str = "M5") -> str:
    return request.query_params.get("tf", default).strip().upper() or default


def create_app(settings: Settings, mt5: MT5Client) -> Starlette:
    def handle(func):  # noqa: ANN001, ANN202
        def endpoint(request: Request) -> JSONResponse:
            try:
                return _json(func(request))
            except SymbolNotFoundError as exc:
                return _json({"erro": str(exc)}, 404)
            except ValueError as exc:
                return _json({"erro": str(exc)}, 400)
            except MT5Error as exc:
                return _json({"erro": str(exc)}, 502)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Erro inesperado no painel")
                return _json({"erro": f"Erro inesperado ({type(exc).__name__}): {exc}"}, 500)

        return endpoint

    def config(request: Request) -> dict[str, Any]:
        return {"instrumentos": list(settings.instruments), "timeframes": list(smc.TIMEFRAMES)}

    def chart(request: Request) -> dict[str, Any]:
        bars = int(request.query_params.get("candles", graficos.DEFAULT_BARS))
        return graficos.chart(mt5, _symbol(request), _timeframe(request), bars)

    def structure(request: Request) -> dict[str, Any]:
        chart_tf = _timeframe(request)
        frames = list(dict.fromkeys((*SIDEBAR_TIMEFRAMES, chart_tf)))
        out = smc.report(mt5, _symbol(request), frames)
        # As notas fixas (definições) ficam na documentação; na tela só os avisos sobre os dados de agora.
        out["avisos"] = [n for n in out.pop("observacoes", []) if n not in smc.NOTES and not n.startswith("Alvos =")]
        return out

    def live(request: Request) -> dict[str, Any]:
        symbol, tf = _symbol(request), _timeframe(request)
        quote = mt5.quote(symbol)
        spec = mt5.symbol_spec(quote["simbolo"])
        df = mt5.rates(quote["simbolo"], tf, 2, include_current=True)
        last_open = df["time"].iloc[-1].to_pydatetime()
        forming = bool(df.attrs.get("ultimo_em_formacao"))
        return {
            "cotacao": quote,
            "candles": graficos.candles(df, spec["digitos"]),
            "ultimo_em_formacao": forming,
            "fecha_em": graficos.epoch(tempo.bar_end(last_open, tf)) if forming else None,
            "agora": graficos.epoch(mt5.now_utc()),
        }

    def index(request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    routes = [
        Route("/", index),
        Route("/api/config", handle(config)),
        Route("/api/grafico", handle(chart)),
        Route("/api/estrutura", handle(structure)),
        Route("/api/ao_vivo", handle(live)),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    return Starlette(routes=routes, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)])
