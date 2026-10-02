# MCP_Trader

Servidor MCP (stdio) de análise de mercado para forex e ações dos EUA. Dá ao Claude acesso a cotações, histórico e indicadores técnicos via MetaTrader 5 (Exness), a um calculador de tamanho de posição que usa a especificação real do contrato na corretora, e a fundamentos de empresas americanas via SEC EDGAR.

**MT5 somente leitura**: não existe tool para enviar, alterar ou cancelar ordens, e o acesso a essas funções do MetaTrader 5 é bloqueado no código. As únicas gravações são no journal local (um arquivo SQLite no seu computador).

## Como você usa: interface ou terminal?

O servidor MCP não tem tela própria. Ele roda "por trás" de um aplicativo do Claude, e a interface é esse aplicativo: você conversa em português, e o Claude chama as ferramentas sozinho.

| Tarefa | Onde | Frequência |
|--------|------|------------|
| Instalar Python, criar o `.venv`, preencher o `.env` | Terminal (PowerShell) | Uma vez |
| Conectar o servidor ao Claude | Um comando ou um arquivo JSON | Uma vez |
| Pedir análises, cálculo de lote, fundamentos | Chat do Claude | Dia a dia, sem terminal |

Onde conversar com o Claude:

- **Claude Desktop** (recomendado para o dia a dia): aplicativo de chat com janela. Depois de conectado, basta perguntar, por exemplo: "Como está o EURUSD no H4?". O Claude também pode montar gráficos com os candles que o servidor devolve.
- **Claude Code** (terminal ou extensão do VS Code): mesmo funcionamento, num painel do editor. Mais prático quando você também está mexendo no código.
- **MCP Inspector**: página web para chamar cada ferramenta manualmente, preenchendo os campos. Serve para testar e depurar, não para uso diário.

Uma tela própria (gráficos interativos, calculadora com formulário) está no roadmap como opcional. Veja a seção [Roadmap](#roadmap).

## Requisitos

- **Windows** (a biblioteca Python do MetaTrader 5 só funciona no Windows)
- **Python 3.11+**
- **Terminal MetaTrader 5** da Exness logado numa **conta DEMO** (MT4 não funciona: a biblioteca Python só suporta MT5)

## Instalação

Abra o PowerShell na pasta do projeto e execute:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Configuração

Copie `.env.example` para `.env` e preencha:

```powershell
Copy-Item .env.example .env
```

| Variável | Descrição | Obrigatório? |
|----------|-----------|--------------|
| `MT5_PATH` | Caminho do `terminal64.exe` de uma instalação separada do MT5, logada só na demo | Não (padrão: terminal instalado) |
| `MT5_LOGIN` | Número da conta. Fixa a conta esperada (conferida a cada uso) e faz login automático | Não, mas recomendado |
| `MT5_PASSWORD` | Senha da conta; vazio usa a senha salva no terminal | Não |
| `MT5_SERVER` | Nome exato do servidor demo (ex.: `Exness-MT5Trial11`); também conferido a cada uso | Não, mas recomendado |
| `SYMBOL_SUFFIX` | Sufixo dos símbolos, se a detecção automática falhar (ex.: `m`) | Não |
| `SEC_USER_AGENT` | Identificação exigida pela SEC: `"Seu Nome seu@email.com"` | **Sim** (para `fundamentos`) |
| `MT5_TIMEOUT_MS` | Timeout de conexão com o MT5, em milissegundos | Não (padrão: 60000) |
| `MAX_BARS` | Limite de candles buscados por chamada | Não (padrão: 5000) |
| `JOURNAL_PATH` | Arquivo do journal (SQLite) | Não (padrão: `trading-mcp\journal.sqlite3` na pasta do seu usuário) |
| `JOURNAL_EXPORT_DIR` | Pasta das cópias de segurança e CSVs do journal | Não (padrão: `journal_export` no projeto) |
| `INSTRUMENTOS` | Instrumentos operados, separados por vírgula (reação a eventos e contexto entre ativos) | Não (padrão: `USTEC,US30,JP225,XAUUSD,GBPUSD,EURUSD,BTCUSD,UKOIL,DXY`) |
| `REACOES_PATH` | Banco das reações guardadas (SQLite) | Não (padrão: `trading-mcp\reacoes.sqlite3` na pasta do seu usuário) |
| `RISCO_POR_OPERACAO_PCT` | Risco máximo por operação, em % da base do dia | Não (padrão: 1,25) |
| `PERDA_MAXIMA_DIA_PCT` | Perda máxima no dia de mercado, em % da base do dia | Não (padrão: 5) |
| `PERDA_MAXIMA_SEMANA_PCT` | Perda máxima na semana de mercado, em % da base da semana | Não (padrão: 25) |
| `PROPOSTAS_PATH` | Banco das propostas de operação (SQLite) | Não (padrão: `trading-mcp\propostas.sqlite3` na pasta do seu usuário) |

O `.env` é lido da pasta raiz do projeto, mesmo que o servidor seja iniciado de outro lugar (`TRADING_MCP_ENV_FILE` aponta para outro arquivo, se preferir). Arquivos salvos pelo Bloco de Notas (com BOM ou UTF-16) funcionam. O `.env` está no `.gitignore` e nunca deve ir para o git.

### Segurança

Use uma instalação **separada** do MT5, logada **só** na conta demo, e aponte `MT5_PATH` para o `terminal64.exe` dela. O terminal precisa estar aberto e logado para as ferramentas funcionarem.

- Se `MT5_LOGIN` estiver preenchido, `MT5_PATH` passa a ser obrigatório. Sem ele, o login seria feito no seu terminal principal e trocaria a conta logada nele. O servidor recusa essa combinação.
- A cada uso, o servidor confere a conta logada contra `MT5_LOGIN` e `MT5_SERVER`. Com qualquer uma delas preenchida, o par conta/servidor da primeira conexão fica congelado, mesmo depois de o terminal reiniciar: se alguém trocar a conta, as ferramentas ficam bloqueadas até ele voltar para a conta original. Enquanto o terminal está aberto, o servidor não reloga sozinho; se o terminal reiniciar, a reconexão pede o login de `MT5_LOGIN`/`MT5_SERVER` (com as duas preenchidas, volta à conta do `.env`). Sem essas variáveis, a troca é aceita, mas o cache de símbolos é limpo.
- Se o terminal perder a conexão com a corretora, as cotações saem com `estado = "desconectado"` em vez de parecerem atuais, e `historico`/`indicadores` avisam que os candles recentes podem faltar.
- O acesso à biblioteca MetaTrader5 passa por uma lista de funções permitidas, todas de leitura. Isso evita chamadas acidentais neste código, mas não é uma barreira de segurança: a proteção real é não existir nenhuma tool de ordens.
- Camada extra no próprio terminal: em **Ferramentas → Opções → Expert Advisors**, marque **"Desativar negociação automática via API Python externa"** e deixe o botão Algo Trading desligado. A leitura continua funcionando; `info_conta` mostra as duas travas.
- A ferramenta `info_conta` avisa se a conta conectada não for demo ou não estiver fixada no `.env`.

## Ferramentas

| Nome | Descrição | Parâmetros principais |
|------|-----------|----------------------|
| `cotacao` | Última cotação: bid, ask, spread, horário (UTC/São Paulo/Nova York), idade e estado | `simbolo` (ex.: USTEC, EURUSD) |
| `historico` | Candles OHLC em CSV, com resumo do período | `simbolo`, `timeframe` (M1, M3, M5… MN1; padrão H1), `quantidade` (1–500, padrão 100), `incluir_candle_atual` (padrão: sim) |
| `indicadores` | RSI, MACD, EMA, SMA, ATR, Bollinger | `simbolo`, `lista` (padrão: RSI(14), MACD(12,26,9), EMA 20/50, ATR(14)), `timeframe`, `incluir_candle_atual` |
| `tamanho_posicao` | Lote para arriscar X% do saldo (cálculo livre; para os seus limites, use `proposta_operacao`) | `simbolo`, `entrada`, `stop`, `risco_percentual` (padrão: o seu limite por operação, 1,25%), `saldo` (padrão: saldo da conta) |
| `risco_conta` | Seus limites agora: saldo do início do dia e da semana de mercado, resultado, perda nos stops abertos e pendentes, quanto ainda cabe e o risco máximo da próxima operação | — |
| `proposta_operacao` | Proposta pelas suas regras (não envia ordem): lote, risco, tipo de ordem, risco/retorno, notícias na validade, contexto SMC; recusa com o motivo se estourar um limite | `simbolo`, `entrada`, `stop`, `alvo` (opcional), `validade_min` (5–240, padrão 30) |
| `propostas_listar` | Propostas guardadas e a situação (válida, expirada ou recusada) | `dias` (padrão 7), `simbolo`, `limite` |
| `info_conta` | Saldo, margem, posições abertas e travas de negociação do terminal | — |
| `posicoes` | Posições abertas e ordens pendentes: estado da cotação, distância até stop e alvo, resultado se forem atingidos, duração e exposição por símbolo | `simbolo` (vazio = todos), `incluir_pendentes` (padrão: sim) |
| `simbolos` | Busca símbolos disponíveis na conta | `busca` (ex.: USD, Apple), `limite` (1–200, padrão 30) |
| `calendario` | Calendário econômico dos EUA: horário (UTC/SP/NY), importância, realizado, previsão, anterior, anterior revisado e surpresa | `horas_a_frente` (padrão 24), `horas_atras` (padrão 2), `importancia_minima`, `busca` (ex.: CPI, NFP, claims, FOMC) |
| `fundamentos` | Fundamentos da SEC EDGAR (receita, lucro, LPA, ROE, margem) | `ticker` (ex.: AAPL, MSFT, BRK.B) |
| `reacao_evento` | Fato publicado (realizado, previsão, surpresa) e, separado, o movimento de cada instrumento depois do evento | `evento` (ex.: CPI, NFP, claims) ou `horario_utc`, `simbolos` (padrão: `INSTRUMENTOS`), `janelas_min` (padrão 1, 5, 15) |
| `contexto_mercado` | Instrumentos lado a lado: variação em 15 min, 1 h, 4 h e no dia, faixa do dia e estado da cotação | `simbolos` (padrão: `INSTRUMENTOS`) |
| `reacoes_registrar` | Guarda as divulgações dos EUA que o calendário cobre e a reação medida de cada instrumento (não duplica; se faltar tempo, rode de novo) | — |
| `reacoes_estatisticas` | Reações guardadas a um evento, agrupadas pela surpresa (acima, abaixo ou igual à previsão), com o tamanho da amostra | `evento` (vazio = o que está guardado), `simbolos`, `janelas_min` (1, 5, 15, 60; padrão 5 e 15), `dias` |
| `estrutura_smc` | Estrutura SMC (só medição): topos e fundos micro e macro, BOS/CHoCH por pavio e por fechamento, premium/discount, FVGs, order blocks, liquidez igual e varreduras; máxima/mínima do dia e da semana de mercado e das sessões, topos/fundos diários; alvos acima e abaixo | `simbolo`, `timeframes` (M1, M3, M5, M15, M30, H1, H4, D1; padrão M5, M15, H1, H4), `entrada` e `stop` (opcionais, para o risco/retorno de cada alvo) |
| `journal_sincronizar` | Importa as operações da conta do histórico do MT5 para o journal (não duplica, não apaga anotações) | `dias` (padrão 7) |
| `journal_anotar` | Anota uma operação: setup, tags, motivo, observação, stop inicial, notícia | `operacao_id` ou `ticket`, e os campos a gravar |
| `journal_listar` | Operações com resultado, R, stop inicial, notícia e anotações | `dias` (padrão 30), `simbolo`, `setup`, `status`, `limite` |
| `journal_estatisticas` | Estatísticas das operações fechadas, por setup, símbolo, notícia e direção | `dias` (vazio = todas), `simbolo`, `setup` |
| `journal_exportar` | Cópia de segurança do banco e CSV para o Excel | — |

**Estado da cotação**: `atual` (tick com até 60 s), `mercado_fechado_provavel` (sem ticks recentes e sem negociação, na semana anterior, no mesmo intervalo que hoje está sem ticks: pausa diária, fim de semana, fora da sessão), `atrasado` (sem ticks recentes, mas na semana anterior houve negociação nesse intervalo: feriado, atraso ou problema de conexão), `antigo` (não foi possível verificar, ou o último tick tem mais de uma semana), `desconectado` (terminal sem conexão com a corretora) e `horario_inconsistente` (tick à frente do relógio UTC: servidor fora de UTC ou relógio do Windows errado). Só `atual` deve ser tratado como preço de agora.

**Candle em formação**: `historico` e `indicadores` informam `em_formacao` pelo horário, não pela posição. Com `incluir_candle_atual = não`, só sai o candle que de fato está aberto; na pausa diária ou no fim de semana o último candle já fechou e é mantido.

**Indicadores**: seguem as convenções do TradingView (EMA, RSI e ATR com semente SMA, suavização de Wilder, Bollinger com desvio padrão populacional). Com o candle atual incluído, os valores mudam até ele fechar. O ATR e o MACD nativos do MT5 usam médias simples, então podem diferir levemente do gráfico do MT5.

**Posições**: para cada posição, `stop.resultado_se_atingido` vai do preço de entrada até o stop atual (negativo = perda; positivo = lucro protegido) e `variacao_desde_agora` vai do preço atual até ele; o mesmo para o alvo. A `situacao` do stop é `com_risco`, `no_preco_de_entrada`, `lucro_protegido` ou `sem_stop`. Distância positiva = nível ainda não atingido; `ultrapassado` marca um stop ou alvo que o preço já passou (gap ou cotação parada), e aí a variação desde agora fica nula. `pontos` são pontos do MT5 (no USTECm, 0,01): para índices, a distância em pontos do índice é o campo `preco`. Os totais somam posição por posição, sem compensar posições opostas (a conta é hedging): `perda_nos_stops` (valor positivo) só soma stops com risco e não inclui posições sem stop; se faltar algum valor, os totais saem nulos em vez de parciais. Ordens pendentes trazem o resultado se forem executadas e o stop for atingido (na stop limitada, a entrada é o preço da limitada). São medições sobre o stop **atual**: o risco inicial e o resultado em R ficam para o journal (B3). Comissão não incluída; swap à parte.

**Tamanho de posição**: a direção é deduzida (stop abaixo da entrada = compra). O resultado traz o risco com o lote arredondado, o efeito do spread atual nas compras (`risco_com_spread`) e a margem estimada. Ele recusa stop menor que o tick do símbolo e avisa quando o spread consome boa parte do stop ou quando a margem passa da margem livre. Comissão e swap não estão incluídos: em contas Raw Spread/Zero, some a comissão por lote.

## Reação a eventos e contexto entre ativos

`reacao_evento` separa três coisas que não devem se misturar: o **fato publicado** (do calendário: realizado, previsão, surpresa e outros eventos no mesmo horário), o **movimento medido** em cada instrumento e, por último, a interpretação, que fica com o Claude e deve citar as incertezas.

- **Medição**: candles M1 de bid do MT5, em UTC, com resolução de 1 minuto (o horário tem de ser em minuto cheio). Referência = último preço antes do horário do evento; +1, +5 e +15 min = último preço antes de cada marca. Também mostra a máxima e a mínima na janela e o spread (mediana nos 5 min antes, máximo nos 2 min depois, pelos ticks).
- **Movimento típico**: `vezes_o_tipico` divide o movimento pela mediana dos movimentos do mesmo tamanho nas 2 h antes do evento. Um 0,1% no EURUSD e um 0,1% no BTC não têm o mesmo peso; 3× o típico é uma reação clara, perto de 1× é ruído normal.
- **Eventos em volta**: o fato publicado traz os eventos dos EUA do mesmo horário; `eventos_dentro_da_janela` lista os divulgados durante a medição (importância moderada ou alta) e `janelas_afetadas` diz quais janelas misturam mais de um evento. **O calendário é só dos EUA**: BCE, BoE, BoJ e OPEP não aparecem. A leitura direcional da MetaQuotes (`leitura_da_fonte`) fica separada dos fatos.
- **Busca**: `evento` pega o dia mais recente com divulgação e, nele, o evento de maior importância mais cedo (em dia de FOMC, a decisão das 18:00, não a entrevista); os demais aparecem em `outros_candidatos`.
- **Avisos de dados**: referência antiga (mercado parado antes do evento), sem negociação depois (mercado fechado), candle faltando no fim da janela, janelas que ainda não terminaram (`pendente`).
- **Compare pela variação em %**: um ponto de USTEC, de ouro e de EURUSD não têm o mesmo peso. Pips só aparecem em pares de moedas (EURUSD, GBPUSD).
- **Alcance**: os candles M1 vão até cerca de 3 meses para trás (menos no BTCUSD, que negocia 24 h). A busca por nome olha os últimos 7 dias; para eventos mais antigos, informe `horario_utc` (o fato publicado só aparece se o arquivo do calendário cobrir a data: veja `InpDaysBack`).
- **Sem yields**: a conta não tem instrumento de Treasury; rendimentos não estão disponíveis. `DXYm` é um CFD da corretora e `JP225m` é cotado em ienes.

`contexto_mercado` mostra os instrumentos operados lado a lado (variação em 15 min, 1 h, 4 h e desde o último preço antes de 00:00 UTC, e onde o preço está na faixa do dia). `desde` marca quando o preço de partida é mais antigo, porque o mercado estava fechado (o UKOIL, por exemplo, para às 21:00 UTC). Serve para ver, por exemplo, o dólar subindo junto com a queda do ouro e dos índices. Correlação aparente não indica causa.

## Reações guardadas

O calendário do terminal cobre poucos dias para trás e o M1, cerca de 3 meses: o que não for guardado se perde. `reacoes_registrar` guarda num banco local cada divulgação dos EUA de importância moderada ou alta com horário exato (o fato: realizado, previsão, surpresa) e, separado, o movimento de cada instrumento operado em +1, +5, +15 e +60 min, medido do mesmo jeito que `reacao_evento` (os números batem). `reacoes_estatisticas` agrupa as divulgações de um evento pelo sentido da surpresa.

- **Rotina**: rode `reacoes_registrar` a cada 5 ou 6 dias (com `InpDaysBack` = 7) ou a cada poucas semanas (com 100). Cada chamada guarda tudo o que o arquivo do calendário cobre e avisa se houve **lacuna** desde a anterior. Pode rodar sempre: não duplica, e uma divulgação que estava sem realizado ganha o valor quando ele aparece. Mede do horário mais antigo para o mais novo e para em cerca de 40 s (o Claude Desktop desiste de uma ferramenta em cerca de 60 s); se faltar, avisa para rodar de novo.
- **Primeira carga**: para guardar os ~3 meses que o terminal ainda tem, mude `InpDaysBack` do serviço para 100 (veja a seção do calendário) e rode `reacoes_registrar` até não faltar nada e, 10 min depois, mais uma vez para confirmar as medições com falha.
- **Medições a confirmar**: uma medição com falha (sem negociação antes ou depois do evento, referência antiga, preço antigo no fim da janela, candles faltando) pode ser só histórico que o terminal ainda não baixou. Ela fica `a_confirmar`, é refeita nos registros seguintes e só entra nas estatísticas quando duas medições com pelo menos 10 min de distância dão o mesmo resultado; se o horário sair do histórico M1 antes disso, fica `nao_confirmavel`. Com o terminal sem conexão, nada é medido.
- **Histórico M1 por símbolo**: o terminal guarda um número fixo de candles, então símbolos que negociam 24 h (BTCUSD) cobrem menos dias. Divulgações anteriores ao início do M1 de um símbolo ficam sem medição para ele (`fora_do_historico_m1` diz desde quando há M1).
- **Estatísticas**: por instrumento e janela, `todas` traz o tamanho da amostra (`n`), a mediana do movimento absoluto em % e de `vezes_o_tipico` e quantas foram reação clara (3× o típico ou mais); `surpresa_acima`, `surpresa_abaixo` e `igual_a_previsao` trazem a mediana da variação em % e quantas vezes subiu ou caiu. `ultimas_divulgacoes` lista as mais recentes com o movimento de cada instrumento.
- **Fora das contas** (`fora_das_contas`), por motivo: `sem_medicao` (fora do histórico M1 ou ainda não medida), `sem_referencia` (sem negociação nas 2 h antes), `referencia_antiga` (mercado parado antes do evento), `sem_negociacao` (nada depois do evento), `preco_antigo` (o último preço da janela é de mais de 5 min antes do fim dela), `a_confirmar` e `nao_confirmavel`.
- **Misturas**: eventos no mesmo horário dividem a mesma reação (payroll, desemprego e salários saem juntos): veja `no_mesmo_horario`. `com_outro_evento_na_janela` conta divulgações com outro evento moderado ou alto dentro da janela. O calendário é só dos EUA.
- **Amostra pequena**: menos de 20 divulgações = `amostra_pequena`. Em 3 meses, um evento semanal (claims) chega a ~13 divulgações e um mensal (CPI, payroll) a ~3; a amostra cresce conforme você guarda.
- **Fatos guardados depois**: a divulgação guarda a previsão como o calendário a mostrava no registro; `registradas_depois_do_dia` conta as guardadas mais de 24 h depois da divulgação (na primeira carga, quase todas).
- **Onde fica**: `trading-mcp\reacoes.sqlite3` na pasta do seu usuário, ao lado do journal (`REACOES_PATH` muda o local). Cada registro que muda algo grava uma cópia em `journal_export\reacoes-copia.sqlite3`, dentro do projeto (vai para o OneDrive): medições com mais de ~3 meses não podem ser refeitas.
- **Estimativas**: em eventos com primeira estimativa e revisões do mesmo período (PIB), `estimativas` conta cada tipo; as estatísticas juntam os dois.

## Estrutura SMC

`estrutura_smc` marca a estrutura do jeito que dá para medir sem opinião; a leitura e a decisão de entrar continuam suas. As regras seguem as implementações mais usadas (indicador "Smart Money Concepts" da LuxAlgo no TradingView e a biblioteca Python `smartmoneyconcepts`), com o que você usa por cima: rompimento por pavio **e** por fechamento, dia virando às 17:00 de Nova York, sessões e topos/fundos diários.

- **Topos e fundos**: pelo pavio, confirmados alguns candles depois (sem repintar): **micro** = 5 candles de cada lado, **macro** = 50 (estrutura interna e externa da LuxAlgo). Só candles fechados entram.
- **BOS/CHoCH**: rompimento do último topo/fundo ainda não rompido. A favor da tendência = BOS; contra = CHoCH (a tendência vira). Cada rompimento mostra quando o pavio passou (`por_pavio`) e quando um candle fechou além (`por_fechamento`); as duas leituras têm a própria tendência.
- **Premium/discount**: onde o preço está na faixa entre o último topo e o último fundo macro, esticada pelos extremos depois deles para acompanhar um rompimento (acima de 52,5% = premium, abaixo de 47,5% = discount).
- **FVG**: três candles com espaço entre a máxima do 1º e a mínima do 3º (ou o contrário), o do meio forte; mostra os abertos mais perto do preço e quanto já foi preenchido.
- **Order block**: no rompimento por fechamento, o candle de mínima mais baixa (ou máxima mais alta) entre o topo/fundo rompido e o rompimento (regra da LuxAlgo); zona de pavio a pavio; sai da lista quando um candle fecha além da zona; `tocado` = o pavio já voltou nela. O mesmo candle no micro e no macro aparece uma vez.
- **Liquidez**: topos/fundos iguais (a menos de 0,1 ATR; três ou mais seguidos viram um grupo) ainda não tomados; varreduras recentes (pavio além e fechamento de volta) dos topos/fundos micro e da liquidez igual, com `rompido_depois_em` quando o preço depois fechou além.
- **Níveis** (`niveis`): máxima e mínima do dia e da semana de mercado atuais (até agora; no fim de semana, a semana atual é a que fechou na sexta) e anteriores (o dia vira às 17:00 de Nova York, pulando fim de semana e feriado; a semana, domingo 17:00); sessões de hoje e do dia anterior em sequência, cada uma até a abertura da seguinte (Ásia: Tóquio 9 h até Londres 8 h; Londres até Nova York 8 h; Nova York até 17 h, no horário local de cada praça); e os topos/fundos diários que nenhum candle passou, nem o de hoje (candle D1 do MT5, que vira às 00:00 UTC; o toco de domingo entra na segunda). Para cada máxima/mínima de um período encerrado, se e quando foi **varrida** (pavio além e fechou de volta) e/ou **rompida** (fechou além), em candles M5 fechados; `incompleto` = o histórico M5 do terminal não chega ao início do período.
- **Alvos** (`alvos`): até 5 acima e 5 abaixo do preço (ou da entrada), do mais perto ao mais longe: liquidez ainda não tomada (máximas/mínimas do dia e da semana anteriores e das sessões encerradas, máxima/mínima de hoje, topos/fundos iguais, topos/fundos diários intactos) e o início das zonas contrárias (OB e FVG de baixa acima, de alta abaixo). Níveis muito próximos viram um alvo só, com todos os tipos. Com `entrada` e `stop`, cada alvo na direção da operação traz o risco/retorno. É onde o preço costuma reagir pela leitura SMC, não previsão; ainda não há taxa de acerto medida.
- **Primeira consulta de um símbolo** pode levar ~15 s (o terminal monta o histórico de timeframes como o M3); depois, menos de 1 s.

## Journal

O journal registra as operações da conta demo para medir o que funciona. **O MT5 é a fonte dos fatos**: cada posição vira uma operação, com entrada e saída pelo preço médio dos negócios executados, volume, comissão, swap, resultado líquido, horários e motivo do fechamento (stop, alvo, manual). Você só acrescenta, pelo chat, o que o MT5 não sabe: setup, tags, motivo da entrada e observações.

- **Stop inicial e R**: o stop inicial vem da ordem que abriu a posição (quando o stop foi definido na boleta). Se você só colocou o stop depois, o journal usa o stop visto com a posição aberta na primeira sincronização (marcado como `observado`, porque pode já ter sido movido), ou o que você informar com `journal_anotar`. Risco inicial = perda até esse stop; R = resultado líquido / risco inicial.
- **Notícia**: a operação é marcada `sim` quando houve evento dos EUA de importância alta entre 30 min antes da entrada e o fechamento, pelo calendário do MT5. O arquivo do calendário cobre só os dias de `InpDaysBack` do serviço (padrão 7): sincronize dentro desse prazo, senão a marcação fica `desconhecido`. Você pode corrigir com `journal_anotar`.
- **Contexto da entrada** (`contexto`): na sincronização, o journal mede sozinho o contexto SMC na hora da primeira entrada, **só com candles que já tinham fechado nela** (nada do que o preço fez depois; nem o candle que estava aberto). Usa as mesmas regras de `estrutura_smc` e compara com a direção da operação:
  - `sessao` (Ásia, Londres, Nova York ou fora);
  - `estrutura_M5`, `_M15`, `_H1`, `_H4` (micro) e `estrutura_macro_H1`: tendência por fechamento a favor ou contra;
  - `varredura_a_favor`: liquidez do lado contrário tomada antes, com o preço de volta na entrada (abaixo numa compra, acima numa venda). Em ordem de peso: `nivel_chave` (máxima/mínima do dia e da semana anteriores e das sessões encerradas, nas 2 h antes), `liquidez_igual` (topos/fundos iguais), `topo_fundo` (topo/fundo micro do M5/M15, 2 h) e `topo_fundo_m1_m3` (M1/M3, 30 min);
  - `choch_a_favor` (o menor timeframe entre M1, M3 e M5 com CHoCH por fechamento a favor nos 30 min antes) e `choch_contra`;
  - `zona_a_favor` (OB não mitigado e/ou FVG aberto a favor contendo o preço de entrada; numa compra, comparado pelo bid estimado, porque os candles são de bid) e `zona_contra`;
  - `premium_discount_M15` e `_H1`: comprar em discount ou vender em premium = a favor.

  `journal_listar` mostra esses rótulos; com `contexto_detalhado`, também os níveis, zonas, CHoCH e varreduras. Cada operação leva ~0,6 s; a sincronização mede enquanto não passa de ~30 s e deixa o resto para a próxima (avisa). Timeframe sem histórico no terminal (o M1 cobre ~3 meses) fica `sem_dados`. Mudando as regras, a versão do contexto sobe e tudo é medido de novo.
- **Ingredientes seus** (gatilho, timeframe de entrada, confluências): anote em `tags` com `journal_anotar`, sempre com os mesmos nomes; as estatísticas agrupam por tag (uma operação com várias tags conta em cada uma).
- **Estatísticas**: só operações fechadas; cada grupo mostra o tamanho da amostra e quantas operações ficaram sem R. Calculadas no servidor, sempre do mesmo jeito. Além de setup, símbolo, notícia e direção, saem `por_tag` e `por_contexto` (um agrupamento para cada rótulo do contexto). Cada rótulo é uma leitura isolada: com poucas operações, a diferença entre grupos não prova nada. Não há métrica de quanto a operação poderia ter ganho.
- **Onde fica**: o banco fica em `trading-mcp\journal.sqlite3` na pasta do seu usuário, fora do OneDrive (sincronizar um banco aberto pode corrompê-lo) e fora do AppData (o Claude Desktop da Microsoft Store redireciona o AppData dos programas que ele abre). `journal_exportar` grava uma cópia do banco e um CSV (separador `;`, vírgula decimal, abre direto no Excel) em `journal_export`, dentro do projeto: como o projeto está no OneDrive, as cópias vão para a nuvem.

## Limites de risco e propostas de operação

As suas regras: **1,25% por operação, 5% de perda no dia e 25% na semana**, sobre o saldo do **início** do dia de mercado (vira às 17:00 de Nova York) e da semana (domingo 17:00 de Nova York). O valor fica fixo durante o período; as porcentagens mudam no `.env`.

- **Base e resultado**: o saldo do início vem do histórico do MT5 (saldo atual menos o que mexeu no saldo desde então). Depósito ou saque dentro do período entra na base, não no resultado: com a conta aberta no meio da semana, a base da semana é o depósito. O resultado inclui as operações (lucro, comissão, swap) e os demais lançamentos (tarifas, juros, dividendos); crédito da corretora fica de fora.
- **Disponível** = limite + resultado do período − perda nos stops das posições abertas (com o swap já acumulado nelas) − perda nos stops das ordens pendentes (se executadas). É o pior caso se tudo bater no stop agora; lucro já protegido por stop não conta como folga. Posição ou ordem **sem stop** deixa o disponível indeterminado e bloqueia novas propostas.
- **Proposta** (`proposta_operacao`): lote para o risco máximo permitido (1,25%, ou menos se o dia ou a semana tiverem menos espaço), arredondado para baixo. O tipo de ordem sai do preço atual: **pendente** (limitada ou stop) executa no próprio preço, e o risco é da entrada ao stop; **a mercado**, a compra executa no ask e a venda no bid de agora, e o risco parte desse preço (precisa de cotação atual). Traz o risco/retorno e o valor no alvo, as notícias de importância alta durante a validade (e até 15 min depois) e o contexto SMC da entrada (a favor/contra, como no journal).
- **Recusa**, com o motivo: dia ou semana no limite, posição ou ordem sem stop, lote mínimo acima do permitido, margem insuficiente, preço de execução já além do stop ou do alvo, sem cotação, conta que não é demo ou terminal sem conexão.
- **Nada é enviado**: proposta não é ordem. Cada uma fica guardada com validade (padrão 30 min) e uma assinatura HMAC dos parâmetros exatos (conta, símbolo, direção, tipo, entrada, stop, alvo, lote, risco, validade, situação), com a chave num arquivo ao lado do banco (`propostas.chave`). A etapa F só poderá executar, na conta demo, uma proposta válida, íntegra, dentro do prazo e aprovada por você fora do chat, conferindo os limites de novo na hora: propostas não reservam risco entre si (avisa quando as válidas somadas passariam do espaço).
- **Fora da conta**: comissão, gap e escorregamento no stop. Risco/retorno é a geometria da operação, não a chance de acerto.

## Exemplos de perguntas

- "Como está o USTEC agora? A cotação é atual?"
- "Tem evento importante dos EUA nos próximos 30 minutos?"
- "O Jobless Claims saiu? Compare realizado, previsão e anterior."
- "Qual é o RSI e o MACD do EURUSD no H4?"
- "Quanto perco se os stops das minhas posições forem atingidos? E quanto falta até cada stop?"
- "Sincroniza o journal e anota a operação de hoje no USTEC: setup OB + FVG, entrei na varredura da mínima de Londres."
- "Como estão minhas estatísticas por setup? E com notícia e sem notícia?"
- "O Claims veio acima da previsão. Como reagiram USTEC, US30, ouro, DXY e as moedas nos primeiros 15 minutos?"
- "Me dá o contexto entre ativos agora: dólar, ouro, índices, petróleo e BTC."
- "Como está a estrutura do ouro no M15, H1 e H4? Londres já varreu a máxima da Ásia?"
- "Se eu comprar o US30 em 51.250 com stop em 51.190, quais são os alvos e o risco/retorno de cada um?"
- "Guarda as reações da semana. Como USTEC e ouro costumam reagir ao Claims acima e abaixo da previsão? Qual o tamanho da amostra?"
- "Quantos lotes devo usar para arriscar 1% com entrada 1.0850 e stop 1.0820 no EURUSD?"
- "Mostre os fundamentos da AAPL: receita, lucro, margem e ROE do último ano."
- "Qual foi a variação percentual do XAUUSD nos últimos 50 candles em D1?"
- "Quais são as posições abertas na minha conta?"
- "Qual é o nome do símbolo da Apple e da Tesla nesta conta?"

## Calendário econômico (serviço MQL5)

A biblioteca Python do MetaTrader 5 não acessa o calendário econômico. Por isso, um pequeno serviço MQL5 (`mql5/Services/TradingMcpCalendar.mq5`) roda dentro do terminal e grava o calendário dos EUA em `MQL5\Files\trading_mcp\calendar_US.json`. A ferramenta `calendario` só lê esse arquivo. O serviço não negocia, então funciona com o Algo Trading desligado e com a negociação via Python desativada.

Instalação (uma vez):

1. Copie `mql5/Services/TradingMcpCalendar.mq5` para a pasta `MQL5\Services` do terminal (no MT5: **Arquivo → Abrir pasta de dados**).
2. Compile: abra no MetaEditor (F4 no terminal) e pressione F7, ou pela linha de comando: `MetaEditor64.exe /compile:"<pasta de dados>\MQL5\Services\TradingMcpCalendar.mq5" /log`.
3. No terminal, **Navegador (Ctrl+N) → Serviços → TradingMcpCalendar → botão direito → Adicionar serviço → OK**. A aba Diário mostra `TradingMcpCalendar: exportando US...`.

O serviço confere mudanças a cada 15 s e regrava tudo a cada 5 min. `InpDaysBack` (padrão 7) define quantos dias para trás vão no arquivo: para guardar reações (`reacoes_registrar`) e para o journal marcar notícias em operações antigas, use 100 (botão direito no serviço → **Propriedades → Parâmetros de entrada**; não precisa recompilar). Ele volta sozinho quando o terminal reabre, se estava rodando ao fechar. Se o arquivo parar de ser atualizado, a ferramenta avisa (`estado = "desatualizado"`).

Como ler os dados:

- **Identifique a medida pelo `codigo`** (em inglês), por `descricao` e por `medida`, nunca só pelo nome. O terminal traduz os nomes e às vezes erra: em 2026-10-01 o CPI cheio mensal aparecia como "Núcleo do Índice de Preços ao Consumidor (IPC) (Mensal)", o mesmo nome do núcleo.
- **Valores**: na unidade do indicador (`unidade`; por exemplo, NFP em "mil empregos": 162 = 162 mil). `surpresa` = realizado − previsão na mesma unidade (p.p. para percentuais).
- **Previsão, não consenso**: o campo é `previsao`, a previsão do calendário do MT5, que nem sempre é o consenso de mercado (pesquisa com analistas). Há previsões com 3 casas decimais, típicas de modelo. Ausente vem como `null`, nunca zero. Em 2026-10-01, o NFP de 2/10 tinha previsão de 52 mil no MT5 e consenso de 89 mil no Forex Factory.
- **Estimativas revisadas**: `estimativa` aparece quando o dado tem várias divulgações para o mesmo período (PIB, Michigan, estoques). Numa estimativa revisada, `anterior` é a estimativa anterior do **mesmo** período, não o período anterior.
- **Feriados** sempre aparecem, com `data` em vez de horário, porque afetam as sessões.
- **Anterior e anterior revisado**: `anterior` é o valor publicado na divulgação anterior; `anterior_revisado` só aparece quando a fonte o revisou.
- **Latência**: `latencia_fonte_s` é o tempo entre o horário do evento e a primeira vez que o serviço viu o realizado. Só aparece se o serviço já estava rodando antes da divulgação.
- **Simultâneos**: `mesmo_horario` lista todos os eventos de cada horário, inclusive os fora do filtro. Use para não atribuir a um único dado um movimento de preço.

## Conectar ao Claude

Nos exemplos abaixo, troque `C:\caminho\para\MCP_Trader` pela pasta onde você clonou o projeto.

### Claude Desktop

Edite `claude_desktop_config.json` (no app: **Configurações → Desenvolvedor → Editar configuração**) e adicione o servidor. Na instalação da Microsoft Store o arquivo fica em `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\`, não em `%APPDATA%\Claude\`. **Edite com o Claude Desktop totalmente fechado** (ícone da bandeja → Sair): com ele aberto, o app regrava o arquivo e descarta a edição.

```json
{
  "mcpServers": {
    "trading": {
      "command": "C:\\caminho\\para\\MCP_Trader\\.venv\\Scripts\\python.exe",
      "args": ["-m", "trading_mcp"]
    }
  }
}
```

Reinicie o Claude Desktop depois de salvar.

### Claude Code

```powershell
claude mcp add trading -- "C:\caminho\para\MCP_Trader\.venv\Scripts\python.exe" -m trading_mcp
claude mcp list
```

### MCP Inspector (testes, requer Node.js)

```powershell
npx @modelcontextprotocol/inspector "C:\caminho\para\MCP_Trader\.venv\Scripts\python.exe" -m trading_mcp
```

## Testes

Os testes usam um MT5 simulado; não precisam do terminal:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Estrutura do projeto

```
src/trading_mcp/
├── server.py          # Servidor MCP e definição das ferramentas
├── config.py          # Leitura do .env e das variáveis de ambiente
├── mt5_client.py      # Cliente MetaTrader 5 (somente leitura, identidade da conta, estado das cotações)
├── tempo.py           # Base de tempo: UTC, exibição em São Paulo e Nova York, fim de candle
├── calendario.py      # Calendário econômico (lê o arquivo do serviço MQL5)
├── posicoes.py        # Relatório de posições e ordens pendentes
├── journal.py         # Journal de operações em SQLite (importado do histórico do MT5)
├── contexto_entrada.py # Contexto SMC na hora da entrada de cada operação (para as estatísticas do journal)
├── reacao.py          # Reação a eventos e contexto entre ativos
├── reacoes.py         # Reações guardadas (SQLite) e estatísticas por tipo de surpresa
├── smc.py             # Estrutura SMC: topos/fundos, BOS/CHoCH, FVG, order blocks, liquidez, sessões
├── indicators.py      # Indicadores técnicos
├── risk.py            # Tamanho de posição e arredondamento de lote
├── limites.py         # Limites de risco do usuário e propostas de operação (sem envio)
└── sec_edgar.py       # Fundamentos da SEC EDGAR

tests/
├── fake_mt5.py        # Simulador do módulo MetaTrader5
└── test_*.py          # Testes

docs/
└── diagnostico-mt5-2026-10-01.md   # Medições no terminal real (fuso, sessões, histórico, símbolos, calendário)

mql5/Services/
└── TradingMcpCalendar.mq5          # Serviço que exporta o calendário econômico do MT5
```

## Notas

- **Símbolos**: digite sem sufixo (EURUSD); o servidor resolve para o nome da conta (ex.: EURUSDm em algumas contas Exness). Use `simbolos` para achar o nome exato de ações.
- **Horários**: em UTC. O servidor da Exness usa UTC (diferença de 0 a 1 s medida no terminal real; veja `docs/`). Cotações e o último candle também vêm em São Paulo e Nova York, com o horário de verão dos EUA.
- **Índices são CFDs**: `USTECm` acompanha o Nasdaq 100, mas não é o índice; o mesmo vale para `US30m`, `US500m` e `DXYm`. A conta não tem instrumento de Treasury de 10 anos.
- **Fundamentos**: apenas empresas dos EUA que entregam 10-K/10-Q. ADRs estrangeiras (20-F) não são suportadas. O 4º trimestre é derivado como "anual menos 9 meses" (sem LPA), porque as empresas não entregam 10-Q do Q4.
- **Não é recomendação de investimento**: os dados servem para estudo e análise.

## Limitações conhecidas

- Validado em 2026-10-01 com uma conta demo Standard da Exness (`docs/diagnostico-mt5-2026-10-01.md`). Troca de conta e perda de conexão com o servidor rodando ainda não foram reproduzidas no terminal real, só com o MT5 simulado.
- O estado da cotação deduz a sessão pela semana anterior: num feriado, uma cotação parada aparece como `atrasado`; se o feriado foi na semana anterior, uma parada real hoje aparece como `mercado_fechado_provavel`. Na semana da mudança do horário de verão, a pausa diária pode ser classificada errada por 1 hora.
- O fuso UTC do servidor precisa ser reconferido depois de 1/11/2026 (fim do horário de verão dos EUA).
- Calendário: depende do terminal aberto com o serviço rodando. Alguns indicadores ficam sem realizado no calendário do MT5 (em 2026-10-01, o PMI industrial da S&P Global continuava sem valor horas depois da divulgação). No payroll de 2/10, o número principal chegou ao arquivo 9 s depois da divulgação; os componentes (salário por hora, payroll privado) vieram depois, sem tempo medido.
- Posições (`posicoes`): conferido no terminal real em 2026-10-02 com uma venda e uma compra limitada (veja `docs/`); uma posição de compra ainda não foi aberta no terminal real. Comissão não aparece (fica nos negócios do histórico).
- Journal: conferido no terminal real em 2026-10-02 com uma operação manual de BTCUSDm. O contexto da entrada foi conferido com as três operações da demo de 2/10 (BTCUSD, JP225 e US30; veja `docs/`). O risco inicial usa a cotação de conversão do momento da sincronização (exato para símbolos cotados em dólar, como USTEC e US30). Comissões cobradas por dia ou por mês, fora das operações, não entram no resultado.
- Para alguns bancos e empresas com duas classes de ações, `caixa` e `acoes_em_circulacao` vêm vazios, com uma observação explicando.

## Roadmap

Revisado em 2026-10-01. Tudo continua somente leitura até a etapa F.

- **A** (feito): base validada no MT5 real (identidade da conta, horários em UTC, estado da cotação).
- **B** (feito): calendário econômico (B2), consulta de posições com distâncias e risco até o stop (B1) e journal em SQLite importado do MT5 (B3).
- **C**: reação observada a eventos e contexto entre ativos para os instrumentos operados (C1) e reações guardadas para estatísticas por tipo de surpresa (C2), ambas feitas. Próximo: notícias, só depois de medir a latência das fontes gratuitas.
- **D**: o SMC do usuário é discricionário (sem setup fixo), então a etapa virou ferramentas de estrutura (D1: `estrutura_smc`, feito) e o contexto SMC de cada operação no journal (D2, feito: sessão, estrutura, CHoCH, varredura, OB/FVG, premium/discount, medidos sozinhos na hora da entrada, mais as tags do usuário) para medir quais combinações funcionam. Backtest só de uma variante que os dados indicarem.
- **E**: painel dos limites do usuário (1,25% / 5% / 25% sobre o saldo do início do dia e da semana de mercado) e propostas de operação guardadas com validade e assinatura, sem envio (feito). A aprovação fora do chat entra com a execução, na F.
- **F**: execução **somente em conta demo**, com verificação de conta imediatamente antes do envio, reconciliação e kill switch.
- **G**: coleta contínua, alertas ou dashboard, só se o uso justificar.
