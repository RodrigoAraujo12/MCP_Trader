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

## Calendário econômico (serviço `TradingMcpCalendar`, iniciado às 23:28 UTC)

O primeiro arquivo exportado tinha 150 eventos e 227 valores dos EUA, cobrindo de 7 dias atrás a 14 dias à frente. O JSON saiu em UTF-8 sem BOM, e a diferença do servidor para o GMT era 0.

- **Escala confirmada:** valor ÷ 10⁶ dá o número exibido, na unidade do multiplicador.
  - NFP de 2/10: previsão 52 e anterior 162, com `THOUSANDS` (mil empregos).
  - Continuing Claims: 1,701, com `MILLIONS`.
- **Anterior revisado:** no Initial Jobless Claims de 1/10 vieram anterior 197 e anterior revisado 198. O original e a revisão chegam separados.
- **Nomes traduzidos pelo terminal, com erro:** `consumer-price-index-mm` (CPI cheio) aparecia como "Núcleo do Índice de Preços ao Consumidor (IPC) (Mensal)", o mesmo nome de `consumer-price-index-ex-food-energy-mm` (núcleo). Use sempre o `event_code`.
- **A previsão do MT5 não é necessariamente consenso:** o NFP de 2/10 tinha previsão de 52 mil no MT5 e consenso de 89 mil no Forex Factory. Algumas previsões têm 3 casas decimais (por exemplo, balança comercial −81,515), típicas de modelo.
- **Revisões:** `revision` 0 indica divulgação única; 1, primeira estimativa; 2 ou mais, estimativa revisada. Exemplo: os estoques no atacado de agosto saíram em 30/09 com revisão 1 e realizado 0,7, e o valor de 08/10, com revisão 3 e o mesmo período, traz `prev` = 0,7. Por isso, numa estimativa revisada, `anterior` é a estimativa anterior do mesmo período.
- **Feriados:** Columbus Day vem com importância `NONE` e horário 00:00 (modo `DATE`).
- **Realizado ausente:** o PMI industrial da S&P Global (`markit-manufacturing-pmi`, 13:45 UTC) continuava sem realizado cerca de 10 h depois. O ISM das 14:00 UTC tinha realizado.

## Posições e ordens pendentes (etapa B1, 2026-10-02 ~04:05 UTC)

Conferido com uma venda a mercado de 0,01 BTCUSDm (stop e alvo) e uma compra limitada de 0,01 BTCUSDm (stop e alvo), abertas à mão na demo.

- `positions_get()` e `orders_get()` sem itens devolvem tupla vazia, com `last_error` `(1, 'Success')`; None fica só para erro.
- Campos da posição: `ticket, time, time_msc, time_update, time_update_msc, type, magic, identifier, reason, volume, price_open, sl, tp, price_current, swap, profit, symbol, comment, external_id`. Não há comissão.
- Campos da ordem: `ticket, time_setup, time_setup_msc, time_done, time_done_msc, time_expiration, type, type_time, type_filling, state, magic, position_id, position_by_id, reason, volume_initial, volume_current, price_open, sl, tp, price_current, price_stoplimit, symbol, comment, external_id`.
- `price_current` da posição de venda = ask; da compra limitada = ask (o lado que a ativa). Uma posição de compra não foi aberta; pela simetria deve ser o bid.
- `order_calc_profit` dá perda negativa (venda até o stop: −1,41; até o alvo: +5,83) e bate com `profit` da posição (0,19 = 0,19; 1,27 = 1,27).
- `symbol_info` diferencia maiúsculas (`USTECM` não existe); `USTECm`, `USTEC` e `US30m` resolvem certo.
- BTCUSDm é classificado como forex pelo `trade_calc_mode`: aparece "pips" igual aos pontos. Não afeta os índices.
- O relatório `posicoes` conferiu à mão: distâncias, resultado no stop/alvo, relação alvo/stop, tempo aberta e totais.

## Histórico de negócios (etapa B3, 2026-10-02 ~04:20 UTC)

- `history_deals_get` com `datetime` **sem fuso** é lido como horário local do Windows (UTC−3 aqui): a janela 04:00–05:00 sem fuso não trouxe nada; com fuso UTC ou com epoch inteiro, trouxe os 2 negócios. O cliente sempre manda datas com fuso.
- Campos do negócio: `ticket, order, time, time_msc, type, entry, magic, position_id, reason, volume, price, commission, swap, profit, fee, symbol, comment, external_id`. O depósito inicial aparece como `type` 2 (saldo), `position_id` 0.
- A ordem a mercado guarda o stop e o alvo enviados na boleta (`sl` 85726,68 / `tp` 85002,95), e o preço pedido difere da execução (ordem 85571,1; negócio 85585,85). Por isso a entrada vem do negócio e o stop inicial da ordem.
- Fechamento manual pelo terminal: negócio de saída com `reason` 0 (cliente) e ordem própria sem stop/alvo. A ordem pendente cancelada ficou no histórico com `state` 2 e `position_id` 0.
- Journal sincronizado com essa operação: entrada 85585,85, saída 85448,40, +1,38, risco inicial 1,41, R 0,98; a segunda sincronização não duplicou.

## Instrumentos operados e reação a eventos (etapa C, 2026-10-02 ~04:30 UTC)

| Instrumento | Símbolo | Observação |
|---|---|---|
| Nikkei 225 | `JP225m` | CFD, lucro em **JPY**, 1 casa decimal |
| Ouro | `XAUUSDm` | Modo de cálculo forex, base XAU, contrato 100 |
| Libra / Euro | `GBPUSDm`, `EURUSDm` | 5 casas |
| Bitcoin | `BTCUSDm` | Modo de cálculo forex, contrato 1, spread ~10 USD |
| Brent | `UKOILm` | CFD, contrato 1000 |
| Índice do dólar | `DXYm` | CFD (modo 4), contrato 1000 |

Os índices da Exness vêm com moeda base e de lucro = USD (por isso "pips" só para pares de moedas de verdade).

- `copy_rates_range` e `copy_ticks_range` com datas com fuso funcionam; todos os 9 tinham M1 e ticks no Jobless Claims de 1/10, 12:30 UTC.
- Reação medida em 5 min, em % e em vezes o movimento típico das 2 h antes: US30 +0,12% (3,8×), ouro +0,10% (2,3×), DXY −0,03% (1,7×), JP225 +0,07% (1,6×), EURUSD +0,04% (1,6×), BTC +0,09% (1,9×), GBPUSD +0,03% (1,4×), USTEC +0,03% (0,6×), UKOIL +0,04% (0,4×). Pela % o BTC parecia reagir mais que o DXY; pelo típico, os dois ficaram parecidos, e o USTEC ficou dentro do ruído. O Claims veio 5 mil acima da previsão, mas os contínuos vieram abaixo e não há como separar o efeito de cada um.
- `contexto_mercado` às 04:40 UTC: o UKOIL não negocia na virada do dia (último preço às 21:00 UTC, primeiro candle às 00:10), por isso a variação do dia parte do preço das 21:00 e vem marcada com `desde`.
- Spread na conta demo: praticamente fixo na divulgação (USTEC 112 pontos antes e depois; JP225 31 → 34; ouro 240 → 260; UKOIL 44 → 42). A conta real pode se comportar diferente.

## Reações guardadas (etapa C2, 2026-10-02 ~10:00 UTC)

- Início do M1 que o terminal entrega (`copy_rates_from_pos` com 99.999 candles, limite de 100.000 do terminal): UKOIL 11/06 09:26, JP225 22/06 20:26, US30 23/06 11:37, XAUUSD 23/06 13:08, USTEC 23/06 13:14, EURUSD 25/06 07:45, GBPUSD 25/06 22:24, DXY 26/06 08:13, **BTCUSD 24/07 22:34** (negocia 24 h, então 100 mil candles cobrem menos dias). Todos em UTC.
- `copy_rates_range` pedindo 100 dias de M1 de uma vez falha com "Invalid params" (passa do limite de candles): a medição é feita evento a evento (3 h de M1 + ticks de 7 min por instrumento).
- Tempo: com o histórico já carregado no terminal, cada chamada leva milissegundos e um horário de divulgação com os 9 instrumentos leva ~0,3–0,5 s. A primeira leitura de um período antigo leva alguns segundos (o terminal baixa o histórico). Com o calendário de 7 dias: 48 divulgações, 22 horários, 198 medições em ~10 s.
- Os números guardados batem com `reacao_evento` no mesmo horário (Claims de 1/10, 12:30 UTC: USTEC −0,008/+0,025/−0,038% e ouro +0,044/+0,102/+0,066% em 1/5/15 min).

## Ainda não reproduzido no terminal real

- Troca de conta e perda de conexão com o servidor em execução. Estão cobertas por testes com MT5 simulado.
- Latência do calendário numa divulgação real. A primeira oportunidade é o NFP de 2/10, às 12:30 UTC, com o serviço rodando antes.
