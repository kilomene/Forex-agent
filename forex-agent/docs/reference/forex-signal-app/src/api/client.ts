/**
 * Typed client for forex-signal-worker's API. Every method here maps
 * 1:1 to a real route already built and tested in the Worker — see
 * forex-signal-worker/src/index.js for the source of truth.
 */

import { getApiConfig } from './config';
import type {
  AvailableChart,
  ChartData,
  PerformanceSnapshot,
  Reflection,
  Signal,
  SignalStatus,
  TradingMode,
} from './types';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'ApiError';
  }
}

export class NotConfiguredError extends Error {
  constructor() {
    super('No Worker connection configured yet.');
    this.name = 'NotConfiguredError';
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const config = await getApiConfig();
  if (!config) throw new NotConfiguredError();

  const response = await fetch(`${config.baseUrl}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${config.apiKey}`,
      ...options.headers,
    },
  });

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as { error?: string };
      if (body.error) message = body.error;
    } catch {
      // response wasn't JSON, keep the generic message
    }
    throw new ApiError(response.status, message);
  }

  return response.json() as Promise<T>;
}

// --- Signals ---

export function listSignals(params?: { status?: SignalStatus; limit?: number }): Promise<{ signals: Signal[] }> {
  const query = new URLSearchParams();
  if (params?.status) query.set('status', params.status);
  if (params?.limit) query.set('limit', String(params.limit));
  const qs = query.toString();
  return request(`/signals${qs ? `?${qs}` : ''}`);
}

export function getSignal(id: string): Promise<{ signal: Signal }> {
  return request(`/signals/${id}`);
}

export function approveSignal(id: string): Promise<{ ok: true }> {
  return request(`/signals/${id}/approve`, { method: 'POST' });
}

export function rejectSignal(id: string): Promise<{ ok: true }> {
  return request(`/signals/${id}/reject`, { method: 'POST' });
}

// --- Settings ---

export function getTradingMode(): Promise<{ trading_mode: TradingMode }> {
  return request('/settings/trading-mode');
}

export function setTradingMode(mode: TradingMode): Promise<{ ok: true; trading_mode: TradingMode }> {
  return request('/settings/trading-mode', {
    method: 'POST',
    body: JSON.stringify({ mode }),
  });
}

// --- Performance ---

export function getLatestPerformance(): Promise<
  { available: false } | { available: true; snapshot: PerformanceSnapshot }
> {
  return request('/performance/latest');
}

// --- Reflections ---

export function listReflections(params?: { symbol?: string; limit?: number }): Promise<{ reflections: Reflection[] }> {
  const query = new URLSearchParams();
  if (params?.symbol) query.set('symbol', params.symbol);
  if (params?.limit) query.set('limit', String(params.limit));
  const qs = query.toString();
  return request(`/reflections${qs ? `?${qs}` : ''}`);
}

// --- Devices (push notifications) ---

export function registerDeviceToken(token: string): Promise<{ ok: true }> {
  return request('/devices/register', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });
}

export function sendTestPush(): Promise<{ ok: boolean; sent?: number; total?: number; error?: string }> {
  return request('/devices/test-push', { method: 'POST' });
}

// --- Kill switch ---

export function getKillSwitchState(): Promise<{ engaged: boolean; close_all_requested_at: string | null }> {
  return request('/kill-switch');
}

export function getAiProviderInfo(): Promise<{ provider: string; uses_tool_calling: boolean; model: string | null }> {
  return request('/settings/ai-provider');
}

export function setKillSwitchEngaged(engaged: boolean): Promise<{ ok: true; engaged: boolean }> {
  return request('/kill-switch', {
    method: 'POST',
    body: JSON.stringify({ engaged }),
  });
}

export function requestCloseAll(): Promise<{ ok: true }> {
  return request('/kill-switch/close-all', { method: 'POST' });
}

// --- Self-hosted AI model connection (Alibaba VM, etc) ---

export function getCustomAiSettings(): Promise<{ url: string | null; has_api_key: boolean }> {
  return request('/settings/custom-ai');
}

export function saveCustomAiUrl(url: string): Promise<{ ok: true; url: string | null; has_api_key: boolean }> {
  return request('/settings/custom-ai', {
    method: 'POST',
    body: JSON.stringify({ url }),
  });
}

export function saveCustomAiApiKey(apiKey: string): Promise<{ ok: true; url: string | null; has_api_key: boolean }> {
  return request('/settings/custom-ai', {
    method: 'POST',
    body: JSON.stringify({ api_key: apiKey }),
  });
}

export function clearCustomAiSettings(): Promise<{ ok: true }> {
  return request('/settings/custom-ai/clear', { method: 'POST' });
}

export function testCustomAiVm(url: string): Promise<{ ok: boolean; detail?: unknown }> {
  return request('/settings/custom-ai/test-vm', {
    method: 'POST',
    body: JSON.stringify({ url }),
  });
}

export function testCustomAiModel(url: string, apiKey: string): Promise<{ ok: boolean; detail?: string; sample_response?: string }> {
  return request('/settings/custom-ai/test-model', {
    method: 'POST',
    body: JSON.stringify({ url, api_key: apiKey }),
  });
}

// --- Charts ---

export function listAvailableCharts(): Promise<{ charts: AvailableChart[] }> {
  return request('/chart-data/available');
}

export function getChartData(symbol: string, timeframe: string): Promise<ChartData> {
  const query = new URLSearchParams({ symbol, timeframe });
  return request(`/chart-data?${query.toString()}`);
}

// --- Connection test (used by onboarding) ---

export async function testConnection(baseUrl: string, apiKey: string): Promise<boolean> {
  const response = await fetch(`${baseUrl.replace(/\/$/, '')}/settings/trading-mode`, {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  return response.ok;
}
