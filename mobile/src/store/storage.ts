/**
 * Cross-platform secure-ish storage. On native we keep the API token in the
 * device keychain/keystore via expo-secure-store; on web (used for the
 * screenshot/demo build) SecureStore is unavailable, so we fall back to
 * localStorage via AsyncStorage. Non-secret values (host, flags) always use
 * AsyncStorage.
 */
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

const canUseSecureStore = Platform.OS !== 'web';

export async function getSecret(key: string): Promise<string | null> {
  try {
    if (canUseSecureStore) return await SecureStore.getItemAsync(key);
    return await AsyncStorage.getItem(key);
  } catch {
    return null;
  }
}

export async function setSecret(key: string, value: string): Promise<void> {
  try {
    if (canUseSecureStore) await SecureStore.setItemAsync(key, value);
    else await AsyncStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

export async function deleteSecret(key: string): Promise<void> {
  try {
    if (canUseSecureStore) await SecureStore.deleteItemAsync(key);
    else await AsyncStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

export async function getItem(key: string): Promise<string | null> {
  try {
    return await AsyncStorage.getItem(key);
  } catch {
    return null;
  }
}

export async function setItem(key: string, value: string): Promise<void> {
  try {
    await AsyncStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}
