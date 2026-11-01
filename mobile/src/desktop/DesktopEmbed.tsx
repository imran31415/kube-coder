/**
 * A workspace app or terminal, in the desktop app (#833).
 *
 * The phone embeds these in a WebView inside the screen. The desktop app does
 * not: the bundled UI is the only content with IPC, so remote content never
 * shares its window. Instead the core mints the app session itself and opens
 * the app in a window of its own with no IPC (desktop/src-tauri/src/embed.rs),
 * and this panel stands in for it.
 */
import { Ionicons } from '@expo/vector-icons';
import React, { useCallback, useEffect, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { Button } from '../components/ui';
import { colors, font, radius, space } from '../theme';
import { invoke } from './bridge';
import { embedNext } from './embedPath';

export function DesktopEmbed({ uri }: { uri: string }) {
  const next = embedNext(uri);
  const [state, setState] = useState<'opening' | 'open' | 'error'>('opening');
  const [msg, setMsg] = useState('');
  const terminal = next?.startsWith('/api/terminal-proxy') ?? false;

  const open = useCallback(() => {
    if (!next) return;
    setState('opening');
    invoke('kc_open_embed', { next, title: terminal ? 'kube-coder terminal' : 'kube-coder app' })
      .then(() => setState('open'))
      .catch((e: unknown) => {
        setMsg(typeof e === 'string' ? e : (e as Error)?.message ?? 'Could not open it.');
        setState('error');
      });
  }, [next, terminal]);

  useEffect(open, [open]);

  if (!next) {
    return (
      <View style={styles.wrap}>
        <Text style={styles.title}>Not available in the desktop app yet</Text>
        <Text style={styles.msg}>Open this from the web dashboard.</Text>
      </View>
    );
  }
  return (
    <View style={styles.wrap}>
      <View style={styles.icon}>
        <Ionicons name={terminal ? 'terminal-outline' : 'open-outline'} size={26} color={colors.accent} />
      </View>
      <Text style={styles.title}>
        {state === 'error' ? "Couldn't open it" : `Opened in its own window`}
      </Text>
      <Text style={styles.msg}>
        {state === 'error'
          ? msg
          : 'Workspace content runs in a separate window, apart from the app itself.'}
      </Text>
      <Button title="Open again" icon="open-outline" onPress={open} style={{ marginTop: space.lg }} />
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: space.xl, backgroundColor: colors.bg },
  icon: {
    width: 56,
    height: 56,
    borderRadius: radius.xl,
    backgroundColor: colors.accent + '1a',
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: space.md,
  },
  title: { color: colors.text, fontSize: font.size.lg, fontWeight: '700' },
  msg: { color: colors.textMuted, fontSize: font.size.sm, textAlign: 'center', marginTop: space.sm, maxWidth: 340 },
});
