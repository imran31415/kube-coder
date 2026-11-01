/**
 * Cross-platform secure-ish storage. On native we keep the API token in the
 * device keychain/keystore via expo-secure-store; on web (used for the
 * screenshot/demo build) SecureStore is unavailable, so we fall back to
 * localStorage via AsyncStorage. In the desktop app (#833) both go through the
 * desktop core: the token to the OS keychain, the rest to a settings file.
 * Which backend is which lives in ./storageBackend. Non-secret values (host,
 * flags) never go to a secure store.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

import { invoke, isDesktop } from '../desktop/bridge';
import { desktopBackend, nativeBackend, pickBackend, webBackend } from './storageBackend';

const backend = pickBackend({
  desktop: isDesktop(),
  web: Platform.OS === 'web',
  make: {
    desktop: () => desktopBackend(invoke),
    web: () => webBackend(AsyncStorage),
    native: () => nativeBackend(AsyncStorage, SecureStore),
  },
});

export async function getSecret(key: string): Promise<string | null> {
  try {
    return await backend.getSecret(key);
  } catch {
    return null;
  }
}

export async function setSecret(key: string, value: string): Promise<void> {
  try {
    await backend.setSecret(key, value);
  } catch {
    /* ignore */
  }
}

export async function deleteSecret(key: string): Promise<void> {
  try {
    await backend.deleteSecret(key);
  } catch {
    /* ignore */
  }
}

export async function getItem(key: string): Promise<string | null> {
  try {
    return await backend.getItem(key);
  } catch {
    return null;
  }
}

export async function setItem(key: string, value: string): Promise<void> {
  try {
    await backend.setItem(key, value);
  } catch {
    /* ignore */
  }
}
