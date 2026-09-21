import { useMemo, useState } from 'react';
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useAvailableCharts, useChartData } from '@/hooks/useChartData';
import { CandlestickChart } from '@/components/CandlestickChart';
import { EmptyState, ErrorState, LoadingState } from '@/components/States';
import { colors, radius, spacing, typography } from '@/theme/tokens';
import { formatPrice, formatRelativeTime } from '@/utils/formatting';

export default function ChartsScreen() {
  const { charts, loading: chartsLoading, error: chartsError, refresh: refreshAvailable } = useAvailableCharts();
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [selectedTimeframe, setSelectedTimeframe] = useState<string | null>(null);

  // Once the available list loads, default to the first symbol/timeframe
  // rather than showing an empty screen the user has to configure first.
  const effectiveSymbol = selectedSymbol ?? charts[0]?.symbol ?? null;
  const symbolTimeframes = useMemo(
    () => charts.filter((c) => c.symbol === effectiveSymbol).map((c) => c.timeframe),
    [charts, effectiveSymbol]
  );
  const effectiveTimeframe = selectedTimeframe && symbolTimeframes.includes(selectedTimeframe)
    ? selectedTimeframe
    : symbolTimeframes[0] ?? null;

  const availableSymbols = useMemo(() => [...new Set(charts.map((c) => c.symbol))], [charts]);

  const { data, loading: chartLoading, refreshing, error: chartError, refresh } = useChartData(
    effectiveSymbol,
    effectiveTimeframe
  );

  if (chartsLoading) return <LoadingState message="Loading available charts…" />;
  if (chartsError) return <ErrorState message={chartsError} onRetry={refreshAvailable} />;

  if (charts.length === 0) {
    return (
      <EmptyState
        icon="📊"
        title="No chart data yet"
        message="This fills in once the bridge's chart_data_loop reports its first update — usually within a couple of minutes of starting the bridge."
      />
    );
  }

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <Text style={styles.headerTitle}>Charts</Text>
      </View>

      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.pickerRow}>
        {availableSymbols.map((symbol) => (
          <Chip
            key={symbol}
            label={symbol}
            active={symbol === effectiveSymbol}
            onPress={() => {
              setSelectedSymbol(symbol);
              setSelectedTimeframe(null); // reset so it defaults to that symbol's first available timeframe
            }}
          />
        ))}
      </ScrollView>

      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.pickerRow}>
        {symbolTimeframes.map((tf) => (
          <Chip key={tf} label={tf} active={tf === effectiveTimeframe} onPress={() => setSelectedTimeframe(tf)} />
        ))}
      </ScrollView>

      <ScrollView
        contentContainerStyle={styles.content}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} tintColor={colors.textSecondary} />}
      >
        {chartLoading ? (
          <LoadingState message={`Loading ${effectiveSymbol} ${effectiveTimeframe}…`} />
        ) : chartError ? (
          <ErrorState message={chartError} onRetry={refresh} />
        ) : !data?.available || !data.candles ? (
          <EmptyState icon="📊" title="No data for this pair yet" />
        ) : (
          <>
            <View style={styles.chartCard}>
              <CandlestickChart
                candles={data.candles}
                emaFast={data.ema_fast}
                emaSlow={data.ema_slow}
                width={340}
                height={260}
              />
            </View>

            <View style={styles.legendRow}>
              {data.ema_fast && (
                <LegendItem color={colors.amber} label={`EMA ${data.ema_fast_period}`} />
              )}
              {data.ema_slow && (
                <LegendItem color={colors.info} label={`EMA ${data.ema_slow_period}`} />
              )}
            </View>

            <View style={styles.statsRow}>
              <Stat label="Last close" value={formatPrice(data.candles[data.candles.length - 1].close, effectiveSymbol!)} />
              <Stat label="Candles shown" value={String(data.candles.length)} />
            </View>

            {/* Honest about staleness — this is refreshed on a schedule by
                the bridge, not a live stream, and saying so plainly here
                matters more than looking like a real-time terminal. */}
            <Text style={styles.staleness}>
              Updated {data.updated_at ? formatRelativeTime(data.updated_at) : 'unknown'} — refreshes automatically,
              not a live feed.
            </Text>
          </>
        )}
      </ScrollView>
    </View>
  );
}

function Chip({ label, active, onPress }: { label: string; active: boolean; onPress: () => void }) {
  return (
    <Text onPress={onPress} style={[styles.chip, active && styles.chipActive]}>
      {label}
    </Text>
  );
}

function LegendItem({ color, label }: { color: string; label: string }) {
  return (
    <View style={styles.legendItem}>
      <View style={[styles.legendSwatch, { backgroundColor: color }]} />
      <Text style={styles.legendLabel}>{label}</Text>
    </View>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statLabel}>{label}</Text>
      <Text style={styles.statValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  header: { paddingHorizontal: spacing.lg, paddingTop: spacing.md, paddingBottom: spacing.sm },
  headerTitle: { ...typography.displayLarge, color: colors.textPrimary },
  pickerRow: { paddingHorizontal: spacing.lg, gap: spacing.sm, paddingBottom: spacing.sm },
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
  chipActive: { backgroundColor: colors.buyMuted, borderColor: colors.buy, color: colors.buy },
  content: { padding: spacing.lg, paddingTop: spacing.sm, gap: spacing.md },
  chartCard: {
    backgroundColor: colors.surface,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: spacing.lg,
    alignItems: 'center',
  },
  legendRow: { flexDirection: 'row', gap: spacing.lg },
  legendItem: { flexDirection: 'row', alignItems: 'center', gap: spacing.xs },
  legendSwatch: { width: 10, height: 10, borderRadius: 5 },
  legendLabel: { ...typography.caption, color: colors.textSecondary },
  statsRow: { flexDirection: 'row', gap: spacing.xl },
  stat: { gap: 2 },
  statLabel: { ...typography.label, color: colors.textTertiary },
  statValue: { ...typography.mono, color: colors.textPrimary },
  staleness: { ...typography.caption, color: colors.textTertiary },
});
