//+------------------------------------------------------------------+
//| TradingMcpCalendar.mq5 - servico do MCP_Trader                   |
//|                                                                  |
//| Exporta o calendario economico do MetaTrader 5 (um pais) para    |
//| MQL5\Files\trading_mcp\calendar_<PAIS>.json, lido pelo servidor  |
//| MCP em Python (a biblioteca Python do MT5 nao acessa o           |
//| calendario). Somente leitura: nao negocia nem abre graficos.     |
//|                                                                  |
//| Arquivo em ASCII de proposito (sem acentos) para compilar igual  |
//| em qualquer configuracao do MetaEditor.                          |
//+------------------------------------------------------------------+
#property service
#property copyright "MCP_Trader"
#property version   "1.01"
#property description "Exporta o calendario economico do MT5 para o MCP_Trader (somente leitura)."

input string InpCountry        = "US"; // Codigo do pais (ISO 3166-1 alfa-2)
input int    InpPollSeconds    = 15;   // Intervalo para checar mudancas (s)
input int    InpRefreshSeconds = 300;  // Reexporta tudo mesmo sem mudancas (s)
input int    InpDaysBack       = 7;    // Dias para tras
input int    InpDaysAhead      = 14;   // Dias para frente

#define SCHEMA_VERSION 1
#define EXPORT_FOLDER  "trading_mcp"

// Estado por id de valor, para medir a latencia da fonte:
//   0  = visto sem realizado (aguardando divulgacao)
//   1  = ja tinha realizado quando foi visto pela primeira vez (latencia desconhecida)
//   >1 = momento (GMT) em que o realizado apareceu num valor antes vazio
ulong    g_seen_ids[];
datetime g_seen_state[];

//--- JSON -----------------------------------------------------------------
string JsonEscape(const string text)
  {
   string out = text;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\r", "\\r");
   StringReplace(out, "\n", "\\n");
   StringReplace(out, "\t", "\\t");
   return out;
  }

string JStr(const string text)    { return "\"" + JsonEscape(text) + "\""; }
string JLong(const long value)    { return value == LONG_MIN ? "null" : IntegerToString(value); }
string JULong(const ulong value)  { return StringFormat("%I64u", value); }
string JBool(const bool value)    { return value ? "true" : "false"; }

//--- realizado visto pela primeira vez --------------------------------------
// Devolve o momento em que o realizado apareceu, ou 0 se desconhecido. Valores que ja chegam com
// realizado (servico iniciado depois da divulgacao, terminal reconectando) nao ganham latencia falsa.
datetime FirstSeen(const ulong value_id, const bool has_actual)
  {
   int n = ArraySize(g_seen_ids);
   for(int i = 0; i < n; i++)
      if(g_seen_ids[i] == value_id)
        {
         if(g_seen_state[i] == 0 && has_actual)
            g_seen_state[i] = TimeGMT();
         return g_seen_state[i] > 1 ? g_seen_state[i] : 0;
        }
   ArrayResize(g_seen_ids, n + 1);
   ArrayResize(g_seen_state, n + 1);
   g_seen_ids[n] = value_id;
   g_seen_state[n] = has_actual ? (datetime)1 : (datetime)0;
   return 0;
  }

//--- arquivo ----------------------------------------------------------------
string ExportPath() { return EXPORT_FOLDER + "\\calendar_" + InpCountry + ".json"; }

// Escreve num .tmp e troca de uma vez: o Python nunca le um arquivo pela metade.
bool WriteAtomic(const string content, string &error)
  {
   string path = ExportPath();
   string tmp  = path + ".tmp";
   FolderCreate(EXPORT_FOLDER);
   ResetLastError();
   int handle = FileOpen(tmp, FILE_WRITE | FILE_TXT | FILE_ANSI, '\t', CP_UTF8);
   if(handle == INVALID_HANDLE)
     {
      error = "FileOpen falhou: " + IntegerToString(GetLastError());
      return false;
     }
   FileWriteString(handle, content);
   FileClose(handle);
   ResetLastError();
   if(!FileMove(tmp, 0, path, FILE_REWRITE))
     {
      error = "FileMove falhou: " + IntegerToString(GetLastError());
      return false;
     }
   return true;
  }

string Header(const ulong change_id, const datetime started_gmt, const bool ok, const string error)
  {
   datetime server_now = TimeTradeServer();
   datetime gmt_now = TimeGMT();
   string h = "\"schema\":" + IntegerToString(SCHEMA_VERSION)
            + ",\"ok\":" + JBool(ok)
            + ",\"error\":" + (ok ? "null" : JStr(error))
            + ",\"country\":" + JStr(InpCountry)
            + ",\"server\":" + JStr(AccountInfoString(ACCOUNT_SERVER))
            + ",\"terminal_connected\":" + JBool((bool)TerminalInfoInteger(TERMINAL_CONNECTED))
            + ",\"generated_gmt\":" + IntegerToString((long)gmt_now)
            + ",\"generated_server\":" + IntegerToString((long)server_now)
            + ",\"server_gmt_offset_s\":" + IntegerToString((long)server_now - (long)gmt_now)
            + ",\"service_started_gmt\":" + IntegerToString((long)started_gmt)
            + ",\"poll_seconds\":" + IntegerToString(InpPollSeconds)
            + ",\"refresh_seconds\":" + IntegerToString(InpRefreshSeconds)
            + ",\"days_back\":" + IntegerToString(InpDaysBack)
            + ",\"days_ahead\":" + IntegerToString(InpDaysAhead)
            + ",\"change_id\":\"" + JULong(change_id) + "\"";
   return h;
  }

string EventJson(const MqlCalendarEvent &ev)
  {
   return "{\"id\":" + JULong(ev.id)
        + ",\"name\":" + JStr(ev.name)
        + ",\"event_code\":" + JStr(ev.event_code)
        + ",\"type\":" + JStr(EnumToString(ev.type))
        + ",\"sector\":" + JStr(EnumToString(ev.sector))
        + ",\"frequency\":" + JStr(EnumToString(ev.frequency))
        + ",\"time_mode\":" + JStr(EnumToString(ev.time_mode))
        + ",\"unit\":" + JStr(EnumToString(ev.unit))
        + ",\"multiplier\":" + JStr(EnumToString(ev.multiplier))
        + ",\"importance\":" + JStr(EnumToString(ev.importance))
        + ",\"digits\":" + IntegerToString((long)ev.digits)
        + ",\"source_url\":" + JStr(ev.source_url)
        + "}";
  }

string ValueJson(const MqlCalendarValue &v)
  {
   bool has_actual = v.actual_value != LONG_MIN;
   datetime seen = FirstSeen(v.id, has_actual);
   return "{\"id\":" + JULong(v.id)
        + ",\"event_id\":" + JULong(v.event_id)
        + ",\"time\":" + IntegerToString((long)v.time)
        + ",\"period\":" + IntegerToString((long)v.period)
        + ",\"revision\":" + IntegerToString(v.revision)
        + ",\"actual\":" + JLong(v.actual_value)
        + ",\"forecast\":" + JLong(v.forecast_value)
        + ",\"prev\":" + JLong(v.prev_value)
        + ",\"revised_prev\":" + JLong(v.revised_prev_value)
        + ",\"impact\":" + JStr(EnumToString(v.impact_type))
        + ",\"actual_seen_gmt\":" + (seen > 0 ? IntegerToString((long)seen) : "null")
        + "}";
  }

bool Export(const ulong change_id, const datetime started_gmt, string &error)
  {
   datetime server_now = TimeTradeServer();
   datetime from = server_now - InpDaysBack * 86400;
   datetime to   = server_now + InpDaysAhead * 86400;
   MqlCalendarValue values[];
   ResetLastError();
   int n = CalendarValueHistory(values, from, to, InpCountry);
   if(n < 0)
     {
      error = "CalendarValueHistory falhou: " + IntegerToString(GetLastError());
      return false;
     }

   string values_json = "";
   string events_json = "";
   ulong  event_ids[];
   ulong  missing_ids[];
   for(int i = 0; i < n; i++)
     {
      values_json += (i > 0 ? "," : "") + ValueJson(values[i]);
      bool known = false;
      for(int k = 0; k < ArraySize(event_ids) && !known; k++)
         known = event_ids[k] == values[i].event_id;
      for(int k = 0; k < ArraySize(missing_ids) && !known; k++)
         known = missing_ids[k] == values[i].event_id;
      if(known)
         continue;
      MqlCalendarEvent ev;
      if(!CalendarEventById(values[i].event_id, ev))
        {
         // Sem a descricao o Python ignora o valor; a contagem vai no cabecalho para ele avisar.
         int q = ArraySize(missing_ids);
         ArrayResize(missing_ids, q + 1);
         missing_ids[q] = values[i].event_id;
         continue;
        }
      int m = ArraySize(event_ids);
      ArrayResize(event_ids, m + 1);
      event_ids[m] = values[i].event_id;
      events_json += (m > 0 ? "," : "") + EventJson(ev);
     }

   string content = "{" + Header(change_id, started_gmt, true, "")
                  + ",\"values_count\":" + IntegerToString(n)
                  + ",\"missing_events\":" + IntegerToString(ArraySize(missing_ids))
                  + ",\"events\":[" + events_json + "]"
                  + ",\"values\":[" + values_json + "]}";
   return WriteAtomic(content, error);
  }

void ExportError(const ulong change_id, const datetime started_gmt, const string error)
  {
   string ignored = "";
   WriteAtomic("{" + Header(change_id, started_gmt, false, error) + ",\"events\":[],\"values\":[]}", ignored);
  }

//--- laco principal -----------------------------------------------------------
void OnStart()
  {
   datetime started = TimeGMT();
   ulong change_id = 0;
   MqlCalendarValue ignored[];
   CalendarValueLast(change_id, ignored, InpCountry); // primeira chamada so registra o estado atual
   datetime last_export = 0;
   bool pending = false;  // mudanca detectada que ainda nao foi exportada com sucesso
   int failures = 0;
   PrintFormat("TradingMcpCalendar: exportando %s para MQL5\\Files\\%s", InpCountry, ExportPath());

   while(!IsStopped())
     {
      MqlCalendarValue changed[];
      if(CalendarValueLast(change_id, changed, InpCountry) > 0)
         pending = true;
      bool due = last_export == 0 || pending || (TimeGMT() - last_export) >= InpRefreshSeconds;
      if(due)
        {
         string error = "";
         if(Export(change_id, started, error))
           {
            last_export = TimeGMT();
            pending = false;
            failures = 0;
           }
         else
           {
            failures++;
            Print("TradingMcpCalendar: ", error);
            // Falha passageira (ex.: 5401, tempo esgotado): mantem o ultimo arquivo bom e tenta de novo
            // no proximo ciclo. O erro so e publicado se nunca exportou ou se a falha persiste.
            if(last_export == 0 || failures >= 3)
               ExportError(change_id, started, error);
           }
        }
      // Dorme em passos de 1 s para parar rapido quando o terminal pedir.
      for(int s = 0; s < InpPollSeconds && !IsStopped(); s++)
         Sleep(1000);
     }
  }
//+------------------------------------------------------------------+
