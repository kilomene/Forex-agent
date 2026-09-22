import { useCallback, useEffect, useRef, useState } from 'react';
import { AppState, type AppStateStatus } from 'react-native';
import * as LocalAuthentication from 'expo-local-authentication';
import * as SecureStore from 'expo-secure-store';

const LOCK_ENABLED_KEY = 'forex_biometric_lock_enabled';

export async function isBiometricLockEnabled(): Promise<boolean> {
  const value = await SecureStore.getItemAsync(LOCK_ENABLED_KEY);
  return value === 'true';
}

export async function setBiometricLockEnabled(enabled: boolean): Promise<void> {
  await SecureStore.setItemAsync(LOCK_ENABLED_KEY, String(enabled));
}

export async function isBiometricHardwareAvailable(): Promise<boolean> {
  const hasHardware = await LocalAuthentication.hasHardwareAsync();
  const isEnrolled = await LocalAuthentication.isEnrolledAsync();
  return hasHardware && isEnrolled;
}

/**
 * Locks the app behind Face ID/fingerprint (falls back to device passcode
 * automatically — that's expo-local-authentication's default behavior,
 * not something disabled here). Re-locks whenever the app returns to the
 * foreground, not just on cold start — otherwise backgrounding the app
 * with it unlocked would defeat the point entirely.
 */
export function useBiometricLock() {
  const [enabled, setEnabled] = useState(false);
  const [unlocked, setUnlocked] = useState(false);
  const [checking, setChecking] = useState(true);
  const appState = useRef(AppState.currentState);

  const attemptUnlock = useCallback(async () => {
    const result = await LocalAuthentication.authenticateAsync({
      promptMessage: 'Unlock Forex Signal Bot',
      disableDeviceFallback: false,
    });
    setUnlocked(result.success);
    return result.success;
  }, []);

  useEffect(() => {
    isBiometricLockEnabled().then(async (isEnabled) => {
      setEnabled(isEnabled);
      if (isEnabled) {
        await attemptUnlock();
      } else {
        setUnlocked(true);
      }
      setChecking(false);
    });
  }, [attemptUnlock]);

  useEffect(() => {
    const subscription = AppState.addEventListener('change', (nextState: AppStateStatus) => {
      const cameFromBackground = appState.current.match(/inactive|background/) && nextState === 'active';
      if (cameFromBackground && enabled) {
        setUnlocked(false);
        attemptUnlock();
      }
      appState.current = nextState;
    });
    return () => subscription.remove();
  }, [enabled, attemptUnlock]);

  return { locked: enabled && !unlocked, checking, retry: attemptUnlock };
}
