# MCP_Trader

Servidor MCP (stdio) de análise de mercado para forex e ações dos EUA. Dá ao Claude acesso a cotações, histórico e indicadores técnicos via MetaTrader 5 (Exness), a um calculador de tamanho de posição que usa a especificação real do contrato na corretora, e a fundamentos de empresas americanas via SEC EDGAR.

**Fase 1 (somente leitura)**: as ferramentas consultam dados. Não existe tool para enviar, alterar ou cancelar ordens, e o acesso a essas funções do MetaTrader 5 é bloqueado no código.

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
| `historico` | Candles OHLC em CSV, com resumo do período | `simbolo`, `timeframe` (M1–MN1, padrão H1), `quantidade` (1–500, padrão 100), `incluir_candle_atual` (padrão: sim) |
| `indicadores` | RSI, MACD, EMA, SMA, ATR, Bollinger | `simbolo`, `lista` (padrão: RSI(14), MACD(12,26,9), EMA 20/50, ATR(14)), `timeframe`, `incluir_candle_atual` |
| `tamanho_posicao` | Lote para arriscar X% do saldo | `simbolo`, `entrada`, `stop`, `risco_percentual` (padrão 1%), `saldo` (padrão: saldo da conta) |
| `info_conta` | Saldo, margem, posições abertas e travas de negociação do terminal | — |
| `posicoes` | Posições abertas e ordens pendentes: estado da cotação, distância até stop e alvo, resultado se forem atingidos, duração e exposição por símbolo | `simbolo` (vazio = todos), `incluir_pendentes` (padrão: sim) |
| `simbolos` | Busca símbolos disponíveis na conta | `busca` (ex.: USD, Apple), `limite` (1–200, padrão 30) |
| `calendario` | Calendário econômico dos EUA: horário (UTC/SP/NY), importância, realizado, previsão, anterior, anterior revisado e surpresa | `horas_a_frente` (padrão 24), `horas_atras` (padrão 2), `importancia_minima`, `busca` (ex.: CPI, NFP, claims, FOMC) |
| `fundamentos` | Fundamentos da SEC EDGAR (receita, lucro, LPA, ROE, margem) | `ticker` (ex.: AAPL, MSFT, BRK.B) |

**Estado da cotação**: `atual` (tick com até 60 s), `mercado_fechado_provavel` (sem ticks recentes e sem negociação, na semana anterior, no mesmo intervalo que hoje está sem ticks: pausa diária, fim de semana, fora da sessão), `atrasado` (sem ticks recentes, mas na semana anterior houve negociação nesse intervalo: feriado, atraso ou problema de conexão), `antigo` (não foi possível verificar, ou o último tick tem mais de uma semana), `desconectado` (terminal sem conexão com a corretora) e `horario_inconsistente` (tick à frente do relógio UTC: servidor fora de UTC ou relógio do Windows errado). Só `atual` deve ser tratado como preço de agora.

**Candle em formação**: `historico` e `indicadores` informam `em_formacao` pelo horário, não pela posição. Com `incluir_candle_atual = não`, só sai o candle que de fato está aberto; na pausa diária ou no fim de semana o último candle já fechou e é mantido.

**Indicadores**: seguem as convenções do TradingView (EMA, RSI e ATR com semente SMA, suavização de Wilder, Bollinger com desvio padrão populacional). Com o candle atual incluído, os valores mudam até ele fechar. O ATR e o MACD nativos do MT5 usam médias simples, então podem diferir levemente do gráfico do MT5.

**Posições**: para cada posição, `stop.resultado_se_atingido` vai do preço de entrada até o stop atual (negativo = perda; positivo = lucro protegido) e `variacao_desde_agora` vai do preço atual até ele; o mesmo para o alvo. A `situacao` do stop é `com_risco`, `no_preco_de_entrada`, `lucro_protegido` ou `sem_stop`. Distância positiva = nível ainda não atingido; `ultrapassado` marca um stop ou alvo que o preço já passou (gap ou cotação parada), e aí a variação desde agora fica nula. `pontos` são pontos do MT5 (no USTECm, 0,01): para índices, a distância em pontos do índice é o campo `preco`. Os totais somam posição por posição, sem compensar posições opostas (a conta é hedging): `perda_nos_stops` (valor positivo) só soma stops com risco e não inclui posições sem stop; se faltar algum valor, os totais saem nulos em vez de parciais. Ordens pendentes trazem o resultado se forem executadas e o stop for atingido (na stop limitada, a entrada é o preço da limitada). São medições sobre o stop **atual**: o risco inicial e o resultado em R ficam para o journal (B3). Comissão não incluída; swap à parte.

**Tamanho de posição**: a direção é deduzida (stop abaixo da entrada = compra). O resultado traz o risco com o lote arredondado, o efeito do spread atual nas compras (`risco_com_spread`) e a margem estimada. Ele recusa stop menor que o tick do símbolo e avisa quando o spread consome boa parte do stop ou quando a margem passa da margem livre. Comissão e swap não estão incluídos: em contas Raw Spread/Zero, some a comissão por lote.

## Exemplos de perguntas

- "Como está o USTEC agora? A cotação é atual?"
- "Tem evento importante dos EUA nos próximos 30 minutos?"
- "O Jobless Claims saiu? Compare realizado, previsão e anterior."
- "Qual é o RSI e o MACD do EURUSD no H4?"
- "Quanto perco se os stops das minhas posições forem atingidos? E quanto falta até cada stop?"
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

O serviço confere mudanças a cada 15 s e regrava tudo a cada 5 min. Ele volta sozinho quando o terminal reabre, se estava rodando ao fechar. Se o arquivo parar de ser atualizado, a ferramenta avisa (`estado = "desatualizado"`).

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
├── indicators.py      # Indicadores técnicos
├── risk.py            # Tamanho de posição e arredondamento de lote
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
- Calendário: depende do terminal aberto com o serviço rodando. Alguns indicadores ficam sem realizado no calendário do MT5 (em 2026-10-01, o PMI industrial da S&P Global continuava sem valor horas depois da divulgação). A latência da fonte ainda não foi medida numa divulgação real.
- Posições (`posicoes`): conferido no terminal real em 2026-10-02 com uma venda e uma compra limitada (veja `docs/`); uma posição de compra ainda não foi aberta no terminal real. Comissão não aparece (fica nos negócios do histórico).
- Para alguns bancos e empresas com duas classes de ações, `caixa` e `acoes_em_circulacao` vêm vazios, com uma observação explicando.

## Roadmap

Revisado em 2026-10-01. Tudo continua somente leitura até a etapa F.

- **A** (feito): base validada no MT5 real (identidade da conta, horários em UTC, estado da cotação).
- **B**: calendário econômico (feito: B2), consulta de posições com distâncias e risco até o stop (feito: B1) e journal manual em SQLite (B3).
- **C**: reação observada a eventos (janelas de 1/5/15 min alinhadas em UTC) e contexto entre ativos; notícias só depois de medir a latência das fontes gratuitas.
- **D**: backtest de um único setup definido por regras objetivas.
- **E**: propostas de operação com limites rígidos e aprovação humana fora do chat, ainda sem envio.
- **F**: execução **somente em conta demo**, com verificação de conta imediatamente antes do envio, reconciliação e kill switch.
- **G**: coleta contínua, alertas ou dashboard, só se o uso justificar.
