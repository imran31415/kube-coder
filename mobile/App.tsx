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
import AppsScreen from './src/screens/AppsScreen';
import AppViewScreen from './src/screens/AppViewScreen';
import DesktopScreen from './src/screens/DesktopScreen';
import HypervisorScreen from './src/screens/HypervisorScreen';
import MemoryScreen from './src/screens/MemoryScreen';
import FilesScreen from './src/screens/FilesScreen';
import SkillsScreen from './src/screens/SkillsScreen';
import MetricsScreen from './src/screens/MetricsScreen';
import SettingsScreen from './src/screens/SettingsScreen';
import ControllerScreen from './src/screens/ControllerScreen';
import { NavDrawer } from './src/components/NavDrawer';
import { MenuButton } from './src/components/ui';
import type { AppsStackParams, TasksStackParams } from './src/navigation';
import { hasController, hydrate, isConfigured } from './src/store/config';
import { navigationRef, setActiveTab } from './src/store/nav';
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

/** Stack screenOptions with a dead-end guard: if a screen sits at the bottom
 *  of its stack (e.g. a deep link landed on a detail screen with nothing
 *  beneath it), there is no back button — lead with the ☰ menu instead so
 *  every screen keeps a way out. Otherwise leave headerLeft alone: setting it
 *  at all replaces the default back button. */
function stackScreenOptions({
  route,
  navigation,
}: {
  route: { key: string };
  navigation: { getState: () => { routes: { key: string }[] } };
}) {
  const first = navigation.getState().routes[0]?.key === route.key;
  return { ...headerOptions, ...(first ? { headerLeft: () => <MenuButton /> } : null) };
}

const Stack = createNativeStackNavigator<TasksStackParams>();
function TasksStack() {
  return (
    <Stack.Navigator screenOptions={stackScreenOptions}>
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
    <AppsStackNav.Navigator screenOptions={stackScreenOptions}>
      <AppsStackNav.Screen name="AppList" component={AppsScreen} options={{ headerShown: false }} />
      <AppsStackNav.Screen name="AppView" component={AppViewScreen} options={{ title: 'App' }} />
    </AppsStackNav.Navigator>
  );
}

const Tab = createBottomTabNavigator();

function MainTabs() {
  // The bottom tab bar was replaced by a hamburger drawer (too many
  // destinations for a 3–5 slot bar). The tab navigator still owns the screens
  // + their state; we just render no bar (tabBar={() => null}) and drive
  // navigation from the NavDrawer overlay. Desktop is first so it's the home.
  // The Controller screen is only registered once a controller connection
  // exists — with none, the app is unchanged.
  const showController = hasController(useConfig());
  return (
    <View style={styles.tabsHost}>
      <Tab.Navigator
        tabBar={() => null}
        backBehavior="history"
        screenOptions={{ headerShown: false }}
        screenListeners={{
          state: (e) => {
            const st = e.data?.state as { index?: number; routeNames?: string[] } | undefined;
            if (st && typeof st.index === 'number' && st.routeNames) {
              setActiveTab(st.routeNames[st.index]);
            }
          },
        }}
      >
        <Tab.Screen name="Desktop" component={DesktopScreen} />
        <Tab.Screen name="Hypervisor" component={HypervisorScreen} />
        <Tab.Screen name="Tasks" component={TasksStack} />
        <Tab.Screen name="Apps" component={AppsStack} />
        <Tab.Screen name="Memory" component={MemoryScreen} />
        <Tab.Screen name="Files" component={FilesScreen} />
        <Tab.Screen name="Skills" component={SkillsScreen} />
        <Tab.Screen name="Metrics" component={MetricsScreen} />
        {showController && <Tab.Screen name="Controller" component={ControllerScreen} />}
        <Tab.Screen name="Settings" component={SettingsScreen} />
      </Tab.Navigator>
      <NavDrawer />
    </View>
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
      <NavigationContainer theme={navTheme} ref={navigationRef}>
        <StatusBar style="light" />
        <Gate />
      </NavigationContainer>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  tabsHost: { flex: 1 },
  boot: { flex: 1, backgroundColor: colors.bg, alignItems: 'center', justifyContent: 'center' },
  bootText: { color: colors.text, fontSize: font.size.xl, fontWeight: '800' },
});
