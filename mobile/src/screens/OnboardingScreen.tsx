/** First-run screen: enter workspace host + API token, validate, persist. */
import React, { useState } from 'react';
import {
  KeyboardAvoidingView,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { LinearGradient } from 'expo-linear-gradient';
import { Button, Label } from '../components/ui';
import { ping } from '../api/client';
import { getConfig, saveConnection } from '../store/config';
import { colors, font, gradients, radius, space } from '../theme';

export default function OnboardingScreen() {
  const [host, setHost] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function connect() {
    setError(null);
    if (!/^https?:\/\//.test(host.trim())) {
      setError('Host must start with https://');
      return;
    }
    if (!token.trim()) {
      setError('Paste your API token');
      return;
    }
    setBusy(true);
    // Persist first so the client can read host+token, then validate.
    await saveConnection(host, token);
    try {
      await ping();
      // success — App re-renders into the tab navigator via config subscription
    } catch (e) {
      setError(`Could not connect: ${(e as Error).message}`);
      // roll back so we stay on onboarding
      await saveConnection('', '');
      setHost(host);
      setToken(token);
    } finally {
      setBusy(false);
      // keep fields populated if we rolled back
      if (getConfig().host) return;
    }
  }

  return (
    <SafeAreaView style={styles.safe} edges={['top', 'bottom']}>
      <KeyboardAvoidingView
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        style={{ flex: 1 }}
      >
        <ScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled">
          <View style={styles.logoWrap}>
            <LinearGradient
              colors={gradients.brand}
              start={{ x: 0, y: 0 }}
              end={{ x: 1, y: 1 }}
              style={styles.logo}
            >
              <Text style={styles.logoText}>{'</>'}</Text>
            </LinearGradient>
            <Text style={styles.title}>kube-coder</Text>
            <Text style={styles.subtitle}>Connect to your workspace</Text>
          </View>

          <View style={styles.form}>
            <Label>Workspace host</Label>
            <TextInput
              value={host}
              onChangeText={setHost}
              placeholder="https://you.kube-coder.example.com"
              placeholderTextColor={colors.textFaint}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              style={styles.input}
            />

            <Label style={{ marginTop: space.lg }}>API token</Label>
            <TextInput
              value={token}
              onChangeText={setToken}
              placeholder="Paste your Bearer token"
              placeholderTextColor={colors.textFaint}
              autoCapitalize="none"
              autoCorrect={false}
              secureTextEntry
              style={styles.input}
            />

            {error ? <Text style={styles.error}>{error}</Text> : null}

            <Button title="Connect" onPress={connect} loading={busy} style={{ marginTop: space.xl }} />

            <Text style={styles.hint}>
              Open your dashboard in a browser, sign in, and copy the API token from
              the Settings tab. Your token is stored securely on this device.
            </Text>
          </View>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  scroll: { flexGrow: 1, justifyContent: 'center', padding: space.xl },
  logoWrap: { alignItems: 'center', marginBottom: space.xxl },
  logo: {
    width: 76,
    height: 76,
    borderRadius: radius.xl,
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: space.lg,
  },
  logoText: { color: colors.accentText, fontSize: 28, fontWeight: '900' },
  title: { color: colors.text, fontSize: font.size.xxl, fontWeight: '800' },
  subtitle: { color: colors.textMuted, fontSize: font.size.md, marginTop: space.xs },
  form: { gap: space.xs },
  input: {
    backgroundColor: colors.bgElevated,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    color: colors.text,
    fontSize: font.size.md,
    paddingHorizontal: space.lg,
    height: 50,
  },
  error: { color: colors.danger, fontSize: font.size.sm, marginTop: space.md },
  hint: { color: colors.textFaint, fontSize: font.size.sm, marginTop: space.xl, lineHeight: 19 },
});
