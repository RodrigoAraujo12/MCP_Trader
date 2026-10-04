"""Vigia de preço: sai no toque, ignora cotação velha, desiste por tempo ou por MT5 fora do ar."""

from __future__ import annotations

import pytest

from trading_mcp import vigia
from trading_mcp.mt5_client import MT5Error, SymbolNotFoundError


class _Relogio:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _Cliente:
    """Devolve as cotações (ou exceções) em sequência, repetindo a última."""

    def __init__(self, itens):
        self.itens = list(itens)

    def quote(self, simbolo):
        item = self.itens.pop(0) if len(self.itens) > 1 else self.itens[0]
        if isinstance(item, Exception):
            raise item
        bid, estado = item
        return {"simbolo": "BTCUSDm", "bid": bid, "estado": estado, "horario": {"sao_paulo": "2026-10-04 19:00:00"}}


def _rodar(itens, acima=100.0, abaixo=90.0, max_min=10.0):
    relogio, linhas = _Relogio(), []
    codigo = vigia.vigiar(
        _Cliente(itens), "BTCUSD", acima, abaixo,
        intervalo_s=60, max_min=max_min, sleep=relogio.sleep, monotonic=relogio.monotonic, out=linhas.append,
    )
    return codigo, linhas, relogio.t


@pytest.mark.parametrize(
    ("bid", "esperado"), [(100.0, "acima"), (100.5, "acima"), (90.0, "abaixo"), (89.9, "abaixo"), (95.0, None)]
)
def test_nivel_tocado_inclui_o_proprio_nivel(bid, esperado):
    assert vigia.nivel_tocado(bid, 100.0, 90.0) == esperado


def test_nivel_tocado_com_um_lado_so():
    assert vigia.nivel_tocado(50.0, None, 90.0) == "abaixo"
    assert vigia.nivel_tocado(150.0, None, 90.0) is None


def test_sai_no_primeiro_toque():
    codigo, linhas, t = _rodar([(95.0, "atual"), (96.0, "atual"), (100.2, "atual")])
    assert codigo == vigia.TOQUE and t == 120
    assert linhas[-1].startswith("TOQUE ACIMA BTCUSDm bid=100.2 (nível 100.0)")


def test_cotacao_velha_nao_dispara():
    codigo, linhas, _ = _rodar([(150.0, "mercado_fechado_provavel")], max_min=3)
    assert codigo == vigia.SEM_TOQUE and linhas[-1] == "FIM_SEM_TOQUE"


def test_falhas_seguidas_do_mt5_encerram_com_erro():
    codigo, linhas, _ = _rodar([MT5Error("terminal fechado")], max_min=60)
    assert codigo == vigia.ERRO and linhas[-1].startswith("ERRO_MT5 10 leituras seguidas")


def test_falha_isolada_nao_encerra():
    codigo, _, _ = _rodar([MT5Error("soluço"), (89.0, "atual")])
    assert codigo == vigia.TOQUE


def test_simbolo_inexistente_encerra_na_hora():
    codigo, linhas, t = _rodar([SymbolNotFoundError("XYZ não existe")])
    assert codigo == vigia.ERRO and t == 0 and "XYZ" in linhas[-1]


def test_main_exige_algum_nivel():
    with pytest.raises(SystemExit):
        vigia.main(["BTCUSD"])
