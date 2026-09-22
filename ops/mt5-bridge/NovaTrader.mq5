//+------------------------------------------------------------------+
//| NovaTrader.mq5                                                   |
//| File-driven trade executor for the Nova MT5-on-Wine pipeline.    |
//|                                                                  |
//| DEMO ONLY: OnInit refuses to run unless the account server name  |
//| contains "demo" (case-insensitive).                              |
//|                                                                  |
//| INPUT   MQL5/Files/nova_commands.jsonl  (one JSON object/line)   |
//|   {"type":"trade.open","id":"cmd-1","symbol":"EURUSD",            |
//|    "direction":"BUY","volume":0.10,"sl":1.0850,"tp":1.0950,       |
//|    "signal_id":"..."}                                            |
//|   {"type":"trade.close","id":"cmd-2","position_id":123456}       |
//| OUTPUT  MQL5/Files/nova_trades.jsonl  (append only)              |
//|   trade.opened (ticket, deal, fill_price, time, signal_id, symbol, |
//|     direction, volume) / trade.closed (ticket, exit_price,        |
//|     profit, reason, time, symbol, direction, volume) /            |
//|     trade.rejected                                                |
//| OUTPUT  MQL5/Files/nova_symbol_specs.json (full rewrite every    |
//|   InSpecsSec seconds)                                            |
//| CURSOR  MQL5/Files/nova_trader.cursor (persisted byte offset)     |
//|                                                                  |
//| The EA never throws on bad input: every validation failure is a  |
//| Print plus a trade.rejected line. All work happens in OnTimer;   |
//| OnTick is intentionally empty.                                   |
//+------------------------------------------------------------------+
#property copyright "Nova Works"
#property version   "1.00"
#property strict

input long   InMagic           = 20260921;
input int    InSlippage        = 10;      // max deviation, points
input int    InMaxSpreadPoints = 1000000; // owner order 2026-09-22:
   // spread filter disabled ("nothing should be blocking you from
   // trading"); input kept so the cap can be restored without recompile
input int    InTimerSec        = 2;       // command poll interval
input int    InSpecsSec        = 60;      // specs refresh interval
input string InSymbols         = "EURUSD,GBPUSD,USDJPY,USDCHF,AUDUSD,USDCAD,NZDUSD,XAUUSD,XAGUSD,US30,US500";

// Symbol list source: MQL5/Files/nova_symbols.txt (first non-empty line,
// comma-separated). Falls back to InSymbols if the file is absent.
// Symbols the broker does not carry are skipped gracefully.
#define MAX_SYM 128

string g_symbols[MAX_SYM];
int    g_nsym      = 0;
long   g_lastSpecs = 0;

// Master symbol list from nova_symbols.txt (pre-select). After a terminal
// restart the broker's Market Watch may not be synced yet, so SymbolSelect
// can fail transiently at OnInit; EnsureSymbols() retries the missing ones
// on every specs cycle so the specs file heals without a re-attach.
string g_master[MAX_SYM];
int    g_nmaster   = 0;

//+------------------------------------------------------------------+
//| Minimal JSON helpers (our files are one flat object per line)    |
//+------------------------------------------------------------------+
string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return(s);
}

// Extract a "key":"value" string field. Returns false if absent.
bool GetJsonString(const string line, const string key, string &val)
{
   string pat = "\"" + key + "\"";
   int p = StringFind(line, pat);
   if(p < 0) return(false);
   p = StringFind(line, ":", p + StringLen(pat));
   if(p < 0) return(false);
   p++;
   int n = StringLen(line);
   while(p < n)
   {
      ushort c = StringGetCharacter(line, p);
      if(c != ' ' && c != '\t') break;
      p++;
   }
   if(p >= n || StringGetCharacter(line, p) != '"') return(false);
   p++;
   int q = p;
   while(q < n)
   {
      ushort c = StringGetCharacter(line, q);
      if(c == '\\') { q += 2; continue; }
      if(c == '"') break;
      q++;
   }
   if(q > n) return(false);
   val = StringSubstr(line, p, q - p);
   return(true);
}

// Extract a "key":number field. Returns false if absent/malformed.
bool GetJsonNumber(const string line, const string key, double &val)
{
   string pat = "\"" + key + "\"";
   int p = StringFind(line, pat);
   if(p < 0) return(false);
   p = StringFind(line, ":", p + StringLen(pat));
   if(p < 0) return(false);
   p++;
   int n = StringLen(line);
   while(p < n)
   {
      ushort c = StringGetCharacter(line, p);
      if(c != ' ' && c != '\t') break;
      p++;
   }
   int q = p;
   while(q < n)
   {
      ushort c = StringGetCharacter(line, q);
      if((c >= '0' && c <= '9') || c == '.' || c == '-' || c == '+' || c == 'e' || c == 'E')
         q++;
      else
         break;
   }
   if(q == p) return(false);
   val = StringToDouble(StringSubstr(line, p, q - p));
   return(true);
}

//+------------------------------------------------------------------+
//| Symbol list                                                      |
//+------------------------------------------------------------------+
int LoadSymbolList(string &out[])
{
   int h = FileOpen("nova_symbols.txt", FILE_READ|FILE_TXT|FILE_ANSI);
   string src = "";
   if(h != INVALID_HANDLE)
   {
      while(!FileIsEnding(h))
      {
         string line = FileReadString(h);
         StringTrimLeft(line);
         StringTrimRight(line);
         if(StringLen(line) > 0) { src = line; break; }
      }
      FileClose(h);
      Print("NovaTrader: symbol list loaded from nova_symbols.txt");
   }
   if(StringLen(src) == 0)
   {
      src = InSymbols;
      Print("NovaTrader: nova_symbols.txt not found, using InSymbols input");
   }
   return(StringSplit(src, ',', out));
}

//+------------------------------------------------------------------+
//| Cursor persistence                                               |
//+------------------------------------------------------------------+
bool CursorExists()
{
   return(FileIsExist("nova_trader.cursor"));
}

long LoadCursor()
{
   long off = 0;
   int h = FileOpen("nova_trader.cursor", FILE_READ|FILE_TXT|FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      string s = FileReadString(h);
      FileClose(h);
      StringTrimLeft(s);
      StringTrimRight(s);
      off = StringToInteger(s);
      if(off < 0) off = 0;
   }
   return(off);
}

void SaveCursor(long off)
{
   int h = FileOpen("nova_trader.cursor", FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, IntegerToString(off));
      FileClose(h);
   }
   else
      Print("NovaTrader: failed to write nova_trader.cursor, err=", GetLastError());
}

//+------------------------------------------------------------------+
//| Output helpers                                                   |
//+------------------------------------------------------------------+
void AppendTradeLine(const string json)
{
   int h = FileOpen("nova_trades.jsonl", FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileSeek(h, 0, SEEK_END);
      FileWriteString(h, json + "\n");
      FileClose(h);
   }
   else
      Print("NovaTrader: failed to open nova_trades.jsonl, err=", GetLastError());
}

void EmitRejected(const string commandId, const string reason)
{
   string js = StringFormat("{\"type\":\"trade.rejected\",\"command_id\":\"%s\",\"reason\":\"%s\"}",
                            JsonEscape(commandId), JsonEscape(reason));
   AppendTradeLine(js);
   Print("NovaTrader: rejected cmd=", commandId, " reason=", reason);
}

//+------------------------------------------------------------------+
//| Filling mode helper                                              |
//+------------------------------------------------------------------+
void SetFilling(MqlTradeRequest &req, const string symbol)
{
   long fm = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((fm & SYMBOL_FILLING_IOC) != 0)
      req.type_filling = ORDER_FILLING_IOC;
   else if((fm & SYMBOL_FILLING_FOK) != 0)
      req.type_filling = ORDER_FILLING_FOK;
   else
      req.type_filling = ORDER_FILLING_RETURN;
}

int VolDigits(double step)
{
   int d = 0;
   double s = step;
   while(d < 8 && s < 1.0 - 1e-9) { s *= 10.0; d++; }
   return(d);
}

//+------------------------------------------------------------------+
//| trade.open                                                       |
//+------------------------------------------------------------------+
void HandleTradeOpen(const string line)
{
   string commandId = "unknown";
   GetJsonString(line, "id", commandId);

   string symbol = "", direction = "", signalId = "";
   double volume = 0, sl = 0, tp = 0;
   GetJsonString(line, "signal_id", signalId);

   if(!GetJsonString(line, "symbol", symbol) || StringLen(symbol) == 0)
   { EmitRejected(commandId, "bad_symbol"); return; }
   if(!GetJsonString(line, "direction", direction))
   { EmitRejected(commandId, "bad_direction"); return; }
   StringToUpper(direction);
   if(direction != "BUY" && direction != "SELL")
   { EmitRejected(commandId, "bad_direction"); return; }
   if(!GetJsonNumber(line, "volume", volume) || volume <= 0)
   { EmitRejected(commandId, "bad_volume"); return; }
   if(!GetJsonNumber(line, "sl", sl) || sl <= 0 ||
      !GetJsonNumber(line, "tp", tp) || tp <= 0)
   { EmitRejected(commandId, "bad_sl_tp"); return; }

   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ||
      !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   { EmitRejected(commandId, "trading_disabled"); return; }
   if(!SymbolSelect(symbol, true))
   { EmitRejected(commandId, "unknown_symbol"); return; }
   if(SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
   { EmitRejected(commandId, "symbol_not_tradeable"); return; }

   int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point  = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(point <= 0) { EmitRejected(commandId, "no_price"); return; }

   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick)) { EmitRejected(commandId, "no_tick"); return; }
   double price = (direction == "BUY") ? tick.ask : tick.bid;
   if(price <= 0) { EmitRejected(commandId, "no_price"); return; }

   // SL/TP must be on the correct side of current price
   if(direction == "BUY")
   {
      if(!(sl < price && price < tp)) { EmitRejected(commandId, "invalid_sl_tp"); return; }
   }
   else
   {
      if(!(tp < price && price < sl)) { EmitRejected(commandId, "invalid_sl_tp"); return; }
   }

   // Spread filter
   double spreadPts = (tick.ask - tick.bid) / point;
   if(spreadPts > InMaxSpreadPoints) { EmitRejected(commandId, "spread_too_wide"); return; }

   // Volume: clamp to [min,max], round DOWN to step
   double vmin  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(vstep <= 0) vstep = 0.01;
   if(vmin  <= 0) { EmitRejected(commandId, "bad_volume_limits"); return; }
   int    vd  = VolDigits(vstep);
   double vol = NormalizeDouble(MathFloor(volume / vstep) * vstep, vd);
   if(vol > vmax) vol = NormalizeDouble(vmax, vd);
   if(vol < vmin) { EmitRejected(commandId, "volume_below_min"); return; }

   // Stops level: SL and TP must be at least stops_level away from price
   long   stopsPts = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   double minDist  = stopsPts * point;
   if(MathAbs(price - sl) < minDist || MathAbs(tp - price) < minDist)
   { EmitRejected(commandId, "stops_too_close"); return; }

   // Send
   MqlTradeRequest request;
   MqlTradeResult  result;
   ZeroMemory(request);
   ZeroMemory(result);
   request.action     = TRADE_ACTION_DEAL;
   request.symbol     = symbol;
   request.volume     = vol;
   request.type       = (direction == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   request.price      = price;
   request.sl         = sl;
   request.tp         = tp;
   request.deviation  = InSlippage;
   request.magic      = InMagic;
   request.comment    = "NovaTrader";
   request.type_time  = ORDER_TIME_GTC;
   SetFilling(request, symbol);

   bool sent = OrderSend(request, result);
   if(sent && (result.retcode == TRADE_RETCODE_REQUOTE ||
               result.retcode == TRADE_RETCODE_PRICE_OFF))
   {
      // Refresh rates and retry once
      if(SymbolInfoTick(symbol, tick))
      {
         request.price = (direction == "BUY") ? tick.ask : tick.bid;
         ZeroMemory(result);
         sent = OrderSend(request, result);
      }
   }
   if(!sent)
   { EmitRejected(commandId, "send_failed:" + IntegerToString((long)GetLastError())); return; }
   if(result.retcode != TRADE_RETCODE_DONE)
   { EmitRejected(commandId, "send_failed:" + IntegerToString((long)result.retcode)); return; }

   string js = StringFormat(
      "{\"type\":\"trade.opened\",\"command_id\":\"%s\",\"ticket\":%I64d,\"deal\":%I64d,"
      "\"fill_price\":%s,\"time\":\"%s\",\"signal_id\":\"%s\","
      "\"symbol\":\"%s\",\"direction\":\"%s\",\"volume\":%s}",
      JsonEscape(commandId), (long)result.order, (long)result.deal,
      DoubleToString(result.price, digits),
      TimeToString(TimeTradeServer(), TIME_DATE|TIME_SECONDS),
      JsonEscape(signalId),
      JsonEscape(symbol), direction, DoubleToString(vol, vd));
   AppendTradeLine(js);
   Print("NovaTrader: opened ", symbol, " ", direction, " ",
         DoubleToString(vol, vd), " ticket=", (long)result.order,
         " deal=", (long)result.deal, " @ ", DoubleToString(result.price, digits));
}

//+------------------------------------------------------------------+
//| trade.close                                                      |
//+------------------------------------------------------------------+
void HandleTradeClose(const string line)
{
   string commandId = "unknown";
   GetJsonString(line, "id", commandId);

   double pidD = 0;
   long ticket = 0;
   if(GetJsonNumber(line, "position_id", pidD)) ticket = (long)pidD;
   if(ticket <= 0) { EmitRejected(commandId, "bad_position_id"); return; }

   // Find the position among open positions
   bool   found = false;
   string psym = "";
   double pvol = 0, pprofit = 0;
   int    ptype = -1;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong t = PositionGetTicket(i);
      if((long)t == ticket)
      {
         found   = true;
         psym    = PositionGetString(POSITION_SYMBOL);
         pvol    = PositionGetDouble(POSITION_VOLUME);
         ptype   = (int)PositionGetInteger(POSITION_TYPE);
         pprofit = PositionGetDouble(POSITION_PROFIT); // fallback profit
         break;
      }
   }
   if(!found) { EmitRejected(commandId, "position_not_found"); return; }
   if(pvol <= 0) { EmitRejected(commandId, "bad_position_volume"); return; }

   int digits = (int)SymbolInfoInteger(psym, SYMBOL_DIGITS);
   MqlTick tick;
   if(!SymbolInfoTick(psym, tick)) { EmitRejected(commandId, "no_tick"); return; }

   bool isBuy = (ptype == POSITION_TYPE_BUY);

   MqlTradeRequest request;
   MqlTradeResult  result;
   ZeroMemory(request);
   ZeroMemory(result);
   request.action    = TRADE_ACTION_DEAL;
   request.position  = (ulong)ticket;
   request.symbol    = psym;
   request.volume    = pvol;
   request.type      = isBuy ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   request.price     = isBuy ? tick.bid : tick.ask;
   request.deviation = InSlippage;
   request.magic     = InMagic;
   request.comment   = "NovaTrader close";
   request.type_time = ORDER_TIME_GTC;
   SetFilling(request, psym);

   bool sent = OrderSend(request, result);
   if(!sent)
   { EmitRejected(commandId, "send_failed:" + IntegerToString((long)GetLastError())); return; }
   if(result.retcode != TRADE_RETCODE_DONE)
   { EmitRejected(commandId, "send_failed:" + IntegerToString((long)result.retcode)); return; }

   // Profit: prefer the closing deal's recorded profit, fall back to the
   // position profit captured before the close.
   double profit = pprofit;
   ulong deal = result.deal;
   if(deal > 0 && HistoryDealSelect(deal))
      profit = HistoryDealGetDouble(deal, DEAL_PROFIT);

   string js = StringFormat(
      "{\"type\":\"trade.closed\",\"ticket\":%I64d,\"exit_price\":%s,"
      "\"profit\":%.2f,\"reason\":\"command\",\"time\":\"%s\","
      "\"symbol\":\"%s\",\"direction\":\"%s\",\"volume\":%s}",
      ticket, DoubleToString(result.price, digits), profit,
      TimeToString(TimeTradeServer(), TIME_DATE|TIME_SECONDS),
      JsonEscape(psym), isBuy ? "BUY" : "SELL",
      DoubleToString(pvol, 8));
   AppendTradeLine(js);
   Print("NovaTrader: closed ticket=", ticket, " @ ",
         DoubleToString(result.price, digits), " profit=", DoubleToString(profit, 2));
}

//+------------------------------------------------------------------+
//| Command dispatch                                                 |
//+------------------------------------------------------------------+
void HandleCommandLine(string line)
{
   StringTrimLeft(line);
   StringTrimRight(line);
   if(StringLen(line) == 0) return;

   int n = StringLen(line);
   if(StringGetCharacter(line, 0) != '{' || StringGetCharacter(line, n - 1) != '}')
   {
      Print("NovaTrader: skipping malformed line: ", line);
      return;
   }
   string type = "";
   if(!GetJsonString(line, "type", type))
   {
      Print("NovaTrader: skipping line without type: ", line);
      return;
   }
   if(type == "trade.open")
      HandleTradeOpen(line);
   else if(type == "trade.close")
      HandleTradeClose(line);
   // anything else: ignored silently
}

//+------------------------------------------------------------------+
//| Command file processing with persisted cursor                    |
//+------------------------------------------------------------------+
void ProcessCommands()
{
   if(!FileIsExist("nova_commands.jsonl")) return; // executor creates it

   bool haveCursor = CursorExists();
   long cursor = haveCursor ? LoadCursor() : 0;

   int h = FileOpen("nova_commands.jsonl", FILE_READ|FILE_TXT|FILE_ANSI);
   if(h == INVALID_HANDLE)
   {
      Print("NovaTrader: cannot open nova_commands.jsonl, err=", GetLastError());
      return;
   }
   ulong fsize = FileSize(h);
   if(haveCursor && (ulong)cursor > fsize)
   {
      cursor = 0;
      Print("NovaTrader: commands file shrank below cursor, resetting to 0");
   }
   if(!haveCursor)
   {
      cursor = (long)fsize; // start at EOF: never replay pre-existing lines
      Print("NovaTrader: no cursor file, starting at EOF (", fsize, " bytes)");
   }
   if((ulong)cursor < fsize)
   {
      FileSeek(h, cursor, SEEK_SET);
      while(!FileIsEnding(h))
      {
         string line = FileReadString(h);
         HandleCommandLine(line);
      }
      cursor = (long)FileSize(h);
   }
   FileClose(h);
   SaveCursor(cursor);
}

//+------------------------------------------------------------------+
//| Symbol specs output                                              |
//+------------------------------------------------------------------+
// Retry SymbolSelect for master-list symbols missed at OnInit (post-restart
// Market Watch sync race). Runs on the specs cycle; heals g_symbols live.
void EnsureSymbols()
{
   int added = 0;
   for(int k = 0; k < g_nmaster && g_nsym < MAX_SYM; k++)
   {
      string sym = g_master[k];
      bool have = false;
      for(int j = 0; j < g_nsym; j++)
         if(g_symbols[j] == sym) { have = true; break; }
      if(have) continue;
      if(SymbolSelect(sym, true))
      {
         g_symbols[g_nsym++] = sym;
         added++;
      }
   }
   if(added > 0)
      Print("NovaTrader: recovered ", added, " symbols post-restart, total=", g_nsym);
}

void WriteSpecs()
{
   string syms = "";
   for(int i = 0; i < g_nsym; i++)
   {
      string s = g_symbols[i];
      int    digits = (int)SymbolInfoInteger(s, SYMBOL_DIGITS);
      double point  = SymbolInfoDouble(s, SYMBOL_POINT);
      double bid    = SymbolInfoDouble(s, SYMBOL_BID);
      double ask    = SymbolInfoDouble(s, SYMBOL_ASK);
      long   spread = 0;
      if(point > 0 && bid > 0 && ask > 0)
         spread = (long)MathRound((ask - bid) / point);
      if(i > 0) syms += ",";
      syms += StringFormat(
         "\"%s\":{\"tick_value\":%s,\"tick_size\":%s,\"volume_min\":%s,"
         "\"volume_max\":%s,\"volume_step\":%s,\"stops_level_points\":%d,"
         "\"spread_points\":%d,\"digits\":%d,\"point\":%s}",
         s,
         DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE), 8),
         DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE), 8),
         DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MIN), 8),
         DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MAX), 8),
         DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_STEP), 8),
         (int)SymbolInfoInteger(s, SYMBOL_TRADE_STOPS_LEVEL),
         spread, digits,
         DoubleToString(point, 8));
   }

   string js = StringFormat(
      "{\"time\":\"%s\",\"account\":{\"equity\":%.2f,\"balance\":%.2f,"
      "\"currency\":\"%s\",\"server\":\"%s\",\"time\":\"%s\"},\"symbols\":{%s}}",
      TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS),
      AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoString(ACCOUNT_CURRENCY),
      JsonEscape(AccountInfoString(ACCOUNT_SERVER)),
      TimeToString(TimeTradeServer(), TIME_DATE|TIME_SECONDS),
      syms);

   int h = FileOpen("nova_symbol_specs.json", FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, js);
      FileClose(h);
   }
   else
      Print("NovaTrader: failed to write nova_symbol_specs.json, err=", GetLastError());
}

//+------------------------------------------------------------------+
//| EA entry points                                                  |
//+------------------------------------------------------------------+
int OnInit()
{
   // DEMO-ONLY guard
   string server = AccountInfoString(ACCOUNT_SERVER);
   string slow = server;
   StringToLower(slow);
   if(StringFind(slow, "demo") < 0)
   {
      Print("NovaTrader: REFUSING TO RUN - account server '", server,
            "' is not a demo server. This EA is DEMO-ONLY.");
      return(INIT_FAILED);
   }

   string parts[];
   int total = LoadSymbolList(parts);
   g_nmaster = 0;
   g_nsym = 0;
   for(int k = 0; k < total && g_nmaster < MAX_SYM; k++)
   {
      string sym = parts[k];
      StringTrimLeft(sym);
      StringTrimRight(sym);
      if(StringLen(sym) == 0) continue;
      g_master[g_nmaster++] = sym;   // remember every requested symbol
      if(g_nsym >= MAX_SYM) continue;
      if(!SymbolSelect(sym, true))
      {
         Print("NovaTrader: symbol not carried by broker (will retry): ", sym);
         continue;
      }
      g_symbols[g_nsym++] = sym;
   }
   if(g_nsym == 0)
   {
      Print("NovaTrader: no usable symbols, init failed");
      return(INIT_FAILED);
   }

   EventSetTimer(InTimerSec);
   WriteSpecs(); // initial specs snapshot
   g_lastSpecs = TimeGMT();

   Print("NovaTrader ready: magic=", InMagic, " timer=", InTimerSec,
         "s specs=", InSpecsSec, "s symbols=", g_nsym, "/", g_nmaster);
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
void OnTimer()
{
   ProcessCommands();
   if(TimeGMT() - g_lastSpecs >= InSpecsSec)
   {
      g_lastSpecs = TimeGMT();
      EnsureSymbols();
      WriteSpecs();
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   // intentionally empty: all work happens in OnTimer
}
//+------------------------------------------------------------------+
