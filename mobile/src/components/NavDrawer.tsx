/** Slide-out navigation drawer that replaced the bottom tab bar. Opened by the
 *  ☰ button in every top-level screen header; lists all destinations and jumps
 *  to them. Built on core RN Modal + Animated (no extra native deps) so it
 *  works the same on device and the web export. */
import { Ionicons } from '@expo/vector-icons';
import { LinearGradient } from 'expo-linear-gradient';
import React, { useEffect, useRef } from 'react';
import { Animated, Dimensions, Modal, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { closeDrawer, navigateTo, useActiveTab, useDrawerOpen } from '../store/nav';
import { hasController } from '../store/config';
import { useConfig } from '../store/useConfig';
import { colors, font, gradients, radius, space } from '../theme';

type Item = { name: string; label: string; icon: keyof typeof Ionicons.glyphMap; controller?: boolean };

const ITEMS: Item[] = [
  { name: 'Desktop', label: 'Desktop', icon: 'grid-outline' },
  { name: 'Tasks', label: 'Builds', icon: 'layers-outline' },
  { name: 'Apps', label: 'Apps', icon: 'globe-outline' },
  { name: 'Memory', label: 'Memory', icon: 'bookmark-outline' },
  { name: 'Metrics', label: 'Metrics', icon: 'stats-chart-outline' },
  { name: 'Controller', label: 'Controller', icon: 'server-outline', controller: true },
  { name: 'Settings', label: 'Settings', icon: 'settings-outline' },
];

const WIDTH = Math.min(320, Math.round(Dimensions.get('window').width * 0.82));

export function NavDrawer() {
  const open = useDrawerOpen();
  const active = useActiveTab();
  const cfg = useConfig();
  const insets = useSafeAreaInsets();
  // Keep the Modal mounted through the close animation, then unmount.
  const [mounted, setMounted] = React.useState(open);
  const slide = useRef(new Animated.Value(0)).current; // 0 = hidden, 1 = shown

  useEffect(() => {
    if (open) setMounted(true);
    Animated.timing(slide, {
      toValue: open ? 1 : 0,
      duration: open ? 220 : 180,
      useNativeDriver: true,
    }).start(({ finished }) => {
      if (finished && !open) setMounted(false);
    });
  }, [open, slide]);

  if (!mounted) return null;

  const items = ITEMS.filter((i) => !i.controller || hasController(cfg));
  const host = (cfg.controllerHost || cfg.host || '').replace(/^https?:\/\//, '');

  const translateX = slide.interpolate({ inputRange: [0, 1], outputRange: [-WIDTH, 0] });
  const backdropOpacity = slide.interpolate({ inputRange: [0, 1], outputRange: [0, 1] });

  return (
    <Modal visible transparent animationType="none" onRequestClose={closeDrawer} statusBarTranslucent>
      <View style={styles.root}>
        <Animated.View style={[styles.backdrop, { opacity: backdropOpacity }]}>
          <Pressable style={StyleSheet.absoluteFill} onPress={closeDrawer} accessibilityLabel="Close menu" />
        </Animated.View>

        <Animated.View style={[styles.panel, { width: WIDTH, transform: [{ translateX }], paddingTop: insets.top + space.lg }]}>
          <View style={styles.brandRow}>
            <LinearGradient colors={gradients.brand} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={styles.brandMark}>
              <Text style={styles.brandGlyph}>{'</>'}</Text>
            </LinearGradient>
            <View style={{ flexShrink: 1 }}>
              <Text style={styles.brandName}>kube-coder</Text>
              {host ? (
                <Text style={styles.brandHost} numberOfLines={1}>
                  {host}
                </Text>
              ) : null}
            </View>
          </View>

          <View style={styles.items}>
            {items.map((it) => {
              const on = it.name === active;
              return (
                <Pressable
                  key={it.name}
                  onPress={() => navigateTo(it.name)}
                  accessibilityRole="button"
                  accessibilityState={{ selected: on }}
                  style={({ pressed }) => [styles.item, on && styles.itemActive, pressed && styles.itemPressed]}
                >
                  <Ionicons
                    name={on ? (it.icon.replace('-outline', '') as keyof typeof Ionicons.glyphMap) : it.icon}
                    size={20}
                    color={on ? colors.accent : colors.textMuted}
                  />
                  <Text style={[styles.itemLabel, on && styles.itemLabelActive]}>{it.label}</Text>
                </Pressable>
              );
            })}
          </View>

          <Text style={[styles.footer, { paddingBottom: insets.bottom + space.md }]}>kube-coder mobile</Text>
        </Animated.View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  backdrop: { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: 'rgba(0,0,0,0.55)' },
  panel: {
    position: 'absolute',
    top: 0,
    bottom: 0,
    left: 0,
    backgroundColor: colors.bgElevated,
    borderRightWidth: 1,
    borderRightColor: colors.border,
    paddingHorizontal: space.md,
  },
  brandRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.sm,
    paddingBottom: space.lg,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
  },
  brandMark: { width: 36, height: 36, borderRadius: radius.sm, alignItems: 'center', justifyContent: 'center' },
  brandGlyph: { color: colors.accentText, fontSize: 14, fontWeight: '800', fontFamily: font.mono },
  brandName: { color: colors.text, fontSize: font.size.lg, fontWeight: '700', letterSpacing: -0.2 },
  brandHost: { color: colors.textFaint, fontSize: font.size.xs, marginTop: 1 },

  items: { marginTop: space.md, gap: 2, flex: 1 },
  item: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingVertical: space.md,
    paddingHorizontal: space.md,
    borderRadius: radius.md,
  },
  itemActive: { backgroundColor: colors.accent + '1a' },
  itemPressed: { backgroundColor: colors.cardHover },
  itemLabel: { color: colors.textMuted, fontSize: font.size.md, fontWeight: '600' },
  itemLabelActive: { color: colors.text },

  footer: { color: colors.textFaint, fontSize: font.size.xs, paddingHorizontal: space.md, paddingTop: space.md },
});
