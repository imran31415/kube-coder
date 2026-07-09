/**
 * Global navigation-drawer state + a container-level navigation ref.
 *
 * The app replaced its crowded bottom tab bar with a hamburger drawer: every
 * top-level screen shows a ☰ button that calls openDrawer(); the NavDrawer
 * overlay lists all destinations and jumps to them via `navigationRef`. A tiny
 * pub/sub (same shape as the config store) lets the button and the drawer share
 * state without a heavier state library.
 */
import { useEffect, useState } from 'react';
import { createNavigationContainerRef } from '@react-navigation/native';

export const navigationRef = createNavigationContainerRef();

let drawerOpen = false;
let activeTab = 'Desktop';
const openListeners = new Set<(v: boolean) => void>();
const tabListeners = new Set<(v: string) => void>();

export function openDrawer(): void {
  if (drawerOpen) return;
  drawerOpen = true;
  openListeners.forEach((l) => l(true));
}

export function closeDrawer(): void {
  if (!drawerOpen) return;
  drawerOpen = false;
  openListeners.forEach((l) => l(false));
}

/** Called from the tab navigator's state listener so the drawer can highlight
 *  the current destination. */
export function setActiveTab(name: string): void {
  if (name === activeTab) return;
  activeTab = name;
  tabListeners.forEach((l) => l(name));
}

export function useDrawerOpen(): boolean {
  const [v, set] = useState(drawerOpen);
  useEffect(() => {
    openListeners.add(set);
    return () => {
      openListeners.delete(set);
    };
  }, []);
  return v;
}

export function useActiveTab(): string {
  const [v, set] = useState(activeTab);
  useEffect(() => {
    tabListeners.add(set);
    return () => {
      tabListeners.delete(set);
    };
  }, []);
  return v;
}

/** Jump to a top-level destination and close the drawer. */
export function navigateTo(name: string): void {
  if (navigationRef.isReady()) {
    // @ts-expect-error — route names are validated at the navigator, not here.
    navigationRef.navigate(name);
  }
  closeDrawer();
}
