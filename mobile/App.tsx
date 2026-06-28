/**
 * kube-coder mobile — root component.
 *
 * Boot flow: hydrate saved connection from secure storage → if no host+token,
 * show Onboarding; otherwise render the tab navigator. The demo/screenshot
 * build (EXPO_PUBLIC_MOCK=1) skips onboarding and starts straight in the app
 * with mock data.
 */
import { NavigationContainer, DefaultTheme } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { StatusBar } from 'expo-status-bar';
import React, { useEffect } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import OnboardingScreen from './src/screens/OnboardingScreen';
import TasksScreen from './src/screens/TasksScreen';
import TaskDetailScreen from './src/screens/TaskDetailScreen';
import NewTaskScreen from './src/screens/NewTaskScreen';
import MemoryScreen from './src/screens/MemoryScreen';
import MetricsScreen from './src/screens/MetricsScreen';
import SettingsScreen from './src/screens/SettingsScreen';
import type { TasksStackParams } from './src/navigation';
import { hydrate, isConfigured } from './src/store/config';
import { useConfig } from './src/store/useConfig';
import { colors, font } from './src/theme';

const navTheme = {
  ...DefaultTheme,
  dark: true,
  colors: {
    ...DefaultTheme.colors,
    background: colors.bg,
    card: colors.bgElevated,
    text: colors.text,
    border: colors.border,
    primary: colors.accent,
  },
};

const headerOptions = {
  headerStyle: { backgroundColor: colors.bgElevated },
  headerTintColor: colors.text,
  headerTitleStyle: { fontWeight: '700' as const },
  contentStyle: { backgroundColor: colors.bg },
};

const Stack = createNativeStackNavigator<TasksStackParams>();
function TasksStack() {
  return (
    <Stack.Navigator screenOptions={headerOptions}>
      <Stack.Screen name="TaskList" component={TasksScreen} options={{ headerShown: false }} />
      <Stack.Screen name="TaskDetail" component={TaskDetailScreen} options={{ title: 'Task' }} />
      <Stack.Screen
        name="NewTask"
        component={NewTaskScreen}
        options={{ title: 'New task', presentation: 'modal' }}
      />
    </Stack.Navigator>
  );
}

const Tab = createBottomTabNavigator();

const GLYPHS: Record<string, string> = {
  Tasks: '◧',
  Memory: '◆',
  Metrics: '▲',
  Settings: '⚙',
};

function TabIcon({ route, focused }: { route: string; focused: boolean }) {
  return (
    <Text style={[styles.tabIcon, { color: focused ? colors.accent : colors.textFaint }]}>
      {GLYPHS[route] ?? '•'}
    </Text>
  );
}

function MainTabs() {
  return (
    <Tab.Navigator
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarStyle: styles.tabBar,
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.textFaint,
        tabBarLabelStyle: { fontSize: 11, fontWeight: '600' },
        tabBarIcon: ({ focused }) => <TabIcon route={route.name} focused={focused} />,
      })}
    >
      <Tab.Screen name="Tasks" component={TasksStack} />
      <Tab.Screen name="Memory" component={MemoryScreen} />
      <Tab.Screen name="Metrics" component={MetricsScreen} />
      <Tab.Screen name="Settings" component={SettingsScreen} />
    </Tab.Navigator>
  );
}

function Gate() {
  const cfg = useConfig();
  if (!cfg.loaded) {
    return (
      <View style={styles.boot}>
        <Text style={styles.bootText}>kube-coder</Text>
      </View>
    );
  }
  return isConfigured() ? <MainTabs /> : <OnboardingScreen />;
}

export default function App() {
  useEffect(() => {
    hydrate();
  }, []);
  return (
    <SafeAreaProvider>
      <NavigationContainer theme={navTheme}>
        <StatusBar style="light" />
        <Gate />
      </NavigationContainer>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  tabBar: {
    backgroundColor: colors.bgElevated,
    borderTopColor: colors.border,
    height: 64,
    paddingBottom: 8,
    paddingTop: 6,
  },
  tabIcon: { fontSize: 20, lineHeight: 24 },
  boot: { flex: 1, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center' },
  bootText: { color: colors.text, fontSize: font.size.xl, fontWeight: '800' },
});
