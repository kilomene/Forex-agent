import { View } from 'react-native';
import Svg, { Line, Polyline } from 'react-native-svg';
import { colors } from '@/theme/tokens';
import type { OhlcCandle, Direction } from '@/api/types';

/**
 * A minimal price-path sparkline from the real candles the bridge sent
 * with the signal (smc_analysis.py's source data) — not a placeholder
 * chart. Deliberately simple (closes only, no OHLC wicks) since this is
 * a glance-level "which way has it been moving" indicator, not a full
 * charting surface — a real candlestick chart belongs in a future
 * dedicated chart screen, not squeezed into a signal card.
 */
export function PriceSparkline({
  candles,
  direction,
  width = 280,
  height = 56,
}: {
  candles: OhlcCandle[];
  direction: Direction;
  width?: number;
  height?: number;
}) {
  if (!candles || candles.length < 2) return null;

  const closes = candles.map((c) => c.close);
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const range = max - min || 1;
  const padding = 6;

  const points = closes
    .map((price, i) => {
      const x = (i / (closes.length - 1)) * width;
      const y = padding + (1 - (price - min) / range) * (height - padding * 2);
      return `${x},${y}`;
    })
    .join(' ');

  const color = direction === 'BUY' ? colors.buy : colors.sell;
  const midY = padding + (1 - (closes[0] - min) / range) * (height - padding * 2);

  return (
    <View>
      <Svg width={width} height={height}>
        <Line x1={0} y1={midY} x2={width} y2={midY} stroke={colors.border} strokeWidth={1} strokeDasharray="3,3" />
        <Polyline points={points} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      </Svg>
    </View>
  );
}
