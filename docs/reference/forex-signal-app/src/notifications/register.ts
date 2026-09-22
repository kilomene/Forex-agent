/**
 * Registers this device for push notifications from forex-signal-worker.
 *
 * IMPORTANT: the Worker sends pushes via FCM's HTTP v1 API directly (see
 * forex-signal-worker/src/fcm.js) — not through Expo's push notification
 * service. That means this needs the device's raw FCM token
 * (getDevicePushTokenAsync), not an Expo push token
 * (getExpoPushTokenAsync).
 *
 * This has a real consequence: raw FCM tokens require a native Firebase
 * config (google-services.json) baked into the build, which means this
 * WILL NOT WORK in Expo Go — it requires an EAS development or production
 * build, same as your other native-capable apps (TELSA, BITGEN) built via
 * GitHub Actions CI rather than Expo Go.
 */

import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Platform } from 'react-native';
import { registerDeviceToken } from '@/api/client';

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

export type RegistrationResult =
  | { ok: true }
  | { ok: false; reason: 'not-physical-device' | 'permission-denied' | 'registration-failed'; detail?: string };

export async function registerForPushNotifications(): Promise<RegistrationResult> {
  if (!Device.isDevice) {
    return { ok: false, reason: 'not-physical-device' };
  }

  const { status: existingStatus } = await Notifications.getPermissionsAsync();
  let finalStatus = existingStatus;

  if (existingStatus !== 'granted') {
    const { status } = await Notifications.requestPermissionsAsync();
    finalStatus = status;
  }

  if (finalStatus !== 'granted') {
    return { ok: false, reason: 'permission-denied' };
  }

  if (Platform.OS === 'android') {
    await Notifications.setNotificationChannelAsync('signals', {
      name: 'Trade Signals',
      importance: Notifications.AndroidImportance.HIGH,
      vibrationPattern: [0, 250, 250, 250],
    });
  }

  try {
    const tokenResponse = await Notifications.getDevicePushTokenAsync();
    await registerDeviceToken(tokenResponse.data);
    return { ok: true };
  } catch (err) {
    return { ok: false, reason: 'registration-failed', detail: err instanceof Error ? err.message : String(err) };
  }
}

/** Extracts the signal_id from a notification's data payload, for deep-linking to the detail screen. */
export function getSignalIdFromNotification(notification: Notifications.Notification): string | null {
  const data = notification.request.content.data;
  return typeof data?.signal_id === 'string' ? data.signal_id : null;
}
