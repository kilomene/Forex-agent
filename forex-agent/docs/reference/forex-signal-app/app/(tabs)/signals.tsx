import { useState } from 'react';
import { useRouter } from 'expo-router';
import { FlatList, RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSignals } from '@/hooks/useSignals';
import { SignalCard } from '@/components/SignalCard';
import { EmptyState, ErrorState, LoadingState } from '@/components/States';
import { colors, radius, spacing, typography } from '@/theme/tokens';
import type { SignalStatus } from '@/api/types';

const FILTERS: { label: string; status: SignalStatus | undefined }[] = [
  { label: 'All', status: undefined },
  { label: 'Pending', status: 'pending' },
  { label: 'Approved', status: 'approved' },
  { label: 'Executed', status: 'executed' },
];

export default function SignalsScreen() {
  const router = useRouter();
  const [filter, setFilter] = useState<SignalStatus | undefined>(undefined);
  const { signals, loading, refreshing, error, notConfigured, refresh } = useSignals(filter);

  if (notConfigured) {
    router.replace('/onboarding');
    return null;
  }

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.brandLabel}>ALPHA</Text>
        <Text style={styles.headerTitle}>Signals</Text>
      </View>

      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.filterRow}
      >
        {FILTERS.map((f) => (
          <FilterChip
            key={f.label}
            label={f.label}
            active={filter === f.status}
            onPress={() => setFilter(f.status)}
          />
        ))}
      </ScrollView>

      {loading ? (
        <LoadingState message="Loading signals…" />
      ) : error ? (
        <ErrorState message={error} onRetry={refresh} />
      ) : signals.length === 0 ? (
        <EmptyState
          icon="◈"
          title="No signals yet"
          message="New signals will appear here as soon as the bridge detects a setup."
        />
      ) : (
        <FlatList
          data={signals}
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

function FilterChip({ label, active, onPress }: { label: string; active: boolean; onPress: () => void }) {
  return (
    <Text
      onPress={onPress}
      style={[styles.chip, active && styles.chipActive]}
    >
      {label}
    </Text>
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
  brandLabel: {
    ...typography.label,
    color: colors.buy,
    marginBottom: 2,
  },
  filterRow: {
    paddingHorizontal: spacing.lg,
    gap: spacing.sm,
    paddingBottom: spacing.md,
  },
  chip: {
    ...typography.bodyStrong,
    color: colors.textSecondary,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radius.pill,
    overflow: 'hidden',
  },
  chipActive: {
    backgroundColor: colors.buyMuted,
    borderColor: colors.buy,
    color: colors.buy,
  },
  list: {
    padding: spacing.lg,
    paddingTop: 0,
  },
});
