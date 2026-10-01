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
| `MT5_LOGIN` | Número da conta (login automático) | Não |
| `MT5_PASSWORD` | Senha da conta | Não |
| `MT5_SERVER` | Nome exato do servidor demo (ex.: `Exness-MT5Trial...`) | Não |
| `SYMBOL_SUFFIX` | Sufixo dos símbolos, se a detecção automática falhar (ex.: `m`) | Não |
| `SEC_USER_AGENT` | Identificação exigida pela SEC: `"Seu Nome seu@email.com"` | **Sim** (para `fundamentos`) |
| `MT5_TIMEOUT_MS` | Timeout de conexão com o MT5, em milissegundos | Não (padrão: 60000) |
| `MAX_BARS` | Limite de candles buscados por chamada | Não (padrão: 5000) |

O `.env` é lido da pasta raiz do projeto, mesmo que o servidor seja iniciado de outro lugar (`TRADING_MCP_ENV_FILE` aponta para outro arquivo, se preferir). Arquivos salvos pelo Bloco de Notas (com BOM ou UTF-16) funcionam. O `.env` está no `.gitignore` e nunca deve ir para o git.

### Segurança

Use uma instalação **separada** do MT5, logada **só** na conta demo, e aponte `MT5_PATH` para o `terminal64.exe` dela. O terminal precisa estar aberto e logado para as ferramentas funcionarem.

- Se `MT5_LOGIN` estiver preenchido, `MT5_PATH` passa a ser obrigatório. Sem ele, o login seria feito no seu terminal principal e trocaria a conta logada nele. O servidor recusa essa combinação.
- Depois de conectar, o servidor confere se a conta logada é a mesma de `MT5_LOGIN`.
- O acesso à biblioteca MetaTrader5 passa por uma lista de funções permitidas, todas de leitura. Funções como `order_send` são bloqueadas no código.
- A ferramenta `info_conta` avisa se a conta conectada não for demo.

## Ferramentas

| Nome | Descrição | Parâmetros principais |
|------|-----------|----------------------|
| `cotacao` | Bid, ask e spread atual | `simbolo` (ex.: EURUSD) |
| `historico` | Candles OHLC em CSV, com resumo do período | `simbolo`, `timeframe` (M1–MN1, padrão H1), `quantidade` (1–500, padrão 100), `incluir_candle_atual` (padrão: sim) |
| `indicadores` | RSI, MACD, EMA, SMA, ATR, Bollinger | `simbolo`, `lista` (padrão: RSI(14), MACD(12,26,9), EMA 20/50, ATR(14)), `timeframe`, `incluir_candle_atual` |
| `tamanho_posicao` | Lote para arriscar X% do saldo | `simbolo`, `entrada`, `stop`, `risco_percentual` (padrão 1%), `saldo` (padrão: saldo da conta) |
| `info_conta` | Saldo, margem e posições abertas | — |
| `simbolos` | Busca símbolos disponíveis na conta | `busca` (ex.: USD, Apple), `limite` (1–200, padrão 30) |
| `fundamentos` | Fundamentos da SEC EDGAR (receita, lucro, LPA, ROE, margem) | `ticker` (ex.: AAPL, MSFT, BRK.B) |

**Indicadores**: seguem as convenções do TradingView (EMA, RSI e ATR com semente SMA, suavização de Wilder, Bollinger com desvio padrão populacional). Com o candle atual incluído, os valores mudam até ele fechar. O ATR e o MACD nativos do MT5 usam médias simples, então podem diferir levemente do gráfico do MT5.

**Tamanho de posição**: a direção é deduzida (stop abaixo da entrada = compra). O resultado traz o risco com o lote arredondado, o efeito do spread atual nas compras (`risco_com_spread`) e a margem estimada. Ele recusa stop menor que o tick do símbolo e avisa quando o spread consome boa parte do stop ou quando a margem passa da margem livre. Comissão e swap não estão incluídos: em contas Raw Spread/Zero, some a comissão por lote.

## Exemplos de perguntas

- "Qual é o RSI e o MACD do EURUSD no H4?"
- "Quantos lotes devo usar para arriscar 1% com entrada 1.0850 e stop 1.0820 no EURUSD?"
- "Mostre os fundamentos da AAPL: receita, lucro, margem e ROE do último ano."
- "Qual foi a variação percentual do XAUUSD nos últimos 50 candles em D1?"
- "Quais são as posições abertas na minha conta?"
- "Qual é o nome do símbolo da Apple e da Tesla nesta conta?"

## Conectar ao Claude

Nos exemplos abaixo, troque `C:\caminho\para\MCP_Trader` pela pasta onde você clonou o projeto.

### Claude Desktop

Edite `%APPDATA%\Claude\claude_desktop_config.json` e adicione:

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
├── mt5_client.py      # Cliente MetaTrader 5 (somente leitura, com lista de funções permitidas)
├── indicators.py      # Indicadores técnicos
├── risk.py            # Tamanho de posição e arredondamento de lote
└── sec_edgar.py       # Fundamentos da SEC EDGAR

tests/
├── fake_mt5.py        # Simulador do módulo MetaTrader5
└── test_*.py          # Testes
```

## Notas

- **Símbolos**: digite sem sufixo (EURUSD); o servidor resolve para o nome da conta (ex.: EURUSDm em algumas contas Exness). Use `simbolos` para achar o nome exato de ações.
- **Horários**: candles e cotações estão no horário do servidor da corretora, não no horário local.
- **Fundamentos**: apenas empresas dos EUA que entregam 10-K/10-Q. ADRs estrangeiras (20-F) não são suportadas. O 4º trimestre é derivado como "anual menos 9 meses" (sem LPA), porque as empresas não entregam 10-Q do Q4.
- **Não é recomendação de investimento**: os dados servem para estudo e análise.

## Limitações conhecidas

- O comportamento com o terminal MT5 real ainda não foi testado: a fase 1 foi validada com um MT5 simulado e com a documentação oficial da biblioteca.
- O aviso de mercado fechado no fim de semana usa 21:00 UTC fixo e pode errar em 1 hora durante o horário padrão dos EUA.
- Para alguns bancos e empresas com duas classes de ações, `caixa` e `acoes_em_circulacao` vêm vazios, com uma observação explicando.

## Roadmap

- **Fase 1** (atual): dados de mercado, indicadores, tamanho de posição e fundamentos, somente leitura.
- **Fase 2**: backtest de estratégias. Candidata a ganhar o primeiro componente visual: um gráfico da curva de resultado exibido dentro do chat do Claude (MCP Apps).
- **Fase 3**: carteira (lucro/prejuízo, preço médio).
- **Fase 4**: execução de ordens **somente em conta demo**, com trava no código (recusa se a conta não for demo) e limites de lote.
- **Opcional**: dashboard web próprio (por exemplo, com Streamlit) reaproveitando os mesmos módulos, para usar sem o Claude.
