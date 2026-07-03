/** Bottom-sheet picker for which workspace app to show in the split pane:
 *  running apps from /api/apps, or any port typed by hand. */
import { Ionicons } from '@expo/vector-icons';
import React, { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { listApps } from '../api/client';
import type { AppEntry } from '../api/types';
import { colors, font, radius, space } from '../theme';

export function AppPickerSheet({
  visible,
  onPick,
  onClose,
}: {
  visible: boolean;
  onPick: (port: number, name: string) => void;
  onClose: () => void;
}) {
  const [apps, setApps] = useState<AppEntry[] | null>(null);
  const [customPort, setCustomPort] = useState('');

  useEffect(() => {
    if (!visible) return;
    setApps(null);
    listApps()
      .then((list) => setApps(list.filter((a) => a.status === 'running')))
      .catch(() => setApps([]));
  }, [visible]);

  const custom = parseInt(customPort, 10);
  const customValid = Number.isInteger(custom) && custom > 0 && custom < 65536;

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <Pressable style={styles.backdrop} onPress={onClose}>
        {/* Stop backdrop-press from closing when tapping the sheet itself. */}
        <Pressable style={styles.sheet} onPress={() => undefined}>
          <View style={styles.grabber} />
          <Text style={styles.title}>Show an app alongside</Text>
          <Text style={styles.subtitle}>Pick a running app, or enter a port.</Text>

          {apps === null ? (
            <View style={styles.loading}>
              <ActivityIndicator color={colors.accent} />
            </View>
          ) : apps.length === 0 ? (
            <Text style={styles.empty}>No running apps found — enter a port below.</Text>
          ) : (
            apps.map((a) => (
              <Pressable
                key={a.port}
                style={styles.row}
                onPress={() => onPick(a.port, a.name || `Port ${a.port}`)}
              >
                <View style={styles.rowIcon}>
                  <Ionicons name="globe-outline" size={16} color={colors.accent} />
                </View>
                <Text style={styles.rowName}>{a.name || `Port ${a.port}`}</Text>
                <Text style={styles.rowPort}>:{a.port}</Text>
              </Pressable>
            ))
          )}

          <View style={styles.customRow}>
            <TextInput
              value={customPort}
              onChangeText={setCustomPort}
              placeholder="Custom port (e.g. 3000)"
              placeholderTextColor={colors.textFaint}
              keyboardType="number-pad"
              style={styles.customInput}
            />
            <Pressable
              disabled={!customValid}
              onPress={() => customValid && onPick(custom, `Port ${custom}`)}
              style={[styles.customBtn, !customValid && { opacity: 0.4 }]}
            >
              <Text style={styles.customBtnText}>Show</Text>
            </Pressable>
          </View>
        </Pressable>
      </Pressable>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: '#000a', justifyContent: 'flex-end' },
  sheet: {
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.xl,
    paddingBottom: space.xxl,
    gap: space.sm,
  },
  grabber: {
    alignSelf: 'center',
    width: 36,
    height: 4,
    borderRadius: 2,
    backgroundColor: colors.borderStrong,
    marginBottom: space.md,
  },
  title: { color: colors.text, fontSize: font.size.lg, fontWeight: '800' },
  subtitle: { color: colors.textMuted, fontSize: font.size.sm, marginBottom: space.sm },
  loading: { paddingVertical: space.xl, alignItems: 'center' },
  empty: { color: colors.textFaint, fontSize: font.size.sm, paddingVertical: space.md },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingVertical: space.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  rowIcon: {
    width: 30,
    height: 30,
    borderRadius: radius.sm,
    backgroundColor: colors.accent + '1a',
    alignItems: 'center',
    justifyContent: 'center',
  },
  rowName: { flex: 1, color: colors.text, fontSize: font.size.md, fontWeight: '600' },
  rowPort: { color: colors.textFaint, fontSize: font.size.sm, fontFamily: font.mono },
  customRow: { flexDirection: 'row', gap: space.sm, marginTop: space.md },
  customInput: {
    flex: 1,
    height: 44,
    backgroundColor: colors.card,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.md,
    color: colors.text,
    paddingHorizontal: space.md,
    fontSize: font.size.md,
  },
  customBtn: {
    height: 44,
    paddingHorizontal: space.lg,
    borderRadius: radius.md,
    backgroundColor: colors.accent,
    alignItems: 'center',
    justifyContent: 'center',
  },
  customBtnText: { color: colors.accentText, fontWeight: '700', fontSize: font.size.md },
});
