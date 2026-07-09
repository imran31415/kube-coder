/** Modal to add an admin controller connection: host + admin token, validated
 *  against the controller before it sticks. Mirrors the onboarding connect flow
 *  (save → probe → roll back on failure) but for the second, controller
 *  connection. The token is revealed from the controller web console's "Mobile
 *  access" card. */
import { Ionicons } from '@expo/vector-icons';
import React, { useState } from 'react';
import {
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { pingController } from '../api/controller';
import { getConfig, saveControllerConnection } from '../store/config';
import { colors, font, radius, space } from '../theme';
import { Button, Label } from './ui';

export function ControllerConnectModal({ visible, onClose }: { visible: boolean; onClose: () => void }) {
  const [host, setHost] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function connect() {
    setError(null);
    if (!/^https?:\/\//.test(host.trim())) {
      setError('Host must start with http:// or https://');
      return;
    }
    if (!token.trim()) {
      setError('Paste the controller admin token');
      return;
    }
    setBusy(true);
    // Save first so the client can read controllerHost/Token, then probe.
    const prev = { host: getConfig().controllerHost, token: getConfig().controllerToken };
    await saveControllerConnection(host, token);
    try {
      await pingController();
      setHost('');
      setToken('');
      onClose();
    } catch (e) {
      // Roll back so a bad token doesn't leave a broken connection behind.
      await saveControllerConnection(prev.host, prev.token);
      setError(`Couldn't connect: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal visible={visible} animationType="slide" transparent onRequestClose={onClose}>
      <View style={styles.backdrop}>
        <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
          <View style={styles.sheet}>
            <View style={styles.head}>
              <Text style={styles.title}>Add controller</Text>
              <Pressable onPress={onClose} hitSlop={8} accessibilityLabel="Close">
                <Ionicons name="close" size={22} color={colors.textMuted} />
              </Pressable>
            </View>
            <Text style={styles.help}>
              Reveal these on the controller web console's “Mobile access” card, then paste them here.
            </Text>

            <Label style={{ marginTop: space.md }}>Controller host</Label>
            <TextInput
              value={host}
              onChangeText={setHost}
              placeholder="https://controller.kube-coder.example.com"
              placeholderTextColor={colors.textFaint}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              style={styles.input}
            />

            <Label style={{ marginTop: space.md }}>Controller token</Label>
            <TextInput
              value={token}
              onChangeText={setToken}
              placeholder="Paste the admin token"
              placeholderTextColor={colors.textFaint}
              autoCapitalize="none"
              autoCorrect={false}
              secureTextEntry
              style={styles.input}
            />

            {error ? <Text style={styles.error}>{error}</Text> : null}

            <Button title="Connect controller" onPress={connect} loading={busy} style={{ marginTop: space.lg }} />
          </View>
        </KeyboardAvoidingView>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: '#000a', justifyContent: 'flex-end' },
  sheet: {
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    padding: space.xl,
    paddingBottom: space.xxl,
    gap: 2,
  },
  head: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  title: { color: colors.text, fontSize: font.size.lg, fontWeight: '800' },
  help: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19, marginTop: space.xs },
  input: {
    marginTop: space.xs,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    color: colors.text,
    fontSize: font.size.md,
  },
  error: { color: colors.danger, fontSize: font.size.sm, marginTop: space.md },
});
