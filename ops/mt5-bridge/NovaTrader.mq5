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
//| OUTPUT  MQL5/Files/nova_positions.json (every open broker         |
//|   position, rewritten every InPositionsSec seconds and on any     |
//|   change of the open ticket set)                                 |
//| STATE   MQL5/Files/nova_positions_seen.json (persisted ticket set |
//|   for exit reconciliation: survives EA/terminal restarts so a     |
//|   broker-side exit is never lost or double-reported)              |
//| CURSOR  MQL5/Files/nova_trader.cursor (persisted byte offset)     |
//|                                                                  |
//| POSITION MANAGEMENT (our magic 20260921 only):                   |
//|  - Breakeven lock: at +1R the SL moves to entry (never widened).  |
//|  - Exit reconciliation: tickets that vanish broker-side without  |
//|    a trade.close command emit trade.closed with real P&L from     |
//|    deal history, or an honest unknown/under-reconciliation mark   |
//|    when the closing deal cannot be found. Never invents P&L.      |
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
input int    InPositionsSec    = 60;      // broker positions publish interval
input bool   InBreakevenLock   = true;   // at +1R move SL to entry (never widen)
input string InSymbols         = "EURUSD,GBPUSD,USDJPY,USDCHF,AUDUSD,USDCAD,NZDUSD,XAUUSD,XAGUSD,US30,US500";

// Symbol list source: MQL5/Files/nova_symbols.txt (first non-empty line,
// comma-separated). Falls back to InSymbols if the file is absent.
// Symbols the broker does not carry are skipped gracefully.
#define MAX_SYM 128

string g_symbols[MAX_SYM];
int    g_nsym      = 0;
long   g_lastSpecs = 0;
long   g_lastPositions = 0;
string g_lastPositionsSig = "";

// Tickets we have opened (hedge mode: position ticket == opening order
// ticket), persisted to nova_positions_seen.json so exit reconciliation
// survives EA/terminal restarts. ReconcilePositions() watches them and
// reports broker-side exits (SL/TP hits, manual closes) that no
// trade.close command caused.
struct SeenTicket
{
   ulong  ticket;
   string symbol;
   string direction;    // "BUY" / "SELL"
   double volume;
   long   tracked_at;   // TimeGMT() when first tracked
   long   missing_since;// TimeGMT() when first observed missing, 0 = open
};
SeenTicket g_seen[];

// A ticket missing broker-side for longer than this without a closing
// deal in history gets an honest unknown/under-reconciliation close
// record (P&L never invented). Deal history syncs within minutes, so
// 24h is generous.
#define RECONCILE_UNKNOWN_HOURS 24

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
//| Seen-ticket persistence (nova_positions_seen.json)                |
//| Survives EA/terminal restarts: the exit reconciler never loses   |
//| track of our tickets and never double-reports a close.           |
//+------------------------------------------------------------------+
int SeenIndex(ulong ticket)
{
   int n = ArraySize(g_seen);
   for(int i = 0; i < n; i++)
      if(g_seen[i].ticket == ticket) return(i);
   return(-1);
}

void SaveSeenTickets()
{
   string items = "";
   int n = ArraySize(g_seen);
   for(int i = 0; i < n; i++)
   {
      if(i > 0) items += ",";
      items += StringFormat(
         "{\"ticket\":%I64d,\"symbol\":\"%s\",\"type\":\"%s\","
         "\"volume\":%s,\"tracked_at\":%d,\"missing_since\":%d}",
         (long)g_seen[i].ticket, JsonEscape(g_seen[i].symbol),
         g_seen[i].direction, DoubleToString(g_seen[i].volume, 8),
         g_seen[i].tracked_at, g_seen[i].missing_since);
   }
   string js = "{\"tickets\":[" + items + "]}";
   int h = FileOpen("nova_positions_seen.json", FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, js);
      FileClose(h);
   }
   else
      Print("NovaTrader: failed to write nova_positions_seen.json, err=",
            GetLastError());
}

// Parse one flat {"ticket":N,...} entry starting at entryPos for the
// named fields. Used for both the seen file and nova_positions.json.
bool ParseTicketEntry(const string txt, int entryPos, ulong &ticket,
                      string &symbol, string &type, double &volume,
                      long &magic)
{
   ticket = 0; symbol = ""; type = ""; volume = 0; magic = 0;
   int p = StringFind(txt, "\"ticket\":", entryPos);
   if(p < 0 || p > entryPos + 600) return(false);
   p += 9;
   int n = StringLen(txt), q = p;
   while(q < n)
   {
      ushort c = StringGetCharacter(txt, q);
      if(c >= '0' && c <= '9') q++; else break;
   }
   if(q == p) return(false);
   ticket = (ulong)StringToInteger(StringSubstr(txt, p, q - p));
   int ps = StringFind(txt, "\"symbol\":\"", p);
   if(ps >= 0 && ps < p + 600)
   {
      ps += 10;
      int qs = StringFind(txt, "\"", ps);
      if(qs > ps) symbol = StringSubstr(txt, ps, qs - ps);
   }
   int pt = StringFind(txt, "\"type\":\"", p);
   if(pt >= 0 && pt < p + 600)
   {
      pt += 8;
      int qt = StringFind(txt, "\"", pt);
      if(qt > pt) type = StringSubstr(txt, pt, qt - pt);
   }
   int pv = StringFind(txt, "\"volume\":", p);
   if(pv >= 0 && pv < p + 600)
   {
      pv += 9;
      int qv = pv, nn = StringLen(txt);
      while(qv < nn)
      {
         ushort c = StringGetCharacter(txt, qv);
         if((c >= '0' && c <= '9') || c == '.') qv++; else break;
      }
      if(qv > pv) volume = StringToDouble(StringSubstr(txt, pv, qv - pv));
   }
   int pm = StringFind(txt, "\"magic\":", p);
   if(pm >= 0 && pm < p + 600)
   {
      pm += 8;
      int qm = pm;
      while(qm < n)
      {
         ushort c = StringGetCharacter(txt, qm);
         if(c >= '0' && c <= '9') qm++; else break;
      }
      if(qm > pm) magic = StringToInteger(StringSubstr(txt, pm, qm - pm));
   }
   return(true);
}

string ReadWholeFile(const string name)
{
   if(!FileIsExist(name)) return("");
   int h = FileOpen(name, FILE_READ|FILE_TXT|FILE_ANSI);
   if(h == INVALID_HANDLE) return("");
   string txt = "";
   while(!FileIsEnding(h)) txt += FileReadString(h) + "\n";
   FileClose(h);
   return(txt);
}

void LoadSeenTickets()
{
   ArrayResize(g_seen, 0);
   string txt = ReadWholeFile("nova_positions_seen.json");
   if(StringLen(txt) == 0) return;
   int pos = 0, loaded = 0;
   while(true)
   {
      int p = StringFind(txt, "\"ticket\":", pos);
      if(p < 0) break;
      ulong ticket; string symbol, type; double volume; long magic;
      if(!ParseTicketEntry(txt, p, ticket, symbol, type, volume, magic)) break;
      if(ticket > 0 && SeenIndex(ticket) < 0)
      {
         int k = ArraySize(g_seen);
         ArrayResize(g_seen, k + 1);
         g_seen[k].ticket = ticket;
         g_seen[k].symbol = symbol;
         g_seen[k].direction = type;
         g_seen[k].volume = volume;
         g_seen[k].tracked_at = TimeGMT();
         g_seen[k].missing_since = 0;
         int pms = StringFind(txt, "\"missing_since\":", p);
         if(pms >= 0 && pms < p + 600)
         {
            pms += 16;
            int qms = pms, nn = StringLen(txt);
            while(qms < nn)
            {
               ushort c = StringGetCharacter(txt, qms);
               if(c >= '0' && c <= '9') qms++; else break;
            }
            if(qms > pms)
               g_seen[k].missing_since =
                  StringToInteger(StringSubstr(txt, pms, qms - pms));
         }
         loaded++;
      }
      pos = p + 9;
   }
   if(loaded > 0)
      Print("NovaTrader: loaded ", loaded, " seen tickets from nova_positions_seen.json");
}

// Seed the seen set from the last published nova_positions.json so a
// restart never drops tickets that were open at shutdown. Only our
// magic is tracked.
void SeedFromPublishedPositions()
{
   string txt = ReadWholeFile("nova_positions.json");
   if(StringLen(txt) == 0) return;
   int pos = 0, added = 0;
   while(true)
   {
      int p = StringFind(txt, "\"ticket\":", pos);
      if(p < 0) break;
      ulong ticket; string symbol, type; double volume; long magic;
      if(!ParseTicketEntry(txt, p, ticket, symbol, type, volume, magic)) break;
      if(ticket > 0 && magic == InMagic && SeenIndex(ticket) < 0)
      {
         int k = ArraySize(g_seen);
         ArrayResize(g_seen, k + 1);
         g_seen[k].ticket = ticket;
         g_seen[k].symbol = symbol;
         g_seen[k].direction = type;
         g_seen[k].volume = volume;
         g_seen[k].tracked_at = TimeGMT();
         g_seen[k].missing_since = 0;
         added++;
      }
      pos = p + 9;
   }
   if(added > 0)
   {
      SaveSeenTickets();
      Print("NovaTrader: seeded ", added, " tickets from last nova_positions.json");
   }
}

void TrackTicket(ulong ticket, const string symbol, const string direction,
                 double volume)
{
   int i = SeenIndex(ticket);
   long now = TimeGMT();
   if(i >= 0)
   {
      g_seen[i].symbol = symbol;
      g_seen[i].direction = direction;
      g_seen[i].volume = volume;
      g_seen[i].missing_since = 0;
   }
   else
   {
      int k = ArraySize(g_seen);
      ArrayResize(g_seen, k + 1);
      g_seen[k].ticket = ticket;
      g_seen[k].symbol = symbol;
      g_seen[k].direction = direction;
      g_seen[k].volume = volume;
      g_seen[k].tracked_at = now;
      g_seen[k].missing_since = 0;
   }
   SaveSeenTickets();
}

void UntrackTicket(ulong ticket)
{
   int i = SeenIndex(ticket);
   if(i < 0) return;
   int n = ArraySize(g_seen);
   g_seen[i] = g_seen[n - 1];
   ArrayResize(g_seen, n - 1);
   SaveSeenTickets();
}

// Duplicate guard: has nova_trades.jsonl already got a trade.closed for
// this ticket? Makes reconciliation idempotent even if the seen file is
// lost or hand-edited.
bool AlreadyClosedInTradesFile(ulong ticket)
{
   string txt = ReadWholeFile("nova_trades.jsonl");
   if(StringLen(txt) == 0) return(false);
   string key = "\"ticket\":" + IntegerToString((long)ticket);
   int klen = StringLen(key);
   int pos = 0;
   while(true)
   {
      int p = StringFind(txt, "\"trade.closed\"", pos);
      if(p < 0) break;
      // Restrict the match to the single JSON line holding this
      // trade.closed. A fixed char window can bleed into the NEXT line
      // and match that record's ticket (e.g. a following trade.opened),
      // causing a false "already closed" and a silently dropped close.
      int lineStart = p;
      while(lineStart > 0 && StringGetCharacter(txt, lineStart - 1) != '\n')
         lineStart--;
      int lineEnd = StringFind(txt, "\n", p);
      if(lineEnd < 0) lineEnd = StringLen(txt);
      string seg = StringSubstr(txt, lineStart, lineEnd - lineStart);
      int q = StringFind(seg, key);
      if(q >= 0)
      {
         int after = q + klen;
         if(after >= StringLen(seg)) return(true);
         ushort c = StringGetCharacter(seg, after);
         if(c < '0' || c > '9') return(true); // exact ticket match
      }
      pos = p + 13;
   }
   return(false);
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

   // Spread filter: REMOVED per owner order 2026-09-22 ("nothing should be
   // blocking you from trading"). InMaxSpreadPoints is kept as a vestigial
   // input for profile compatibility but is no longer consulted.

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
   // Hedge mode: position ticket == opening order ticket
   TrackTicket((ulong)result.order, symbol, direction, vol);
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
   UntrackTicket((ulong)ticket);
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

   string serverName = AccountInfoString(ACCOUNT_SERVER);
   // Account identity for the executor's identity lock (§7): the login is
   // authoritative from the terminal; the type is derived the same way the
   // OnInit demo gate derives it (server name contains "demo" -> DEMO).
   string lowerSrv = serverName;
   StringToLower(lowerSrv);
   string acctType = (StringFind(lowerSrv, "demo") >= 0) ? "DEMO" : "LIVE";
   string js = StringFormat(
      "{\"time\":\"%s\",\"account\":{\"equity\":%.2f,\"balance\":%.2f,"
      "\"currency\":\"%s\",\"login\":%I64d,\"type\":\"%s\","
      "\"server\":\"%s\",\"time\":\"%s\"},\"symbols\":{%s}}",
      TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS),
      AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoString(ACCOUNT_CURRENCY),
      AccountInfoInteger(ACCOUNT_LOGIN),
      acctType,
      JsonEscape(serverName),
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
//| PublishPositions: write EVERY open broker position to            |
//| nova_positions.json (positions as an explicit risk input, so      |
//| gates see manual positions too). Rewritten on the positions      |
//| interval AND on any change of the open ticket set.               |
//+------------------------------------------------------------------+
void PublishPositions()
{
   int n = PositionsTotal();
   string items = "";
   for(int i = 0; i < n; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string sym   = PositionGetString(POSITION_SYMBOL);
      double vol   = PositionGetDouble(POSITION_VOLUME);
      int    ptype = (int)PositionGetInteger(POSITION_TYPE);
      long   magic = PositionGetInteger(POSITION_MAGIC);
      double open  = PositionGetDouble(POSITION_PRICE_OPEN);
      double cur   = PositionGetDouble(POSITION_PRICE_CURRENT);
      double pl    = PositionGetDouble(POSITION_PROFIT)
                   + PositionGetDouble(POSITION_SWAP);
      double sl    = PositionGetDouble(POSITION_SL);
      double tp    = PositionGetDouble(POSITION_TP);
      long   tm    = PositionGetInteger(POSITION_TIME);
      if(i > 0) items += ",";
      items += StringFormat(
         "{\"ticket\":%I64d,\"symbol\":\"%s\",\"volume\":%s,\"type\":\"%s\","
         "\"magic\":%d,\"open_price\":%s,\"current_price\":%s,"
         "\"profit\":%s,\"sl\":%s,\"tp\":%s,\"open_time\":%d}",
         (long)ticket, sym, DoubleToString(vol, 2),
         (ptype == POSITION_TYPE_BUY ? "BUY" : "SELL"),
         magic, DoubleToString(open, 8), DoubleToString(cur, 8),
         DoubleToString(pl, 2), DoubleToString(sl, 8),
         DoubleToString(tp, 8), tm);
      // Self-healing watch list: every currently-open position is tracked
      // for exit reconciliation, including positions opened before this
      // EA version started (they are never in the seed file otherwise).
      TrackTicket(ticket, sym,
                  (ptype == POSITION_TYPE_BUY ? "BUY" : "SELL"), vol);
   }
   string js = StringFormat(
      "{\"time\":%d,\"server_time\":\"%s\",\"account\":%I64d,\"positions\":[%s]}",
      TimeGMT(), TimeToString(TimeTradeServer(), TIME_DATE | TIME_SECONDS),
      AccountInfoInteger(ACCOUNT_LOGIN), items);

   int h = FileOpen("nova_positions.json", FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, js);
      FileClose(h);
   }
   else
      Print("NovaTrader: failed to write nova_positions.json, err=", GetLastError());
}

// Publish on the interval, or immediately when the open ticket set
// changes (open/close events propagate to the risk gates within ~2 s).
void MaybePublishPositions()
{
   string sig = "";
   int n = PositionsTotal();
   for(int i = 0; i < n; i++)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      sig += IntegerToString((long)t) + ",";
   }
   bool due = (TimeGMT() - g_lastPositions >= InPositionsSec);
   if(!due && sig == g_lastPositionsSig) return;
   g_lastPositions = TimeGMT();
   g_lastPositionsSig = sig;
   PublishPositions();
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

   // Restore the exit-reconciliation watch list across restarts. The
   // first broker-positions snapshot goes out on the first OnTimer
   // tick via MaybePublishPositions().
   LoadSeenTickets();
   SeedFromPublishedPositions();
   g_lastPositions = 0;
   g_lastPositionsSig = "";

   Print("NovaTrader ready: magic=", InMagic, " timer=", InTimerSec,
         "s specs=", InSpecsSec, "s positions=", InPositionsSec,
         "s breakeven=", (InBreakevenLock ? "on" : "off"),
         " symbols=", g_nsym, "/", g_nmaster,
         " seen_tickets=", ArraySize(g_seen));
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
//| Breakeven lock (owner rule): for every OUR position (magic only):|
//|  1. The SL set at entry is NEVER widened or removed.             |
//|  2. Once price moves 1R in our favor, the SL is moved to the     |
//|     entry price. A winner can then never become a loser -- the   |
//|     worst exit after +1R is a scratch.                           |
//| Runs on every OnTimer tick (2 s). Manual positions (other magic) |
//| are never touched.                                               |
//+------------------------------------------------------------------+
void ManagePositions()
{
   if(!InBreakevenLock) return;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != InMagic) continue;
      string symbol = PositionGetString(POSITION_SYMBOL);
      long   ptype  = PositionGetInteger(POSITION_TYPE);
      double entry  = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl     = PositionGetDouble(POSITION_SL);
      double tp     = PositionGetDouble(POSITION_TP);
      if(entry <= 0 || sl <= 0) continue;

      int    digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      double point  = SymbolInfoDouble(symbol, SYMBOL_POINT);
      if(point <= 0) continue;
      MqlTick tick;
      if(!SymbolInfoTick(symbol, tick)) continue;

      bool   isBuy    = (ptype == POSITION_TYPE_BUY);
      double riskDist = isBuy ? (entry - sl) : (sl - entry); // 1R in price
      if(riskDist <= 0) continue;

      double fav = isBuy ? (tick.bid - entry) : (entry - tick.ask);
      // SL still worse than entry AND trade has earned +1R -> lock breakeven
      bool needBE = isBuy ? (sl < entry) : (sl > entry);
      if(!needBE || fav < riskDist) continue;

      double newSL = NormalizeDouble(entry, digits);
      // never widen: new SL must be strictly better than current
      bool better = isBuy ? (newSL > sl) : (newSL < sl);
      if(!better) continue;
      // respect broker stops level
      long   stopsPts = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
      double minDist  = stopsPts * point;
      double refPrice = isBuy ? tick.bid : tick.ask;
      double gap      = isBuy ? (refPrice - newSL) : (newSL - refPrice);
      if(gap < minDist) continue; // too close to market; retry next tick

      MqlTradeRequest req;
      MqlTradeResult  res;
      ZeroMemory(req);
      ZeroMemory(res);
      req.action   = TRADE_ACTION_SLTP;
      req.symbol   = symbol;
      req.position = ticket;
      req.sl       = newSL;
      req.tp       = tp;
      req.magic    = InMagic;
      if(OrderSend(req, res) && res.retcode == TRADE_RETCODE_DONE)
         Print("NovaTrader: BREAKEVEN LOCK ticket=", ticket, " ", symbol,
               " SL -> entry ", DoubleToString(newSL, digits));
      else
         Print("NovaTrader: breakeven modify failed ticket=", ticket,
               " retcode=", res.retcode, " err=", GetLastError());
   }
}

//+------------------------------------------------------------------+
//| ReconcilePositions: detect broker-side exits (SL/TP hits, manual |
//| closes) of OUR tickets and emit trade.closed. Real P&L comes     |
//| from deal history (summed across all OUT deals for the ticket,   |
//| latest deal sets price/time). If no closing deal is found after   |
//| RECONCILE_UNKNOWN_HOURS of the ticket being missing, an honest    |
//| unknown/under-reconciliation record is emitted -- P&L is NEVER    |
//| invented. Idempotent: the persisted seen set, UntrackTicket, and  |
//| the AlreadyClosedInTradesFile guard prevent duplicate reports    |
//| across restarts. Manual (non-magic) positions are ignored.       |
//+------------------------------------------------------------------+
void ReconcilePositions()
{
   int n = ArraySize(g_seen);
   if(n == 0) return;
   bool needHistory = false;
   for(int k = 0; k < n; k++)
   {
      if(!PositionSelectByTicket(g_seen[k].ticket))
      { needHistory = true; break; }
   }
   if(!needHistory)
   {
      // everything accounted for: clear any stale missing flags
      bool cleared = false;
      for(int k = 0; k < n; k++)
         if(g_seen[k].missing_since != 0)
         { g_seen[k].missing_since = 0; cleared = true; }
      if(cleared) SaveSeenTickets();
      return;
   }

   datetime to = TimeCurrent() + 60;
   datetime from = to - 2 * 86400;
   if(!HistorySelect(from, to)) return;

   long now = TimeGMT();
   for(int k = ArraySize(g_seen) - 1; k >= 0; k--)
   {
      ulong ticket = g_seen[k].ticket;
      if(PositionSelectByTicket(ticket))
      {
         if(g_seen[k].missing_since != 0)
         {
            g_seen[k].missing_since = 0;
            SaveSeenTickets();
         }
         continue;
      }

      // Position is gone broker-side.
      if(AlreadyClosedInTradesFile(ticket))
      {
         // Close already journaled (e.g. by a command close or an
         // earlier reconcile): just stop watching, emit nothing.
         Print("NovaTrader: reconcile: ticket=", ticket,
               " already has trade.closed, untracking");
         UntrackTicket(ticket);
         continue;
      }
      if(g_seen[k].missing_since == 0)
      {
         g_seen[k].missing_since = now;
         SaveSeenTickets();
         Print("NovaTrader: reconcile: ticket=", ticket, " ",
               g_seen[k].symbol, " missing broker-side, watching deal history");
      }

      // Find ALL closing (OUT) deals for this ticket in history.
      ulong  lastDeal  = 0;
      datetime closeTime = 0;
      double closePrice = 0;
      double closeVol   = 0;
      double profitSum  = 0;
      string psym  = "";
      long   dtype = -1;
      int total = HistoryDealsTotal();
      for(int i = 0; i < total; i++)
      {
         ulong d = HistoryDealGetTicket(i);
         if(d == 0) continue;
         if(HistoryDealGetInteger(d, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
         if((ulong)HistoryDealGetInteger(d, DEAL_POSITION_ID) != ticket)
            continue;
         if(HistoryDealGetInteger(d, DEAL_MAGIC) != InMagic) continue;
         profitSum += HistoryDealGetDouble(d, DEAL_PROFIT)
                    + HistoryDealGetDouble(d, DEAL_SWAP)
                    + HistoryDealGetDouble(d, DEAL_COMMISSION);
         datetime dt = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
         if(dt >= closeTime)
         {
            closeTime  = dt;
            lastDeal   = d;
            closePrice = HistoryDealGetDouble(d, DEAL_PRICE);
            psym       = HistoryDealGetString(d, DEAL_SYMBOL);
            dtype      = HistoryDealGetInteger(d, DEAL_TYPE);
         }
         closeVol += HistoryDealGetDouble(d, DEAL_VOLUME);
      }

      if(lastDeal == 0)
      {
         // No closing deal in the 2-day history window yet. Keep
         // watching; after RECONCILE_UNKNOWN_HOURS emit an honest
         // unknown record rather than a phantom open forever.
         long missingFor = now - g_seen[k].missing_since;
         if(missingFor >= RECONCILE_UNKNOWN_HOURS * 3600)
         {
            string js = StringFormat(
               "{\"type\":\"trade.closed\",\"ticket\":%I64d,\"exit_price\":null,"
               "\"profit\":null,\"profit_status\":\"unknown\",\"reason\":\"broker\","
               "\"time\":\"%s\",\"symbol\":\"%s\",\"direction\":\"%s\","
               "\"volume\":%s,\"note\":\"%s\"}",
               (long)ticket,
               TimeToString(TimeTradeServer(), TIME_DATE|TIME_SECONDS),
               JsonEscape(g_seen[k].symbol), g_seen[k].direction,
               DoubleToString(g_seen[k].volume, 8),
               "broker-side exit; closing deal not found in 2-day history "
               "after 24h missing; P&L under reconciliation, not invented");
            AppendTradeLine(js);
            Print("NovaTrader: reconciled UNKNOWN close ticket=", (long)ticket,
                  " ", g_seen[k].symbol, " (no closing deal found)");
            UntrackTicket(ticket);
         }
         continue;
      }

      if(StringLen(psym) == 0) psym = g_seen[k].symbol;
      string dir = g_seen[k].direction;
      if(StringLen(dir) == 0)
         dir = (dtype == DEAL_TYPE_SELL) ? "BUY" : "SELL";
      int digits = (int)SymbolInfoInteger(psym, SYMBOL_DIGITS);
      if(digits <= 0) digits = 5;

      string js = StringFormat(
         "{\"type\":\"trade.closed\",\"ticket\":%I64d,\"exit_price\":%s,"
         "\"profit\":%.2f,\"reason\":\"broker\",\"time\":\"%s\","
         "\"symbol\":\"%s\",\"direction\":\"%s\",\"volume\":%s,"
         "\"note\":\"broker-side exit detected by EA position watch\"}",
         (long)ticket, DoubleToString(closePrice, digits), profitSum,
         TimeToString(closeTime, TIME_DATE|TIME_SECONDS),
         JsonEscape(psym), dir,
         DoubleToString(closeVol > 0 ? closeVol : g_seen[k].volume, 8));
      AppendTradeLine(js);
      Print("NovaTrader: reconciled broker close ticket=", (long)ticket,
            " ", psym, " @ ", DoubleToString(closePrice, digits),
            " profit=", DoubleToString(profitSum, 2));
      UntrackTicket(ticket);
   }
}

//+------------------------------------------------------------------+
void OnTimer()
{
   ProcessCommands();
   ManagePositions();
   ReconcilePositions();
   if(TimeGMT() - g_lastSpecs >= InSpecsSec)
   {
      g_lastSpecs = TimeGMT();
      EnsureSymbols();
      WriteSpecs();
   }
   MaybePublishPositions();
}

//+------------------------------------------------------------------+
void OnTick()
{
   // intentionally empty: all work happens in OnTimer
}
//+------------------------------------------------------------------+
