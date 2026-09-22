import { Tabs } from 'expo-router';
import { Text } from 'react-native';
import { colors } from '@/theme/tokens';

function TabIcon({ symbol, focused }: { symbol: string; focused: boolean }) {
  return (
    <Text style={{ fontSize: 20, opacity: focused ? 1 : 0.5 }}>{symbol}</Text>
  );
}

export default function TabLayout() {
  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: colors.background },
        headerTintColor: colors.textPrimary,
        headerShadowVisible: false,
        tabBarStyle: {
          backgroundColor: colors.surface,
          borderTopColor: colors.border,
        },
        tabBarActiveTintColor: colors.buy,
        tabBarInactiveTintColor: colors.textTertiary,
      }}
    >
      <Tabs.Screen
        name="signals"
        options={{
          title: 'Signals',
          tabBarIcon: ({ focused }) => <TabIcon symbol="◈" focused={focused} />,
        }}
      />
      <Tabs.Screen
        name="charts"
        options={{
          title: 'Charts',
          tabBarIcon: ({ focused }) => <TabIcon symbol="📊" focused={focused} />,
        }}
      />
      <Tabs.Screen
        name="history"
        options={{
          title: 'History',
          tabBarIcon: ({ focused }) => <TabIcon symbol="≡" focused={focused} />,
        }}
      />
      <Tabs.Screen
        name="performance"
        options={{
          title: 'Performance',
          tabBarIcon: ({ focused }) => <TabIcon symbol="◔" focused={focused} />,
        }}
      />
      <Tabs.Screen
        name="settings"
        options={{
          title: 'Settings',
          tabBarIcon: ({ focused }) => <TabIcon symbol="⚙" focused={focused} />,
        }}
      />
    </Tabs>
  );
}
