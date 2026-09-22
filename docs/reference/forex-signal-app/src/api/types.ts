/**
 * Types mirror the Worker's D1 schema (forex-signal-worker/schema.sql)
 * field-for-field — this is the contract between the two projects. If a
 * column is added there, it should be added here.
 */

export type SignalStatus =
  | 'pending'
  | 'approved'
  | 'executed'
  | 'closed'
  | 'rejected_by_user'
  | 'rejected_by_risk'
  | 'skipped_by_agent'
  | 'failed';

export type Direction = 'BUY' | 'SELL';
export type AgentDecision = 'enter' | 'skip';

export interface OhlcCandle {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface SmcStructureBreak {
  index: number;
  direction: 'bullish' | 'bearish';
  level: number;
  time: string;
}

export interface SmcZone {
  type: 'resistance' | 'support';
  price_low: number;
  price_high: number;
  touch_count: number;
}

export interface SmcFvg {
  type: 'bullish' | 'bearish';
  index: number;
  top: number;
  bottom: number;
  time: string;
}

export interface SmcOrderBlock {
  type: 'bullish' | 'bearish';
  index: number;
  high: number;
  low: number;
  time: string;
  caused_break_at_index: number;
}

export interface SmcSummary {
  available: boolean;
  note?: string;
  market_structure?: {
    trend: 'uptrend' | 'downtrend' | 'ranging';
    last_swing_high: { price: number; time: string } | null;
    last_swing_low: { price: number; time: string } | null;
  };
  recent_structure_breaks?: SmcStructureBreak[];
  support_resistance_near_price?: SmcZone[];
  liquidity_zones?: { buy_side: SmcZone[]; sell_side: SmcZone[] };
  fair_value_gaps_recent?: SmcFvg[];
  order_blocks_recent?: SmcOrderBlock[];
}

export interface ToolCall {
  tool: string;
  input: Record<string, unknown>;
}

export interface Signal {
  id: string;
  symbol: string;
  timeframe: string;
  direction: Direction;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  ema_fast: number | null;
  ema_slow: number | null;
  rsi_value: number | null;
  atr_value: number | null;
  candle_time: string;
  trigger_text: string | null;
  ai_reasoning: string | null;
  ai_confidence: number | null;
  ai_risk_notes: string | null;
  ai_tool_calls: string | null; // JSON string, parse with parseToolCalls()
  ai_decision: AgentDecision | null;
  status: SignalStatus;
  mt5_result: string | null;
  rejection_reason: string | null;
  closed_reason: string | null;
  closed_price: number | null;
  closed_at: string | null;
  // Real cost data — see forex-bot-bridge/cost_tracking.py and
  // worker_client.py. spread_at_entry/lot_size come from the actual
  // order result; commission/swap come from MT5's real deal history.
  spread_at_entry: number | null;
  lot_size: number | null;
  commission: number | null;
  swap: number | null;
  created_at: string;
  updated_at: string;
  // Raw JSON text from D1 — parse with parseRecentCandles/parseSmcSummary/
  // parseSymbolSpecs below, same pattern as ai_tool_calls/parseToolCalls.
  // These were previously typed as pre-parsed objects, which was wrong —
  // the Worker never actually persisted them, so every real fetch
  // silently returned undefined. Fixed on both sides together.
  recent_candles: string | null;
  smc_summary: string | null;
  symbol_specs: string | null;
}

export interface PerformanceSnapshot {
  id: string;
  period_start: string;
  period_end: string;
  total_closed_trades: number;
  wins: number;
  losses: number;
  breakeven: number;
  win_rate_pct: number | null;
  total_profit: number;
  average_win: number | null;
  average_loss: number | null;
  profit_factor: number | null;
  largest_win: number | null;
  largest_loss: number | null;
  by_symbol: string; // JSON string
  total_commission: number | null;
  total_swap: number | null;
  net_profit: number | null; // total_profit + total_commission + total_swap
  created_at: string;
}

export interface Reflection {
  id: string;
  signal_id: string;
  symbol: string;
  direction: Direction;
  outcome: 'win' | 'loss' | 'breakeven';
  entry_price: number;
  closed_price: number;
  original_reasoning: string | null;
  original_confidence: number | null;
  reflection_text: string;
  created_at: string;
}

export type TradingMode = 'manual' | 'autonomous';

// --- Charts (independent of the signal pipeline — see forex-bot-bridge/chart_data.py) ---

export interface AvailableChart {
  symbol: string;
  timeframe: string;
  updated_at: string;
}

export interface ChartData {
  available: boolean;
  symbol?: string;
  timeframe?: string;
  candles?: OhlcCandle[];
  ema_fast?: (number | null)[] | null;
  ema_slow?: (number | null)[] | null;
  ema_fast_period?: number;
  ema_slow_period?: number;
  updated_at?: string;
}
export function parseToolCalls(signal: Signal): ToolCall[] {
  if (!signal.ai_tool_calls) return [];
  try {
    return JSON.parse(signal.ai_tool_calls) as ToolCall[];
  } catch {
    return [];
  }
}

export function parseRecentCandles(signal: Signal): OhlcCandle[] {
  if (!signal.recent_candles) return [];
  try {
    return JSON.parse(signal.recent_candles) as OhlcCandle[];
  } catch {
    return [];
  }
}

export function parseSmcSummary(signal: Signal): SmcSummary | null {
  if (!signal.smc_summary) return null;
  try {
    return JSON.parse(signal.smc_summary) as SmcSummary;
  } catch {
    return null;
  }
}

export interface SymbolSpecs {
  tick_value: number;
  tick_size: number;
  contract_size: number;
  digits: number;
}

export function parseSymbolSpecs(signal: Signal): SymbolSpecs | null {
  if (!signal.symbol_specs) return null;
  try {
    return JSON.parse(signal.symbol_specs) as SymbolSpecs;
  } catch {
    return null;
  }
}

export function parseBySymbol(snapshot: PerformanceSnapshot): Record<string, { count: number; profit: number; wins: number }> {
  try {
    return JSON.parse(snapshot.by_symbol);
  } catch {
    return {};
  }
}
