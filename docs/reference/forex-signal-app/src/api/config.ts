/**
 * Worker connection config — base URL + shared bearer token — stored via
 * expo-secure-store, same pattern already used for the GitHub PAT in
 * GitDeploy. Never stored in plain AsyncStorage.
 */

import * as SecureStore from 'expo-secure-store';

const BASE_URL_KEY = 'forex_worker_base_url';
const API_KEY_KEY = 'forex_worker_api_key';

export interface ApiConfig {
  baseUrl: string;
  apiKey: string;
}

export async function getApiConfig(): Promise<ApiConfig | null> {
  const [baseUrl, apiKey] = await Promise.all([
    SecureStore.getItemAsync(BASE_URL_KEY),
    SecureStore.getItemAsync(API_KEY_KEY),
  ]);
  if (!baseUrl || !apiKey) return null;
  return { baseUrl, apiKey };
}

export async function setApiConfig(config: ApiConfig): Promise<void> {
  await Promise.all([
    SecureStore.setItemAsync(BASE_URL_KEY, config.baseUrl.replace(/\/$/, '')),
    SecureStore.setItemAsync(API_KEY_KEY, config.apiKey),
  ]);
}

export async function clearApiConfig(): Promise<void> {
  await Promise.all([
    SecureStore.deleteItemAsync(BASE_URL_KEY),
    SecureStore.deleteItemAsync(API_KEY_KEY),
  ]);
}
