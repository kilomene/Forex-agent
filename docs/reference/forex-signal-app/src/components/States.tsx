import { ActivityIndicator, StyleSheet, Text, View } from 'react-native';
import { colors, spacing, typography } from '@/theme/tokens';

export function LoadingState({ message = 'Loading…' }: { message?: string }) {
  return (
    <View style={styles.container}>
      <ActivityIndicator color={colors.textSecondary} />
      <Text style={styles.message}>{message}</Text>
    </View>
  );
}

export function EmptyState({ icon, title, message }: { icon: string; title: string; message?: string }) {
  return (
    <View style={styles.container}>
      <Text style={styles.icon}>{icon}</Text>
      <Text style={styles.title}>{title}</Text>
      {message && <Text style={styles.message}>{message}</Text>}
    </View>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <View style={styles.container}>
      <Text style={styles.icon}>⚠</Text>
      <Text style={styles.title}>Something went wrong</Text>
      <Text style={styles.message}>{message}</Text>
      {onRetry && (
        <Text style={styles.retry} onPress={onRetry}>
          Tap to retry
        </Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    padding: spacing.xxl,
    gap: spacing.sm,
  },
  icon: {
    fontSize: 32,
    marginBottom: spacing.sm,
  },
  title: {
    ...typography.title,
    color: colors.textPrimary,
    textAlign: 'center',
  },
  message: {
    ...typography.body,
    color: colors.textSecondary,
    textAlign: 'center',
  },
  retry: {
    ...typography.bodyStrong,
    color: colors.info,
    marginTop: spacing.sm,
  },
});
