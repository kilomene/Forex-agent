//+------------------------------------------------------------------+
//| NovaSignals.mq5                                                  |
//| EMA/RSI signal emitter for the Nova Telegram bridge.             |
//|                                                                  |
//| Strategy (mirrors forex-agent core/strategies EmaRsi):            |
//|   BUY  = EMA(fast) crosses above EMA(slow) AND RSI < overbought  |
//|   SELL = EMA(fast) crosses below EMA(slow) AND RSI > oversold    |
//|   SL = entry -/+ ATR*sl_mult, TP = entry +/- ATR*tp_mult         |
//| Evaluated on CLOSED candles only (no repaint).                   |
//|                                                                  |
//| Output: appends one JSON object per line to                      |
//|   MQL5/Files/nova_signals.jsonl                                  |
//| Heartbeat: MQL5/Files/nova_feed.json every 10 seconds with       |
//|   latest bid/ask per symbol (proves the feed is live).           |
//|                                                                  |
//| This EA never trades. It only observes and writes files.         |
//+------------------------------------------------------------------+
#property copyright "Nova Works"
#property version   "1.01"
#property strict

input string   InSymbols      = "EURUSD,GBPUSD,USDJPY,USDCHF,AUDUSD,USDCAD,NZDUSD,EURGBP,EURJPY,EURCHF,EURAUD,EURCAD,EURNZD,GBPJPY,GBPCHF,GBPAUD,GBPCAD,GBPNZD,AUDJPY,AUDCAD,AUDNZD,CADJPY,CHFJPY,NZDJPY,USDZAR,USDMXN,USDTRY,USDSEK,USDNOK,USDPLN,USDHKD,USDHUF,USDILS,USDTHB,EURPLN,EURSEK,EURDKK,GBPZAR,AUDSGD,NZDSGD,XAUUSD,XAUEUR,XAUAUD,XAUCHF,XAUGBP,XAGUSD,XAGEUR,XAGAUD,XAGGBP,XPTUSD,XPDUSD,NAS100,US100,US30,US500,RUS2000,VIX,GER40,EU50,FRA40,UK100,JPN225,HK50,AUS200,CHN50,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,NFLX,AMD,INTC,KO,PEP,WMT,NKE,DIS,MCD,JPM,BAC,SPCX,USOIL,WTI,UKOIL,BRENT,XNGUSD";
input ENUM_TIMEFRAMES InTimeframe = PERIOD_M15;
input int      InEmaFast      = 20;
input int      InEmaSlow      = 50;
input int      InRsiPeriod    = 14;
input int      InAtrPeriod    = 14;
input double   InRsiOverbought = 70.0;
input double   InRsiOversold   = 30.0;
input double   InAtrSLMult     = 1.5;
input double   InAtrTPMult     = 3.0;

// Symbol list source: MQL5/Files/nova_symbols.txt (first non-empty line,
// comma-separated) lets the operator change the watchlist without
// recompiling or reattaching the EA. Falls back to InSymbols if the file
// is absent. Symbols the broker does not carry are skipped gracefully.
#define MAX_SYM 128

string   g_symbols[MAX_SYM];
int      g_nsym = 0;
int      g_hFast[MAX_SYM], g_hSlow[MAX_SYM], g_hRsi[MAX_SYM], g_hAtr[MAX_SYM];
datetime g_lastBar[MAX_SYM];
datetime g_lastSigBar[MAX_SYM];
datetime g_lastBeat = 0;

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
      Print("NovaSignals: symbol list loaded from nova_symbols.txt");
   }
   if(StringLen(src) == 0)
   {
      src = InSymbols;
      Print("NovaSignals: nova_symbols.txt not found, using InSymbols input");
   }
   return(StringSplit(src, ',', out));
}

//+------------------------------------------------------------------+
int OnInit()
{
   string parts[];
   int total = LoadSymbolList(parts);
   g_nsym = 0;
   for(int k = 0; k < total && g_nsym < MAX_SYM; k++)
   {
      string sym = parts[k];
      StringTrimLeft(sym);
      StringTrimRight(sym);
      if(StringLen(sym) == 0) continue;
      if(!SymbolSelect(sym, true))
      {
         Print("NovaSignals: symbol not carried by broker, skipping: ", sym);
         continue;
      }
      int hf = iMA(sym, InTimeframe, InEmaFast, 0, MODE_EMA, PRICE_CLOSE);
      int hs = iMA(sym, InTimeframe, InEmaSlow, 0, MODE_EMA, PRICE_CLOSE);
      int hr = iRSI(sym, InTimeframe, InRsiPeriod, PRICE_CLOSE);
      int ha = iATR(sym, InTimeframe, InAtrPeriod);
      if(hf == INVALID_HANDLE || hs == INVALID_HANDLE ||
         hr == INVALID_HANDLE  || ha == INVALID_HANDLE)
      {
         Print("NovaSignals: indicator handle failed, skipping: ", sym);
         continue;
      }
      g_symbols[g_nsym]  = sym;
      g_hFast[g_nsym]    = hf;
      g_hSlow[g_nsym]    = hs;
      g_hRsi[g_nsym]     = hr;
      g_hAtr[g_nsym]     = ha;
      g_lastBar[g_nsym]  = iTime(sym, InTimeframe, 0);
      g_lastSigBar[g_nsym] = 0;
      g_nsym++;
   }
   if(g_nsym == 0)
   {
      Print("NovaSignals: no usable symbols, init failed");
      return(INIT_FAILED);
   }
   Print("NovaSignals: watching ", g_nsym, " symbols on ", EnumToString(InTimeframe));
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   for(int i = 0; i < g_nsym; i++)
   {
      IndicatorRelease(g_hFast[i]);
      IndicatorRelease(g_hSlow[i]);
      IndicatorRelease(g_hRsi[i]);
      IndicatorRelease(g_hAtr[i]);
   }
}

//+------------------------------------------------------------------+
bool GetBuf(int handle, int shift, double &val)
{
   double buf[];
   if(CopyBuffer(handle, 0, shift, 1, buf) < 1) return(false);
   val = buf[0];
   return(true);
}

//+------------------------------------------------------------------+
string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return(s);
}

//+------------------------------------------------------------------+
void EmitSignal(int i, string direction, double entry, double sl, double tp,
                double ef, double es, double rsi, double atr, datetime ct)
{
   string sym = g_symbols[i];
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   string tfname = EnumToString(InTimeframe);
   // strip "PERIOD_" prefix
   tfname = StringSubstr(tfname, 7);

   string id = StringFormat("%s_%s_%s_%d", sym, tfname, direction,
                            (int)ct);
   string json = StringFormat(
      "{\"id\":\"%s\",\"type\":\"signal.detected\",\"symbol\":\"%s\",\"timeframe\":\"%s\","
      "\"direction\":\"%s\",\"entry_price\":%s,\"stop_loss\":%s,\"take_profit\":%s,"
      "\"ema_fast\":%s,\"ema_slow\":%s,\"rsi_value\":%s,\"atr_value\":%s,"
      "\"candle_time\":\"%s\",\"strategy\":\"ema_rsi\","
      "\"trigger\":\"%s\",\"server_time\":\"%s\"}",
      JsonEscape(id), sym, tfname, direction,
      DoubleToString(entry, digits), DoubleToString(sl, digits), DoubleToString(tp, digits),
      DoubleToString(ef, digits), DoubleToString(es, digits),
      DoubleToString(rsi, 1), DoubleToString(atr, digits),
      TimeToString(ct, TIME_DATE|TIME_SECONDS),
      direction == "BUY" ?
         StringFormat("EMA%d crossed above EMA%d, RSI=%.1f", InEmaFast, InEmaSlow, rsi) :
         StringFormat("EMA%d crossed below EMA%d, RSI=%.1f", InEmaFast, InEmaSlow, rsi),
      TimeToString(TimeTradeServer(), TIME_DATE|TIME_SECONDS));

   int h = FileOpen("nova_signals.jsonl", FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileSeek(h, 0, SEEK_END);
      FileWriteString(h, json + "\n");
      FileClose(h);
      Print("NovaSignals: ", json);
   }
   else
      Print("NovaSignals: failed to open nova_signals.jsonl, err=", GetLastError());
}

//+------------------------------------------------------------------+
void CheckSymbol(int i)
{
   string sym = g_symbols[i];
   datetime cur0 = iTime(sym, InTimeframe, 0);
   if(cur0 == 0 || cur0 == g_lastBar[i]) return;   // no new bar yet
   g_lastBar[i] = cur0;

   // Evaluate on the just-closed bar (shift 1) vs prior closed bar (shift 2).
   double f1, f2, s1, s2, rsi1, atr1;
   if(!GetBuf(g_hFast[i], 1, f1)) return;
   if(!GetBuf(g_hFast[i], 2, f2)) return;
   if(!GetBuf(g_hSlow[i], 1, s1)) return;
   if(!GetBuf(g_hSlow[i], 2, s2)) return;
   if(!GetBuf(g_hRsi[i], 1, rsi1)) return;
   if(!GetBuf(g_hAtr[i], 1, atr1)) return;
   if(f1 == 0 || f2 == 0 || s1 == 0 || s2 == 0 || atr1 == 0) return;

   bool crossedUp   = (f2 <= s2 && f1 > s1);
   bool crossedDown = (f2 >= s2 && f1 < s1);
   if(!crossedUp && !crossedDown) return;

   datetime ct = iTime(sym, InTimeframe, 1);
   if(ct == g_lastSigBar[i]) return;               // one signal per candle
   double close1 = iClose(sym, InTimeframe, 1);
   if(close1 == 0) return;

   if(crossedUp && rsi1 < InRsiOverbought)
   {
      double sl = close1 - atr1 * InAtrSLMult;
      double tp = close1 + atr1 * InAtrTPMult;
      g_lastSigBar[i] = ct;
      EmitSignal(i, "BUY", close1, sl, tp, f1, s1, rsi1, atr1, ct);
   }
   else if(crossedDown && rsi1 > InRsiOversold)
   {
      double sl = close1 + atr1 * InAtrSLMult;
      double tp = close1 - atr1 * InAtrTPMult;
      g_lastSigBar[i] = ct;
      EmitSignal(i, "SELL", close1, sl, tp, f1, s1, rsi1, atr1, ct);
   }
}

//+------------------------------------------------------------------+
void WriteHeartbeat()
{
   string parts = "";
   for(int i = 0; i < g_nsym; i++)
   {
      string sym = g_symbols[i];
      int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double bid = SymbolInfoDouble(sym, SYMBOL_BID);
      double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
      datetime tt = (datetime)SymbolInfoInteger(sym, SYMBOL_TIME);
      if(i > 0) parts += ",";
      parts += StringFormat("\"%s\":{\"bid\":%s,\"ask\":%s,\"time\":\"%s\"}",
                            sym, DoubleToString(bid, digits), DoubleToString(ask, digits),
                            TimeToString(tt, TIME_DATE|TIME_SECONDS));
   }
   long login = AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   string json = StringFormat(
      "{\"type\":\"feed.heartbeat\",\"time\":\"%s\",\"server_time\":\"%s\","
      "\"connected\":%s,\"login\":%d,\"server\":\"%s\",\"balance\":%.2f,\"symbols\":{%s}}",
      TimeToString(TimeGMT(), TIME_DATE|TIME_SECONDS),
      TimeToString(TimeTradeServer(), TIME_DATE|TIME_SECONDS),
      TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false",
      login, JsonEscape(server), balance, parts);

   int h = FileOpen("nova_feed.json", FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, json);
      FileClose(h);
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   for(int i = 0; i < g_nsym; i++)
      CheckSymbol(i);

   if(TimeGMT() - g_lastBeat >= 10)
   {
      g_lastBeat = TimeGMT();
      WriteHeartbeat();
   }
}
//+------------------------------------------------------------------+
