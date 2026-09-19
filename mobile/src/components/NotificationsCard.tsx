/** Settings card: push notifications on/off for this phone (#685).
 *
 *  Before this the only way to stop pushes from the phone was to disconnect the
 *  workspace. Off drops this device's registration from the workspace; on
 *  registers it again. The choice is saved on the phone and survives restarts,
 *  and the in-app Feed keeps every alert either way.
 *
 *  Hidden on the read-only public demo, where registration is refused anyway. */
import React, { useState } from 'react';
import { Pressable, StyleSheet, Switch, Text, View } from 'react-native';
import { setPushNotificationsEnabled } from '../push/notifications';
import { useConfig } from '../store/useConfig';
import { colors, font, space } from '../theme';
import { Card, Label } from './ui';

export function NotificationsCard() {
  const cfg = useConfig();
  const [busy, setBusy] = useState(false);
  const on = cfg.pushEnabled;

  async function toggle() {
    if (busy) return;
    setBusy(true);
    try {
      await setPushNotificationsEnabled(!on);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card style={{ gap: space.sm }}>
      <Label>Notifications</Label>
      <Pressable
        style={styles.row}
        onPress={() => void toggle()}
        disabled={busy}
        accessibilityRole="switch"
        accessibilityLabel="Push notifications"
        accessibilityState={{ checked: on, disabled: busy }}
      >
        <View style={styles.text}>
          <Text style={styles.title}>Push notifications</Text>
          <Text style={styles.hint}>
            {on
              ? 'Alerts when an agent is waiting on you or a decision is recorded. A repeat of the same alert comes back only after you have read it.'
              : 'Off for this phone. The Feed still shows every alert.'}
          </Text>
        </View>
        <Switch
          value={on}
          onValueChange={() => void toggle()}
          disabled={busy}
          trackColor={{ true: colors.accent, false: colors.surface3 }}
          thumbColor={colors.text}
        />
      </Pressable>
    </Card>
  );
}

const styles = StyleSheet.create({
  // 44pt minimum touch target (Apple HIG), same bar as the #692 fixes.
  row: { flexDirection: 'row', alignItems: 'center', gap: space.md, minHeight: 44 },
  text: { flex: 1 },
  title: { color: colors.text, fontSize: font.size.md, fontWeight: '500' },
  hint: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 19, marginTop: 2 },
});
