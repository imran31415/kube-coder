/**
 * kube-coder mobile — root component.
 *
 * Boot flow: hydrate saved connection from secure storage → if no host+token,
 * show Onboarding; otherwise render the tab navigator. The demo/screenshot
 * build (EXPO_PUBLIC_MOCK=1) skips onboarding and starts straight in the app
 * with mock data.
 */
import { Ionicons } from '@expo/vector-icons';
import { NavigationContainer, DefaultTheme } from '@react-navigation/native';
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { StatusBar } from 'expo-status-bar';
import React, { useEffect } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { SafeAreaProvider, useSafeAreaInsets } from 'react-native-safe-area-context';

import OnboardingScreen from './src/screens/OnboardingScreen';
import TasksScreen from './src/screens/TasksScreen';
import TaskDetailScreen from './src/screens/TaskDetailScreen';
import NewTaskScreen from './src/screens/NewTaskScreen';
import AppsScreen from './src/screens/AppsScreen';
import AppViewScreen from './src/screens/AppViewScreen';
import DesktopScreen from './src/screens/DesktopScreen';
import MemoryScreen from './src/screens/MemoryScreen';
import MetricsScreen from './src/screens/MetricsScreen';
import SettingsScreen from './src/screens/SettingsScreen';
import type { AppsStackParams, TasksStackParams } from './src/navigation';
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

const AppsStackNav = createNativeStackNavigator<AppsStackParams>();
function AppsStack() {
  return (
    <AppsStackNav.Navigator screenOptions={headerOptions}>
      <AppsStackNav.Screen name="AppList" component={AppsScreen} options={{ headerShown: false }} />
      <AppsStackNav.Screen name="AppView" component={AppViewScreen} options={{ title: 'App' }} />
    </AppsStackNav.Navigator>
  );
}

const Tab = createBottomTabNavigator();

const TAB_ICONS: Record<string, [keyof typeof Ionicons.glyphMap, keyof typeof Ionicons.glyphMap]> = {
  Tasks: ['layers-outline', 'layers'],
  Desktop: ['grid-outline', 'grid'],
  Apps: ['globe-outline', 'globe'],
  Memory: ['bookmark-outline', 'bookmark'],
  Metrics: ['stats-chart-outline', 'stats-chart'],
  Settings: ['settings-outline', 'settings'],
};

function MainTabs() {
  // Explicit, inset-aware bar sizing: icon (22) + label must always fit. The
  // derived default clips labels when the bottom inset is 0 (web/older
  // devices); home-indicator devices get the inset as extra bottom padding.
  const insets = useSafeAreaInsets();
  const bottomPad = Math.max(insets.bottom, 8);
  return (
    <Tab.Navigator
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarStyle: [styles.tabBar, { height: 54 + bottomPad, paddingBottom: bottomPad }],
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.textFaint,
        // bottom-tabs gives the icon wrapper flex:1, which squeezes the label
        // to a clipped sliver on some layouts (notably web). Pin both slots —
        // the label also needs flexShrink:0 + an explicit height or its text
        // node gets crushed below its own line height and descenders clip.
        tabBarIconStyle: { flexGrow: 0, height: 26 },
        tabBarLabelStyle: {
          fontSize: 11,
          fontWeight: '600',
          lineHeight: 14,
          height: 16,
          flexShrink: 0,
        },
        tabBarIcon: ({ focused, color }) => {
          const [outline, filled] = TAB_ICONS[route.name] ?? ['ellipse-outline', 'ellipse'];
          return <Ionicons name={focused ? filled : outline} size={22} color={color} />;
        },
      })}
    >
      <Tab.Screen name="Tasks" component={TasksStack} />
      <Tab.Screen name="Desktop" component={DesktopScreen} />
      <Tab.Screen name="Apps" component={AppsStack} />
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
  // Height/paddingBottom come from MainTabs (inset-aware) so labels never
  // clip on inset-0 devices and the home indicator gets real clearance.
  tabBar: {
    backgroundColor: colors.bgElevated,
    borderTopColor: colors.border,
    paddingTop: 6,
  },
  boot: { flex: 1, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center' },
  bootText: { color: colors.text, fontSize: font.size.xl, fontWeight: '800' },
});
