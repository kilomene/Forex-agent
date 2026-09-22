import type { Direction, SignalStatus } from '@/api/types';

/** JPY pairs conventionally show 3 decimal places, others show 5. */
export function formatPrice(price: number, symbol: string): string {
  const decimals = symbol.includes('JPY') ? 3 : 5;
  return price.toFixed(decimals);
}

export function pipDistance(entry: number, target: number, symbol: string): number {
  const pipSize = symbol.includes('JPY') ? 0.01 : 0.0001;
  return Math.abs(entry - target) / pipSize;
}

export function formatPips(entry: number, target: number, symbol: string): string {
  return `${Math.round(pipDistance(entry, target, symbol))} pips`;
}

/** Spread is already a raw price-unit difference (ask - bid), not two price points — reuses the same pip-size logic as formatPips. */
export function formatSpread(spreadPriceUnits: number, symbol: string): string {
  const pipSize = symbol.includes('JPY') ? 0.01 : 0.0001;
  const pips = spreadPriceUnits / pipSize;
  return `${pips.toFixed(1)} pips`;
}

/** Reward:risk ratio from entry/stop/target — a quick quality signal on any setup. */
export function riskRewardRatio(entry: number, stopLoss: number, takeProfit: number): number {
  const risk = Math.abs(entry - stopLoss);
  const reward = Math.abs(takeProfit - entry);
  if (risk === 0) return 0;
  return reward / risk;
}

export function formatRelativeTime(isoString: string): string {
  const date = new Date(isoString);
  const diffMs = Date.now() - date.getTime();
  const diffSec = Math.round(diffMs / 1000);

  if (diffSec < 60) return 'just now';
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHour = Math.round(diffMin / 60);
  if (diffHour < 24) return `${diffHour}h ago`;
  const diffDay = Math.round(diffHour / 24);
  if (diffDay < 7) return `${diffDay}d ago`;
  return date.toLocaleDateString();
}

export function formatCurrency(value: number): string {
  const sign = value > 0 ? '+' : value < 0 ? '-' : '';
  return `${sign}$${Math.abs(value).toFixed(2)}`;
}

export function statusLabel(status: SignalStatus): string {
  const labels: Record<SignalStatus, string> = {
    pending: 'Pending review',
    approved: 'Approved',
    executed: 'Executed',
    closed: 'Closed',
    rejected_by_user: 'Rejected',
    rejected_by_risk: 'Blocked by risk engine',
    skipped_by_agent: 'Agent skipped',
    failed: 'Execution failed',
  };
  return labels[status];
}

export function directionLabel(direction: Direction): string {
  return direction === 'BUY' ? 'Long' : 'Short';
}
