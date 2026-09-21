import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useState, useCallback } from 'react';
import { usePerformance } from '@/hooks/usePerformance';
import { useReflections } from '@/hooks/useReflections';
import { EmptyState, ErrorState, LoadingState } from '@/components/States';
import { colors, radius, spacing, typography } from '@/theme/tokens';
import { formatCurrency, formatRelativeTime } from '@/utils/formatting';
import { parseBySymbol } from '@/api/types';

export default function PerformanceScreen() {
  const { snapshot, loading, error, refresh: refreshPerf } = usePerformance();
  const { reflections, refresh: refreshReflections } = useReflections();
  const [refreshing, setRefreshing] = useState(false);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    await Promise.all([refreshPerf(), refreshReflections()]);
    setRefreshing(false);
  }, [refreshPerf, refreshReflections]);

  if (loading) return <LoadingState message="Loading performance…" />;
  if (error) return <ErrorState message={error} onRetry={refreshPerf} />;

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={handleRefresh} tintColor={colors.textSecondary} />}
    >
      <Text style={styles.headerTitle}>Performance</Text>

      {!snapshot ? (
        <EmptyState
          icon="◔"
          title="No performance data yet"
          message="This fills in once performance_review.py reports its first snapshot from real MT5 deal history."
        />
      ) : (
        <>
          <View style={styles.heroCard}>
            <Text style={styles.heroLabel}>GROSS P/L · LAST {snapshot.total_closed_trades} TRADES</Text>
            <Text style={[styles.heroValue, { color: snapshot.total_profit >= 0 ? colors.buy : colors.sell }]}>
              {formatCurrency(snapshot.total_profit)}
            </Text>
            {snapshot.net_profit != null && (
              <Text style={styles.heroNetLine}>
                Net after real costs:{' '}
                <Text style={{ color: snapshot.net_profit >= 0 ? colors.buy : colors.sell, fontWeight: '700' }}>
                  {formatCurrency(snapshot.net_profit)}
                </Text>
              </Text>
            )}
            <Text style={styles.heroSubtext}>
              {snapshot.period_start.slice(0, 10)} — {snapshot.period_end.slice(0, 10)}
            </Text>
          </View>

          <View style={styles.statGrid}>
            <StatCard label="Win rate" value={snapshot.win_rate_pct != null ? `${snapshot.win_rate_pct}%` : '—'} />
            <StatCard label="Profit factor" value={snapshot.profit_factor != null ? snapshot.profit_factor.toFixed(2) : '—'} />
            <StatCard label="Wins" value={String(snapshot.wins)} accent={colors.buy} />
            <StatCard label="Losses" value={String(snapshot.losses)} accent={colors.sell} />
            <StatCard label="Avg win" value={snapshot.average_win != null ? formatCurrency(snapshot.average_win) : '—'} accent={colors.buy} />
            <StatCard label="Avg loss" value={snapshot.average_loss != null ? formatCurrency(snapshot.average_loss) : '—'} accent={colors.sell} />
            {snapshot.total_commission != null && (
              <StatCard label="Commission paid" value={formatCurrency(snapshot.total_commission)} accent={colors.sell} />
            )}
            {snapshot.total_swap != null && (
              <StatCard
                label="Swap paid"
                value={formatCurrency(snapshot.total_swap)}
                accent={snapshot.total_swap < 0 ? colors.sell : colors.buy}
              />
            )}
          </View>

          <Text style={styles.sectionTitle}>By symbol</Text>
          <View style={styles.card}>
            {Object.entries(parseBySymbol(snapshot)).map(([symbol, stats]) => (
              <View key={symbol} style={styles.symbolRow}>
                <Text style={styles.symbolName}>{symbol}</Text>
                <Text style={styles.symbolStat}>{stats.count} trades</Text>
                <Text style={[styles.symbolStat, { color: stats.profit >= 0 ? colors.buy : colors.sell }]}>
                  {formatCurrency(stats.profit)}
                </Text>
              </View>
            ))}
          </View>
        </>
      )}

      <Text style={styles.sectionTitle}>Recent reflections</Text>
      {reflections.length === 0 ? (
        <Text style={styles.emptyReflections}>No reflections recorded yet.</Text>
      ) : (
        reflections.slice(0, 5).map((r) => (
          <View key={r.id} style={styles.reflectionCard}>
            <View style={styles.reflectionHeader}>
              <Text style={styles.reflectionSymbol}>{r.symbol}</Text>
              <View
                style={[
                  styles.outcomePill,
                  { backgroundColor: r.outcome === 'win' ? colors.buyMuted : r.outcome === 'loss' ? colors.sellMuted : colors.surfaceRaised },
                ]}
              >
                <Text
                  style={[
                    styles.outcomeText,
                    { color: r.outcome === 'win' ? colors.buy : r.outcome === 'loss' ? colors.sell : colors.textSecondary },
                  ]}
                >
                  {r.outcome.toUpperCase()}
                </Text>
              </View>
              <Text style={styles.reflectionTime}>{formatRelativeTime(r.created_at)}</Text>
            </View>
            <Text style={styles.reflectionText}>{r.reflection_text}</Text>
          </View>
        ))
      )}
    </ScrollView>
  );
}

function StatCard({ label, value, accent }: { label: string; value: string; accent?: string }) {
  return (
    <View style={styles.statCard}>
      <Text style={styles.statCardLabel}>{label}</Text>
      <Text style={[styles.statCardValue, accent && { color: accent }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  content: { padding: spacing.lg, gap: spacing.lg, paddingBottom: spacing.xxl },
  headerTitle: { ...typography.displayLarge, color: colors.textPrimary },
  heroCard: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.xl,
    alignItems: 'center',
    gap: spacing.xs,
  },
  heroLabel: { ...typography.label, color: colors.textTertiary },
  heroValue: { ...typography.displayLarge, fontSize: 40 },
  heroSubtext: { ...typography.caption, color: colors.textTertiary },
  heroNetLine: { ...typography.caption, color: colors.textSecondary },
  statGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: spacing.md },
  statCard: {
    flexBasis: '31%',
    flexGrow: 1,
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
    gap: 4,
  },
  statCardLabel: { ...typography.label, color: colors.textTertiary },
  statCardValue: { ...typography.title, color: colors.textPrimary },
  sectionTitle: { ...typography.title, color: colors.textPrimary, marginTop: spacing.sm },
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
  },
  symbolRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: spacing.sm,
  },
  symbolName: { ...typography.bodyStrong, color: colors.textPrimary, flex: 1 },
  symbolStat: { ...typography.caption, color: colors.textSecondary, marginLeft: spacing.md },
  emptyReflections: { ...typography.body, color: colors.textTertiary },
  reflectionCard: {
    backgroundColor: colors.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.md,
    gap: spacing.xs,
  },
  reflectionHeader: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm },
  reflectionSymbol: { ...typography.bodyStrong, color: colors.textPrimary },
  outcomePill: { paddingHorizontal: spacing.sm, paddingVertical: 2, borderRadius: radius.pill },
  outcomeText: { ...typography.label },
  reflectionTime: { ...typography.caption, color: colors.textTertiary, marginLeft: 'auto' },
  reflectionText: { ...typography.caption, color: colors.textSecondary, lineHeight: 18 },
});
