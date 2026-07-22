/** Settings: show connection, disconnect. */
import React, { useState } from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Button, Card, Label, ScreenHeader } from '../components/ui';
import { ControllerConnectModal } from '../components/ControllerConnectModal';
import { GitIdentityCard } from '../components/GitIdentityCard';
import { McpServersCard } from '../components/McpServersCard';
import { ProviderKeysCard } from '../components/ProviderKeysCard';
import { MessagingCard } from '../components/MessagingCard';
import { UpdatesCard } from '../components/UpdatesCard';
import { clearConnection, clearControllerConnection, hasController, isDemoHost } from '../store/config';
import { useConfig } from '../store/useConfig';
import { colors, font, space } from '../theme';
import { confirmAction } from '../util/confirm';

function mask(t: string): string {
  return t ? t.slice(0, 4) + '••••••••' + t.slice(-2) : '';
}

export default function SettingsScreen() {
  const cfg = useConfig();
  const masked = mask(cfg.token);
  const isDemo = isDemoHost(cfg.host);
  const [ctrlModal, setCtrlModal] = useState(false);
  const controllerOn = hasController(cfg);

  function disconnect() {
    confirmAction({
      title: isDemo ? 'Leave the public demo?' : 'Disconnect this workspace?',
      message: isDemo
        ? "You'll return to the connect screen, where you can enter your own workspace host and token."
        : 'Your saved host and API token will be removed from this device.',
      confirmLabel: isDemo ? 'Leave demo' : 'Disconnect',
      destructive: true,
      onConfirm: clearConnection,
    });
  }

  function disconnectController() {
    confirmAction({
      title: 'Disconnect controller?',
      message: 'The saved controller host and admin token will be removed from this device.',
      confirmLabel: 'Disconnect',
      destructive: true,
      onConfirm: clearControllerConnection,
    });
  }

  return (
    <SafeAreaView style={styles.safe} edges={['top']}>
      <ScreenHeader title="Settings" subtitle="Connection, identity & updates" />
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
          {cfg.mock || isDemo ? (
            <View style={styles.demoBadge}>
              <Text style={styles.demoText}>
                {cfg.mock ? 'DEMO MODE — showing mock data' : 'PUBLIC DEMO — read-only'}
              </Text>
            </View>
          ) : null}
        </Card>

        {!cfg.mock ? (
          <Button
            title={isDemo ? 'Leave demo' : 'Disconnect'}
            icon="log-out-outline"
            variant="danger"
            onPress={disconnect}
            style={{ marginTop: space.sm }}
          />
        ) : null}

        {!cfg.mock ? <ProviderKeysCard readOnly={isDemo} /> : null}

        {!cfg.mock ? <McpServersCard readOnly={isDemo} /> : null}

        {!cfg.mock ? <MessagingCard readOnly={isDemo} /> : null}

        {/* Identity + self-serve updates. Shown read-only on the public demo and
            in the mock build (where they render canned data), interactive on a
            real workspace connection. */}
        <GitIdentityCard readOnly={isDemo || cfg.mock} />

        <UpdatesCard readOnly={isDemo || cfg.mock} />

        {/* Admin controller — a second, optional connection. */}
        <Card style={{ gap: space.md, marginTop: space.lg }}>
          <View style={styles.ctrlHead}>
            <Label>Controller (admin)</Label>
            <Text style={[styles.ctrlState, { color: controllerOn ? colors.success : colors.textFaint }]}>
              {controllerOn ? 'Connected' : 'Not connected'}
            </Text>
          </View>
          {controllerOn ? (
            <>
              <View>
                <Label>Controller host</Label>
                <Text style={styles.value}>{cfg.controllerHost || (cfg.mock ? 'demo controller' : '—')}</Text>
              </View>
              <View>
                <Label>Admin token</Label>
                <Text style={[styles.value, { fontFamily: font.mono }]}>
                  {mask(cfg.controllerToken) || (cfg.mock ? 'demo' : '—')}
                </Text>
              </View>
            </>
          ) : (
            <Text style={styles.ctrlHelp}>
              Manage all workspaces (list, start/stop, capacity) from a controller. Reveal a host + admin token on
              the controller web console's “Mobile access” card.
            </Text>
          )}
          {!cfg.mock ? (
            controllerOn ? (
              <Button title="Disconnect controller" variant="secondary" icon="log-out-outline" onPress={disconnectController} />
            ) : (
              <Button title="Add controller" icon="server-outline" onPress={() => setCtrlModal(true)} />
            )
          ) : null}
        </Card>

        <Text style={styles.about}>
          kube-coder mobile · drive your workspace from anywhere. Tasks, memory and
          metrics talk to your workspace over the Bearer-token API.
        </Text>
      </ScrollView>
      <ControllerConnectModal visible={ctrlModal} onClose={() => setCtrlModal(false)} />
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
  ctrlHead: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between' },
  ctrlState: { fontSize: font.size.xs, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.5 },
  ctrlHelp: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19 },
  about: { color: colors.textFaint, fontSize: font.size.sm, lineHeight: 20, marginTop: space.lg },
});
