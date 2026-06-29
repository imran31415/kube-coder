/** Settings: show connection, disconnect. */
import React from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Card, Label, ScreenHeader } from '../components/ui';
import { clearConnection } from '../store/config';
import { useConfig } from '../store/useConfig';
import { colors, font, space } from '../theme';
import { confirmAction } from '../util/confirm';

export default function SettingsScreen() {
  const cfg = useConfig();
  const masked = cfg.token ? cfg.token.slice(0, 4) + '••••••••' + cfg.token.slice(-2) : '';

  function disconnect() {
    confirmAction({
      title: 'Disconnect this workspace?',
      message: 'Your saved host and API token will be removed from this device.',
      confirmLabel: 'Disconnect',
      destructive: true,
      onConfirm: clearConnection,
    });
  }

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Settings" subtitle="Connection & about" />
      <ScrollView contentContainerStyle={styles.list} showsVerticalScrollIndicator={false}>
        <Card style={{ gap: space.md }}>
          <View>
            <Label>Workspace host</Label>
            <Text style={styles.value}>{cfg.host || '—'}</Text>
          </View>
          <View>
            <Label>API token</Label>
            <Text style={[styles.value, { fontFamily: font.mono }]}>{masked || '—'}</Text>
          </View>
          {cfg.mock ? (
            <View style={styles.demoBadge}>
              <Text style={styles.demoText}>DEMO MODE — showing mock data</Text>
            </View>
          ) : null}
        </Card>

        {!cfg.mock ? (
          <Button
            title="Disconnect"
            icon="log-out-outline"
            variant="danger"
            onPress={disconnect}
            style={{ marginTop: space.sm }}
          />
        ) : null}

        <Text style={styles.about}>
          kube-coder mobile · drive your workspace from anywhere. Tasks, memory and
          metrics talk to your workspace over the Bearer-token API.
        </Text>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.bg },
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.md },
  value: { color: colors.text, fontSize: font.size.md },
  demoBadge: {
    backgroundColor: colors.warning + '22',
    borderRadius: 8,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    alignSelf: 'flex-start',
  },
  demoText: { color: colors.warning, fontSize: font.size.xs, fontWeight: '700', letterSpacing: 0.5 },
  about: { color: colors.textFaint, fontSize: font.size.sm, lineHeight: 20, marginTop: space.lg },
});
