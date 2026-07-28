/** Slide-out navigation drawer that replaced the bottom tab bar. Opened by the
 *  ☰ button in every top-level screen header; lists all destinations and jumps
 *  to them. Built on core RN Modal + Animated (no extra native deps) so it
 *  works the same on device and the web export. */
import { Ionicons } from '@expo/vector-icons';
import { LinearGradient } from 'expo-linear-gradient';
import React, { useEffect, useRef } from 'react';
import { Animated, Dimensions, Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { closeDrawer, navigateTo, useActiveTab, useDrawerOpen } from '../store/nav';
import { hasController } from '../store/config';
import { useConfig } from '../store/useConfig';
import { NAV_GROUPS } from '../navGroups';
import { colors, font, gradients, radius, space } from '../theme';

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

  // Same categorized IA as the web dashboard (#267); a group whose items are
  // all filtered out (Controller unconfigured) drops entirely.
  const groups = NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((i) => !i.controller || hasController(cfg)),
  })).filter((g) => g.items.length > 0);
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

          {/* Scrollable: the destination list outgrew a short phone screen
              once Triggers + Docs landed (#250), and a plain View silently
              clipped the tail (Docs / Controller / Settings) off the bottom. */}
          <ScrollView
            style={styles.items}
            contentContainerStyle={styles.itemsInner}
            showsVerticalScrollIndicator={false}
          >
            {groups.map((g, gi) => (
              <View key={g.title ?? 'tail'} style={[styles.group, gi > 0 && styles.groupGap]}>
                {g.title ? (
                  <Text style={styles.groupTitle}>{g.title}</Text>
                ) : (
                  <View style={styles.groupDivider} />
                )}
                {g.items.map((it) => {
                  const on = it.name === active;
                  const icon = (on ? it.icon.replace('-outline', '') : it.icon) as keyof typeof Ionicons.glyphMap;
                  return (
                    <Pressable
                      key={it.name}
                      onPress={() => navigateTo(it.name)}
                      accessibilityRole="button"
                      // Distinct from the label text so screen readers (and the
                      // nav guard) can target the drawer entry unambiguously,
                      // even when a screen underneath renders the same word.
                      accessibilityLabel={`Go to ${it.label}`}
                      accessibilityState={{ selected: on }}
                      style={({ pressed }) => [styles.item, on && styles.itemActive, pressed && styles.itemPressed]}
                    >
                      <Ionicons name={icon} size={20} color={on ? colors.accent : colors.textMuted} />
                      <Text style={[styles.itemLabel, on && styles.itemLabelActive]}>{it.label}</Text>
                    </Pressable>
                  );
                })}
              </View>
            ))}
          </ScrollView>

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

  items: { marginTop: space.md, flex: 1 },
  itemsInner: { paddingBottom: space.md },
  group: { gap: 2 },
  groupGap: { marginTop: space.md },
  groupTitle: {
    color: colors.textFaint,
    fontSize: font.size.xs,
    fontWeight: '700',
    letterSpacing: 0.8,
    textTransform: 'uppercase',
    paddingHorizontal: space.md,
    paddingBottom: space.xs,
  },
  groupDivider: { height: 1, backgroundColor: colors.border, marginBottom: space.sm, marginHorizontal: space.sm },
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
