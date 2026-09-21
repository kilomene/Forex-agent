import { useRouter } from 'expo-router';
import { FlatList, RefreshControl, StyleSheet, Text, View } from 'react-native';
import { useSignals } from '@/hooks/useSignals';
import { SignalCard } from '@/components/SignalCard';
import { EmptyState, ErrorState, LoadingState } from '@/components/States';
import { colors, spacing, typography } from '@/theme/tokens';

export default function HistoryScreen() {
  const router = useRouter();
  // No status filter — history shows everything that isn't still pending,
  // which is more useful than a single status since "history" should mean
  // "everything that's been decided," not just one lifecycle stage.
  const { signals, loading, refreshing, error, refresh } = useSignals();
  const decided = signals.filter((s) => s.status !== 'pending' && s.status !== 'approved');

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.headerTitle}>History</Text>
      </View>

      {loading ? (
        <LoadingState message="Loading history…" />
      ) : error ? (
        <ErrorState message={error} onRetry={refresh} />
      ) : decided.length === 0 ? (
        <EmptyState icon="≡" title="No history yet" message="Decided signals — executed, closed, or skipped — will show up here." />
      ) : (
        <FlatList
          data={decided}
          keyExtractor={(s) => s.id}
          contentContainerStyle={styles.list}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} tintColor={colors.textSecondary} />}
          renderItem={({ item }) => (
            <SignalCard signal={item} onPress={() => router.push(`/signal/${item.id}`)} />
          )}
        />
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  header: {
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
    paddingBottom: spacing.sm,
  },
  headerTitle: {
    ...typography.displayLarge,
    color: colors.textPrimary,
  },
  list: {
    padding: spacing.lg,
    paddingTop: 0,
  },
});
