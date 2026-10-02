"""Servidor MCP (stdio) de análise de mercado: forex e ações dos EUA.

O MT5 é só lido: nenhuma tool envia, altera ou cancela ordens. As tools journal_* gravam apenas no
arquivo local do journal.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from trading_mcp import indicators, posicoes as posicoes_report, reacao, risk, tempo
from trading_mcp.calendario import CalendarError, EconomicCalendar
from trading_mcp.config import Settings, load_settings
from trading_mcp.journal import Journal, JournalError
from trading_mcp.mt5_client import MT5Client, MT5Error
from trading_mcp.sec_edgar import SecEdgarClient, SecEdgarError

logger = logging.getLogger(__name__)

# ~500 linhas de CSV ficam bem abaixo do limite de tokens por resposta de tool do Claude Code.
MAX_CANDLES_OUTPUT = 500
DEFAULT_INDICATORS = ["RSI(14)", "MACD(12,26,9)", "EMA(20)", "EMA(50)", "ATR(14)"]
# Entrada mais longe que isso do preço atual provavelmente é erro de digitação.
ENTRY_DISTANCE_WARNING_PCT = 5.0
# País exportado pelo serviço MQL5 TradingMcpCalendar.
CALENDAR_COUNTRY = "US"

INSTRUCTIONS = """\
Servidor de dados de mercado (forex e CFDs de ações dos EUA via MetaTrader 5 / Exness; fundamentos via SEC EDGAR).
Não existe tool para enviar ordens: o MT5 só é lido. As tools journal_* gravam apenas no journal local.

Regras de uso:
- Para tamanho de lote, use SEMPRE a tool `tamanho_posicao` em vez de calcular manualmente: ela usa a
  especificação real do contrato na corretora (valor do tick, lote mínimo, passo).
- Horários estão em UTC (o servidor da Exness usa UTC; conferido no terminal). As tools também trazem
  São Paulo e Nova York; para converter outros horários use as diferenças informadas, que mudam com o
  horário de verão dos EUA.
- Cotação: só apresente como preço atual quando `estado` = "atual". Nos demais estados, informe a idade
  e o aviso; não misture cotações de horários diferentes como se fossem simultâneas.
- `em_formacao` diz se o último candle ainda está aberto; os indicadores mudam até ele fechar.
- Os símbolos são CFDs da corretora (ex.: USTECm acompanha o Nasdaq 100, mas não é o índice): cite o
  símbolo exato.
- Indicadores seguem a convenção do TradingView. O ATR e o MACD nativos do MT5 usam médias simples
  (ATR = média simples do true range; linha de sinal do MACD = média simples), então podem diferir do gráfico do MT5.
- `tamanho_posicao` não inclui comissão nem swap; contas Raw Spread/Zero cobram comissão por lote.
- Posições e ordens pendentes: use `posicoes`. `resultado_se_atingido` vai do preço de entrada até o stop
  ou alvo atual (negativo = perda); `variacao_desde_agora` vai do preço atual até ele. `distancia_do_preco_atual`
  positiva = nível ainda não atingido (`ultrapassado` = o preço já passou dele). Nos totais, `perda_nos_stops`
  é o tamanho da perda (valor positivo). `pontos` são pontos do MT5; nos índices, cite a distância em `preco`.
  Lucro protegido não é garantido: em gaps o stop executa pior. Só trate preço atual e lucro aberto como de
  agora quando a `cotacao` do item estiver "atual". Os valores são medições: não dizem se o risco é adequado.
- Símbolos podem ser informados sem sufixo (EURUSD); o servidor resolve para o nome da conta (ex.: EURUSDm).
- Calendário: identifique a medida pelo `codigo`/`descricao`/`medida` (os nomes traduzidos podem estar
  errados). Compare realizado e `previsao` na mesma unidade (a `surpresa` já vem calculada). A previsão é
  do calendário do MT5 e pode não ser o consenso de mercado: não a chame de consenso. Ausente é null,
  não zero. Com `estimativa` revisada, o `anterior` é a estimativa anterior do mesmo período. Separe o
  fato publicado da sua interpretação e não transforme surpresa em compra ou venda. Para medir reação
  de preço, use horários UTC alinhados e cite os eventos de `mesmo_horario`.
- Reação a eventos (`reacao_evento`): apresente em três partes separadas: (1) o fato publicado e a surpresa;
  (2) o movimento medido, comparando instrumentos pela variação em %; (3) só então a sua interpretação, com as
  incertezas (outros eventos no mesmo horário ou em `eventos_dentro_da_janela`, mercado fechado, referência
  antiga). Compare instrumentos por `vezes_o_tipico` (movimento / movimento típico das 2 h antes), não só por %.
  O calendário é só dos EUA: eventos de outros países não aparecem. `leitura_da_fonte` é a interpretação da
  MetaQuotes, não um fato. Não atribua causa como certeza e não transforme a reação em compra ou venda.
- `contexto_mercado` mostra os instrumentos operados lado a lado; correlação aparente não é causa.
- Journal: rode `journal_sincronizar` antes de listar, anotar ou tirar estatísticas (importa do MT5 sem
  duplicar e sem apagar anotações). Use `journal_anotar` para setup, motivo, observações e o stop inicial
  quando faltar; reaproveite os nomes de `setups_existentes`. As estatísticas vêm prontas do servidor: não
  recalcule; cite o tamanho da amostra e os dados faltantes (sem stop inicial = sem R).
- Os dados servem para estudo e análise; não são recomendação de investimento.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
JOURNAL_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Gravam só no journal local; nada é apagado.
JOURNAL_SYNC = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
JOURNAL_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


@contextlib.contextmanager
def _tool_errors() -> Iterator[None]:
    """Converte falhas previstas em ToolError, cuja mensagem chega ao modelo."""
    try:
        yield
    except ToolError:
        raise
    except (MT5Error, SecEdgarError, CalendarError, JournalError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        # Sem isto o modelo veria só "Error executing tool X", sem pista do que houve.
        logger.exception("Erro inesperado em tool")
        raise ToolError(f"Erro inesperado ({type(exc).__name__}): {exc}") from exc


def _round(value: float, digits: int) -> float:
    return round(float(value), digits)


def _candle_warnings(df: Any) -> list[str]:
    """Avisos sobre a confiabilidade dos candles devolvidos por MT5Client.rates."""
    warnings: list[str] = []
    if not df.attrs.get("conectado", True):
        warnings.append(
            "Terminal sem conexão com a corretora: os candles mais recentes podem estar faltando e o "
            "último pode não ser atualizado."
        )
    if df.attrs.get("horario_inconsistente"):
        warnings.append(
            "O último candle abre depois do horário UTC atual: o servidor pode não usar UTC ou o relógio "
            "do Windows está errado. Não use os horários sem conferir."
        )
    return warnings


def create_server(
    settings: Settings | None = None,
    mt5_client: MT5Client | None = None,
    sec_client: SecEdgarClient | None = None,
    calendar: EconomicCalendar | None = None,
    journal: Journal | None = None,
) -> MCPServer:
    settings = settings or load_settings()
    mt5 = mt5_client or MT5Client(settings)
    sec = sec_client or SecEdgarClient(settings.sec_user_agent)
    calendar = calendar or EconomicCalendar(
        lambda: Path(mt5.terminal_data_path()) / "MQL5" / "Files" / "trading_mcp" / f"calendar_{CALENDAR_COUNTRY}.json"
    )
    journal = journal or Journal(settings.journal_path, settings.journal_export_dir, mt5, calendar)

    server = MCPServer(name="trading-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    @server.tool(annotations=READ_ONLY)
    def cotacao(
        simbolo: Annotated[str, Field(description="Símbolo, ex.: EURUSD, XAUUSD, AAPL")],
    ) -> dict[str, Any]:
        """Última cotação do símbolo: bid, ask, spread, horário (UTC/São Paulo/Nova York), idade e estado.

        Só é preço atual quando `estado` = "atual".
        """
        with _tool_errors():
            return mt5.quote(simbolo)

    # Sem saída estruturada: o SDK duplicaria os candles e formataria cada valor numa linha.
    @server.tool(annotations=READ_ONLY, structured_output=False)
    def historico(
        simbolo: Annotated[str, Field(description="Símbolo, ex.: EURUSD")],
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1, W1 ou MN1")] = "H1",
        quantidade: Annotated[int, Field(ge=1, le=MAX_CANDLES_OUTPUT, description="Número de candles")] = 100,
        incluir_candle_atual: Annotated[
            bool, Field(description="Incluir o candle que ainda está se formando")
        ] = True,
    ) -> dict[str, Any]:
        """Candles OHLC do símbolo em CSV, do mais antigo para o mais recente, com um resumo do período."""
        with _tool_errors():
            resolved = mt5.resolve_symbol(simbolo)
            digits = mt5.symbol_spec(resolved)["digitos"]
            df = mt5.rates(resolved, timeframe, quantidade, include_current=incluir_candle_atual)
            fmt = f"{{:.{digits}f}}"
            last = df["time"].iloc[-1].to_pydatetime()
            lines = ["horario_utc,abertura,maxima,minima,fechamento,volume_ticks"]
            lines += [
                ",".join(
                    [
                        row.time.strftime("%Y-%m-%d %H:%M"),
                        fmt.format(row.open),
                        fmt.format(row.high),
                        fmt.format(row.low),
                        fmt.format(row.close),
                        str(int(row.tick_volume)),
                    ]
                )
                for row in df.itertuples(index=False)
            ]
            first_open = float(df["open"].iloc[0])
            last_close = float(df["close"].iloc[-1])
            result: dict[str, Any] = {
                "simbolo": resolved,
                "timeframe": timeframe.upper(),
                "fuso_horario": "UTC",
                "diferenca_utc_no_ultimo_candle": tempo.offsets(last),
                "ultimo_candle": {**tempo.exibicao(last), "em_formacao": df.attrs["ultimo_em_formacao"]},
                "quantidade": len(df),
                "resumo": {
                    "maxima": _round(df["high"].max(), digits),
                    "minima": _round(df["low"].min(), digits),
                    "variacao_pct": round((last_close / first_open - 1) * 100, 2) if first_open else None,
                },
                "candles_csv": "\n".join(lines),
            }
            warnings = _candle_warnings(df)
            if warnings:
                result["avisos"] = warnings
            return result

    @server.tool(annotations=READ_ONLY)
    def indicadores(
        simbolo: Annotated[str, Field(description="Símbolo, ex.: EURUSD")],
        lista: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Indicadores: RSI(14), SMA(50), EMA(200), MACD(12,26,9), ATR(14), BB(20,2). "
                    "Vazio = RSI, MACD, EMA 20/50 e ATR."
                )
            ),
        ] = None,
        timeframe: Annotated[str, Field(description="M1, M5, M15, M30, H1, H4, D1, W1 ou MN1")] = "H1",
        incluir_candle_atual: Annotated[
            bool, Field(description="Calcular incluindo o candle que ainda está se formando")
        ] = True,
    ) -> dict[str, Any]:
        """Valores atuais (e anteriores) de indicadores técnicos, calculados como no TradingView."""
        with _tool_errors():
            specs = lista or DEFAULT_INDICATORS
            wanted = indicators.required_bars(specs)  # valida as specs antes de tocar no MT5
            resolved = mt5.resolve_symbol(simbolo)
            digits = mt5.symbol_spec(resolved)["digitos"]
            df = mt5.rates(resolved, timeframe, min(wanted, settings.max_bars), include_current=incluir_candle_atual)
            notes: list[str] = []
            if len(df) < wanted:
                notes.append(
                    f"Histórico disponível ({len(df)} candles) menor que o recomendado ({wanted}); "
                    "médias longas podem diferir levemente das plataformas de gráfico."
                )
            forming = df.attrs["ultimo_em_formacao"]
            if forming:
                notes.append("O último candle ainda está em formação: os valores mudam até ele fechar.")
            notes += _candle_warnings(df)
            return {
                "simbolo": resolved,
                "timeframe": timeframe.upper(),
                "candle_referencia": {**tempo.exibicao(df["time"].iloc[-1].to_pydatetime()), "em_formacao": forming},
                "fechamento": _round(df["close"].iloc[-1], digits),
                "candles_usados": len(df),
                "indicadores": indicators.compute(df, specs, digits=digits),
                "observacoes": notes,
            }

    @server.tool(annotations=READ_ONLY)
    def tamanho_posicao(
        simbolo: Annotated[str, Field(description="Símbolo, ex.: EURUSD")],
        entrada: Annotated[float, Field(gt=0, description="Preço de entrada")],
        stop: Annotated[float, Field(gt=0, description="Preço do stop loss")],
        risco_percentual: Annotated[
            float, Field(gt=0, le=100, description="Percentual do saldo a arriscar na operação")
        ] = 1.0,
        saldo: Annotated[
            float | None, Field(gt=0, description="Saldo base; vazio = saldo atual da conta conectada")
        ] = None,
    ) -> dict[str, Any]:
        """Calcula o lote para arriscar X% do saldo, usando a especificação real do contrato na corretora.

        A direção é deduzida: stop abaixo da entrada = compra; acima = venda.
        """
        with _tool_errors():
            resolved = mt5.resolve_symbol(simbolo)
            spec = mt5.symbol_spec(resolved)
            distance = abs(entrada - stop)
            if distance < spec["tick_size"]:
                raise ValueError(
                    f"Stop a {distance:.10g} da entrada: menor que o tick mínimo do símbolo ({spec['tick_size']}). "
                    "Confira os preços de entrada e stop."
                )
            account = mt5.account()
            balance = saldo if saldo is not None else float(account["saldo"])
            pre_warnings: list[str] = []
            if saldo is None and not account["is_demo"]:
                pre_warnings.append(f"Saldo da conta conectada, que NÃO é demo (tipo: {account['tipo_conta']}).")
            if not account["conectado"]:
                pre_warnings.append("Terminal sem conexão com a corretora: saldo e cotação podem estar desatualizados.")
            loss, side = mt5.loss_per_lot(resolved, entrada, stop)
            size = risk.position_size(
                balance=balance,
                risk_percent=risco_percentual,
                loss_per_lot=loss,
                volume_min=spec["volume_min"],
                volume_max=spec["volume_max"],
                volume_step=spec["volume_step"],
            )
            warnings = pre_warnings + list(size.warnings)

            digits = spec["digitos"]
            stop_distance: dict[str, Any] = {
                "preco": _round(distance, digits),
                "pontos": round(distance / spec["ponto"]),
                "pips": round(distance / risk.pip_size(spec["ponto"], digits), 1) if spec["is_forex"] else None,
            }

            margin = None
            if size.lots > 0:
                value = mt5.margin(resolved, side, entrada, size.lots)
                if value is not None:
                    margin = round(value, 2)
                    if saldo is None and margin > float(account["margem_livre"]):
                        warnings.append(
                            f"Margem estimada ({margin}) maior que a margem livre da conta "
                            f"({account['margem_livre']}): a ordem seria recusada."
                        )

            # Compra executa no ask e o stop é acionado pelo bid: se a entrada informada for o preço do
            # gráfico (bid), a perda real cresce pelo spread. Venda entra no bid e o stop é acionado
            # pelo ask, então a perda é a própria distância informada.
            risk_with_spread = None
            current_spread: dict[str, Any] | None = None
            try:
                quote = mt5.quote(resolved)
            except MT5Error:
                warnings.append("Sem cotação atual: não foi possível estimar o efeito do spread.")
            else:
                if quote["estado"] != "atual":
                    detail = f" {quote['aviso']}" if quote.get("aviso") else ""
                    warnings.append(
                        f"Spread e conferência da entrada usam uma cotação não atual ({quote['estado']}, "
                        f"idade {tempo.describe_age(quote['idade_s'])}).{detail}"
                    )
                mid = (quote["bid"] + quote["ask"]) / 2
                if abs(entrada - mid) / mid * 100 > ENTRY_DISTANCE_WARNING_PCT:
                    warnings.append(
                        f"A entrada ({entrada}) está a mais de {ENTRY_DISTANCE_WARNING_PCT:.0f}% do preço atual "
                        f"({_round(mid, digits)}). Confira se o preço está correto."
                    )
                spread = quote["ask"] - quote["bid"]
                current_spread = {"preco": _round(spread, digits), "pontos": quote["spread_pontos"]}
                if spread / distance > 0.10:
                    warnings.append(
                        f"O spread atual ({quote['spread_pontos']} pontos) equivale a {spread / distance:.0%} "
                        "da distância do stop: um stop tão curto pode ser acionado pelo próprio spread."
                    )
                if side == "buy":
                    risk_with_spread = round(size.lots * loss * (1 + spread / distance), 2)
                else:
                    risk_with_spread = round(size.risk_actual, 2)

            return {
                "simbolo": resolved,
                "direcao": "compra" if side == "buy" else "venda",
                "entrada": entrada,
                "stop": stop,
                "distancia_stop": stop_distance,
                "moeda_conta": account["moeda"],
                "saldo_base": round(balance, 2),
                "risco_percentual": risco_percentual,
                "risco_alvo": round(size.risk_target, 2),
                "lotes": size.lots,
                "risco_real": round(size.risk_actual, 2),
                "risco_real_percentual": round(size.risk_actual_percent, 2),
                "risco_com_spread": risk_with_spread,
                "spread_atual": current_spread,
                "nota_spread": (
                    "Compra: se a entrada for o preço do gráfico (bid), a execução ocorre no ask e o risco "
                    "sobe pelo spread atual (risco_com_spread). Venda: o risco é a distância informada. "
                    "Usa o spread de agora; ele pode ser maior em notícias e na virada do dia."
                ),
                "perda_por_lote": round(size.loss_per_lot, 2),
                "margem_estimada": margin,
                "custos_nao_incluidos": "comissão (contas Raw Spread/Zero cobram por lote) e swap",
                "avisos": warnings,
            }

    @server.tool(annotations=READ_ONLY)
    def info_conta() -> dict[str, Any]:
        """Saldo, margem, posições abertas e travas de negociação do terminal (somente leitura)."""
        with _tool_errors():
            account = mt5.account()
            result: dict[str, Any] = {"conta": account, "terminal": mt5.terminal(), "posicoes": mt5.positions()}
            warnings: list[str] = []
            if not account["is_demo"]:
                warnings.append(
                    "ATENÇÃO: a conta conectada NÃO é demo. Este servidor deveria apontar para uma "
                    "instalação do MT5 logada apenas na conta demo (MT5_PATH no .env)."
                )
            if not mt5.account_pinned:
                warnings.append(
                    "Conta não fixada no .env (MT5_LOGIN/MT5_SERVER): uma troca de conta no terminal "
                    "não seria bloqueada."
                )
            if not account["conectado"]:
                warnings.append("Terminal sem conexão com a corretora: valores podem estar desatualizados.")
            if warnings:
                result["avisos"] = warnings
            return result

    @server.tool(annotations=READ_ONLY)
    def posicoes(
        simbolo: Annotated[str, Field(description="Filtrar por símbolo, ex.: USTEC; vazio = todos")] = "",
        incluir_pendentes: Annotated[bool, Field(description="Incluir ordens pendentes")] = True,
    ) -> dict[str, Any]:
        """Posições abertas e ordens pendentes: preço atual e estado da cotação, distância até stop e alvo,
        resultado se forem atingidos (moeda da conta e % do saldo), duração e exposição por símbolo.

        Mede o stop e o alvo como estão agora; não avalia se o risco é adequado.
        """
        with _tool_errors():
            return posicoes_report.build(mt5, simbolo, incluir_pendentes)

    @server.tool(annotations=READ_ONLY)
    def simbolos(
        busca: Annotated[str, Field(description="Texto no nome ou na descrição, ex.: USD, gold, Apple")] = "",
        limite: Annotated[int, Field(ge=1, le=200, description="Máximo de resultados")] = 30,
    ) -> list[dict[str, Any]]:
        """Procura símbolos disponíveis na conta (forex, metais, índices, ações)."""
        with _tool_errors():
            return mt5.search_symbols(busca, limite)

    @server.tool(annotations=READ_ONLY)
    def calendario(
        horas_a_frente: Annotated[
            float, Field(ge=0, le=336, description="Horas à frente de agora (0.5 = próximos 30 minutos)")
        ] = 24,
        horas_atras: Annotated[
            float, Field(ge=0, le=168, description="Horas para trás, para eventos já divulgados")
        ] = 2,
        importancia_minima: Annotated[
            Literal["baixa", "moderada", "alta"], Field(description="Importância mínima atribuída pela fonte")
        ] = "moderada",
        busca: Annotated[
            str, Field(description="Texto no nome do evento, ex.: CPI, Nonfarm, FOMC, Jobless Claims, PPI, ISM")
        ] = "",
        limite: Annotated[int, Field(ge=1, le=100, description="Máximo de eventos")] = 40,
    ) -> dict[str, Any]:
        """Calendário econômico dos EUA (fonte: MetaTrader 5): horário (UTC/São Paulo/Nova York), importância,
        realizado, previsão da fonte, anterior, anterior revisado e surpresa na unidade do indicador.

        "Tem evento nos próximos 30 minutos?": horas_a_frente=0.5. "O indicador saiu?": horas_atras e busca.
        """
        with _tool_errors():
            return calendar.query(
                hours_back=horas_atras,
                hours_ahead=horas_a_frente,
                min_importance=importancia_minima,
                search=busca,
                limit=limite,
            )

    @server.tool(annotations=READ_ONLY)
    def reacao_evento(
        evento: Annotated[
            str, Field(description="Evento no calendário, ex.: CPI, NFP, claims, FOMC (pega o mais recente já divulgado)")
        ] = "",
        horario_utc: Annotated[
            str, Field(description="Horário exato do evento em UTC, ex.: 2026-10-01T12:30:00Z (eventos com mais de 7 dias)")
        ] = "",
        simbolos: Annotated[
            list[str] | None, Field(description="Instrumentos; vazio = os operados (configurados no .env)")
        ] = None,
        janelas_min: Annotated[
            list[int] | None, Field(description=f"Minutos depois do evento (1 a {reacao.MAX_WINDOW_MIN}); padrão 1, 5 e 15")
        ] = None,
    ) -> dict[str, Any]:
        """Como os instrumentos se moveram depois de um evento: o fato publicado (realizado, previsão, surpresa) e,
        separado, o movimento medido em +1/+5/+15 min, a maior alta e a maior queda e o spread. Não atribui causa.
        """
        with _tool_errors():
            return reacao.reaction(
                mt5,
                calendar,
                when=horario_utc,
                search=evento,
                symbols=simbolos or settings.instruments,
                windows=janelas_min or reacao.DEFAULT_WINDOWS,
            )

    @server.tool(annotations=READ_ONLY)
    def contexto_mercado(
        simbolos: Annotated[
            list[str] | None, Field(description="Instrumentos; vazio = os operados (configurados no .env)")
        ] = None,
    ) -> dict[str, Any]:
        """Retrato entre ativos agora: variação em 15 min, 1 h, 4 h e no dia (UTC), faixa do dia e estado da
        cotação de cada instrumento operado."""
        with _tool_errors():
            return reacao.context(mt5, simbolos or settings.instruments)

    @server.tool(annotations=JOURNAL_SYNC)
    def journal_sincronizar(
        dias: Annotated[float, Field(gt=0, le=366, description="Dias de histórico do MT5 a importar")] = 7,
    ) -> dict[str, Any]:
        """Importa para o journal local as operações da conta (posições do MT5): entrada, saída, volume, custos,
        resultado, stop inicial e notícia durante a operação. Pode rodar sempre: não duplica nem apaga anotações.
        """
        with _tool_errors():
            return journal.sync(dias)

    @server.tool(annotations=JOURNAL_WRITE)
    def journal_anotar(
        operacao_id: Annotated[int | None, Field(description="id da operação no journal")] = None,
        ticket: Annotated[int | None, Field(description="ticket da posição no MT5 (alternativa ao id)")] = None,
        setup: Annotated[str | None, Field(description="Nome do setup (reaproveite os já usados)")] = None,
        tags: Annotated[str | None, Field(description="Tags separadas por vírgula (substitui as atuais)")] = None,
        motivo: Annotated[str | None, Field(description="Motivo da entrada")] = None,
        observacao: Annotated[str | None, Field(description="Observação; é acrescentada com data e hora")] = None,
        stop_inicial: Annotated[
            float | None, Field(gt=0, description="Stop original, quando o MT5 não registrou ou registrou outro")
        ] = None,
        noticia: Annotated[
            Literal["sim", "nao"] | None, Field(description="Corrige a marcação de notícia durante a operação")
        ] = None,
    ) -> dict[str, Any]:
        """Anota uma operação do journal (setup, tags, motivo, observação, stop inicial, notícia).
        Os dados de execução vêm do MT5 e não mudam aqui.
        """
        with _tool_errors():
            return journal.annotate(
                operation_id=operacao_id,
                ticket=ticket,
                setup=setup,
                tags=tags,
                reason=motivo,
                note=observacao,
                initial_stop=stop_inicial,
                news=noticia,
            )

    @server.tool(annotations=JOURNAL_READ)
    def journal_listar(
        dias: Annotated[float | None, Field(ge=0, description="Operações abertas nos últimos N dias; vazio = todas")] = 30,
        simbolo: Annotated[str, Field(description="Filtrar por símbolo, ex.: USTEC")] = "",
        setup: Annotated[str, Field(description="Filtrar por setup")] = "",
        status: Annotated[Literal["aberta", "parcial", "fechada"] | None, Field(description="Filtrar por status")] = None,
        limite: Annotated[int, Field(ge=1, le=200, description="Máximo de operações")] = 30,
    ) -> dict[str, Any]:
        """Operações do journal, da mais recente para a mais antiga, com resultado, R, stop inicial, notícia e
        anotações. Traz também os setups já usados."""
        with _tool_errors():
            return journal.list_operations(days=dias, symbol=simbolo, setup=setup, status=status, limit=limite)

    @server.tool(annotations=JOURNAL_READ)
    def journal_estatisticas(
        dias: Annotated[float | None, Field(ge=0, description="Fechadas nos últimos N dias; vazio = todas")] = None,
        simbolo: Annotated[str, Field(description="Filtrar por símbolo")] = "",
        setup: Annotated[str, Field(description="Filtrar por setup")] = "",
    ) -> dict[str, Any]:
        """Estatísticas das operações fechadas: quantidade, taxa de acerto, resultado, ganho e perda médios,
        fator de lucro, R; por setup, símbolo, notícia e direção, com tamanho da amostra e dados faltantes."""
        with _tool_errors():
            return journal.stats(days=dias, symbol=simbolo, setup=setup)

    @server.tool(annotations=JOURNAL_WRITE)
    def journal_exportar() -> dict[str, Any]:
        """Grava uma cópia de segurança do journal e um CSV das operações (abre no Excel) na pasta de exportação."""
        with _tool_errors():
            return journal.export()

    @server.tool(annotations=READ_ONLY)
    def fundamentos(
        ticker: Annotated[str, Field(description="Ticker de ação dos EUA, ex.: AAPL, MSFT, BRK.B")],
    ) -> dict[str, Any]:
        """Fundamentos oficiais (SEC EDGAR): receita, lucro, LPA, balanço, margem e ROE do último ano e trimestre."""
        with _tool_errors():
            return sec.fundamentals(ticker)

    return server


def main() -> None:
    # stdout é o canal JSON-RPC: logs vão para stderr, em UTF-8 (o Claude Desktop lê o log assim;
    # no Windows o padrão seria cp1252 e os acentos sairiam trocados).
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    logger.info("Configuração: %s", settings.env_file or "nenhum .env encontrado (usando só variáveis de ambiente)")
    for error in settings.errors:
        logger.warning("Configuração inválida: %s", error)
    create_server(settings).run("stdio")


if __name__ == "__main__":
    main()
