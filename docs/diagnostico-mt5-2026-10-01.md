# Diagnóstico do MT5 real — 2026-10-01

Sondagens **somente leitura** feitas por volta de 22:00 UTC (quinta-feira, horário de verão dos EUA) num terminal dedicado logado numa conta **demo Standard** da Exness, servidor `Exness-MT5Trial11`. Pacote `MetaTrader5` 5.0.6231, terminal build 6231. Nenhuma função de ordem foi chamada.

## Conta e terminal

| Item | Valor medido |
|---|---|
| `account_info().trade_mode` | `0` (demo), confirmado no painel da Exness |
| Tipo de conta | Standard: símbolos com sufixo `m` |
| `margin_mode` | `2` (hedging: várias posições por símbolo) |
| `terminal_info().trade_allowed` | `False` (botão Algo Trading desligado) |
| `terminal_info().tradeapi_disabled` | `True` depois de marcar "Desativar negociação automática via API Python externa" |
| `terminal_info().maxbars` | 100000 |

## Base de tempo

- Horário do tick (`time_msc`) menos o relógio UTC do computador: entre −0,8 e +1,2 s em USTECm, US30m, US500m, DXYm, EURUSDm e BTCUSDm.
- **Conclusão: o servidor da Exness está em UTC (diferença 0).** A documentação Python do MT5 ("UTC") e a prática relatada no fórum ("horário do servidor") coincidem nesta corretora.
- **A reconferir depois de 1/11/2026**, quando termina o horário de verão dos EUA. A Exness diz que o fuso não muda.

## Símbolos (356 na conta)

| Necessidade | Símbolo | Observação |
|---|---|---|
| Nasdaq 100 | `USTECm` | CFD (`trade_calc_mode` 2). 1 lote = US$1 por ponto. Lote mínimo 0,05; passo 0,01 |
| Dow Jones | `US30m` | CFD. 1 lote = US$1 por ponto. Lote mínimo 0,05 |
| S&P 500 | `US500m` | CFD. 1 lote = US$1 por ponto. **Lote mínimo 0,14** |
| Índice do dólar | `DXYm` | "US Dollar Index", CFD (`trade_calc_mode` 4), contrato 1000. Não se sabe se acompanha o futuro ou o índice à vista |
| Brent | `UKOILm` | CFD, contrato 1000, lote mínimo 0,01 |
| Treasury 10 anos | — | **Nenhum** instrumento de título ou rendimento na conta |

Também existem variantes ampliadas (`USTEC_x100m`, `US30_x10m`, `US500_x100m`). A resolução de sufixo ignora essas variantes e escolhe `USTECm`, `US30m` e `US500m`.

## Sessões observadas (M1 de 2026-09-30, horário de verão dos EUA)

| Símbolo | Sem negociação |
|---|---|
| USTECm, US30m | 21:00–21:59 UTC (17:00–17:59 em Nova York) |
| DXYm | Negociação esparsa entre 21:00 e 21:27 UTC |
| UKOILm | Último candle às 20:53 UTC; a reabertura não foi observada |

A Exness informa os spreads do reinício da sessão: USTECm 112 pontos (1,12 ponto de índice); EURUSDm 48 pontos às 21:59 UTC (virada do dia).

## Histórico disponível

- `copy_rates_from_pos` aceita no máximo `maxbars - 1` candles: 99.999 funcionou; 100.000 deu `(-2, 'Terminal: Invalid params')`.
- **M1** de USTECm alcança até 2026-06-23 (99.999 candles). Antes disso o terminal não devolve dados (limite "Max bars in chart").
- **Ticks** de USTECm existem em 2026-01-05; em 2025-10-01 não há nenhum. Eventos de 2026 podem ser reconstruídos a partir dos ticks.
- 2026-07-03 (feriado observado da Independência) aparece sem negociação nos índices. Cuidado com feriados em amostras de profundidade.
- A coluna `spread` dos candles M1 é **constante** (USTECm sempre 112, US30m sempre 13). Ela não mede alargamento de spread em notícias; para isso é preciso usar os ticks (bid/ask).

## Problemas confirmados no código da fase 1 (corrigidos na etapa A)

- `cotacao("UKOIL")` devolveu um tick de 67 min atrás como cotação normal, sem aviso. Agora o resultado traz `estado = "mercado_fechado_provavel"`.
- Os horários eram rotulados "horário do servidor" sem dizer que são UTC. Agora são UTC, com São Paulo e Nova York ao lado.

## Ainda não reproduzido no terminal real

- Troca de conta e perda de conexão com o servidor em execução. Estão cobertas por testes com MT5 simulado.
- Acesso do Service MQL5 ao calendário. A aba Calendário do terminal mostra eventos.
