import { useEffect, useRef } from 'react';
import { Stack, useRouter } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import * as Notifications from 'expo-notifications';
import { View } from 'react-native';
import { colors } from '@/theme/tokens';
import { getSignalIdFromNotification } from '@/notifications/register';
import { useBiometricLock } from '@/hooks/useBiometricLock';
import { LockScreen } from '@/components/LockScreen';

export default function RootLayout() {
  const router = useRouter();
  const responseListener = useRef<Notifications.EventSubscription>();
  const { locked, checking, retry } = useBiometricLock();

  useEffect(() => {
    // Tapping a push notification navigates straight to what it's about —
    // a signal push goes to that signal, a kill-switch push goes to
    // Settings where the kill switch lives, since there's no single
    // signal it's "about."
    responseListener.current = Notifications.addNotificationResponseReceivedListener((response) => {
      const data = response.notification.request.content.data;
      const signalId = getSignalIdFromNotification(response.notification);

      if (signalId) {
        router.push(`/signal/${signalId}`);
      } else if (typeof data?.type === 'string' && data.type.startsWith('kill_switch')) {
        router.push('/(tabs)/settings');
      }
    });

    return () => {
      responseListener.current?.remove();
    };
  }, [router]);

  return (
    <View style={{ flex: 1, backgroundColor: colors.background }}>
      <StatusBar style="light" />
      <Stack
        screenOptions={{
          headerStyle: { backgroundColor: colors.background },
          headerTintColor: colors.textPrimary,
          headerShadowVisible: false,
          contentStyle: { backgroundColor: colors.background },
        }}
      >
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="onboarding" options={{ headerShown: false, presentation: 'fullScreenModal' }} />
        <Stack.Screen name="signal/[id]" options={{ title: 'Signal', headerBackTitle: 'Back' }} />
      </Stack>
      {!checking && locked && <LockScreen onRetry={retry} />}
    </View>
  );
}
