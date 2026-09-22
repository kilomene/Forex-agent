import { StyleSheet, Text, View } from 'react-native';
import Svg, { Circle } from 'react-native-svg';
import { colors, typography } from '@/theme/tokens';

/**
 * A circular progress ring for the agent's confidence score. Chosen over
 * a plain progress bar because confidence isn't really "how far along" —
 * it's "how much of the full circle of possible certainty this occupies,"
 * and a ring communicates that framing better than a linear bar borrowed
 * from download-progress UI.
 */
export function ConfidenceRing({ confidence, size = 64 }: { confidence: number | null; size?: number }) {
  const strokeWidth = 5;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const pct = confidence ?? 0;
  const dashOffset = circumference * (1 - pct / 100);

  const color = pct >= 65 ? colors.buy : pct >= 40 ? colors.amber : colors.sell;

  return (
    <View style={{ width: size, height: size }}>
      <Svg width={size} height={size}>
        <Circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          stroke={colors.border}
          strokeWidth={strokeWidth}
          fill="none"
        />
        {confidence != null && (
          <Circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            stroke={color}
            strokeWidth={strokeWidth}
            fill="none"
            strokeDasharray={circumference}
            strokeDashoffset={dashOffset}
            strokeLinecap="round"
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
          />
        )}
      </Svg>
      <View style={[StyleSheet.absoluteFill, styles.centerContent]}>
        <Text style={[typography.title, { color: confidence != null ? colors.textPrimary : colors.textTertiary }]}>
          {confidence != null ? `${confidence}` : '—'}
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  centerContent: {
    alignItems: 'center',
    justifyContent: 'center',
  },
});
