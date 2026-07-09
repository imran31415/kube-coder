/** Shared UI primitives, styled with the app theme. */
import { Ionicons } from '@expo/vector-icons';
import { LinearGradient } from 'expo-linear-gradient';
import React from 'react';
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextStyle,
  View,
  ViewStyle,
} from 'react-native';
import { colors, font, gradients, radius, space, statusColor } from '../theme';
import { statusLabel } from '../util/format';

export function Card({
  children,
  style,
  onPress,
  accent,
}: {
  children: React.ReactNode;
  style?: ViewStyle;
  onPress?: () => void;
  accent?: string; // optional left accent bar (e.g. for running tasks)
}) {
  const inner = (
    <View style={[styles.card, accent ? { borderLeftWidth: 2, borderLeftColor: accent } : null, style]}>
      {children}
    </View>
  );
  if (!onPress) return inner;
  return (
    <Pressable onPress={onPress} style={({ pressed }) => (pressed ? styles.pressed : undefined)}>
      {inner}
    </Pressable>
  );
}

export function StatusPill({ status }: { status: string }) {
  const c = statusColor(status);
  return (
    <View style={[styles.pill, { backgroundColor: colors.surface2, borderColor: c + '59' }]}>
      {status === 'running' ? (
        <ActivityIndicator size="small" color={c} style={styles.spinner} />
      ) : (
        <View style={[styles.dot, { backgroundColor: c }]} />
      )}
      <Text style={[styles.pillText, { color: c }]}>{statusLabel(status)}</Text>
    </View>
  );
}

export function Button({
  title,
  onPress,
  variant = 'primary',
  disabled,
  loading,
  icon,
  style,
}: {
  title: string;
  onPress: () => void;
  variant?: 'primary' | 'secondary' | 'danger';
  disabled?: boolean;
  loading?: boolean;
  icon?: keyof typeof Ionicons.glyphMap;
  style?: ViewStyle;
}) {
  const fg =
    variant === 'primary' ? colors.accentText : variant === 'danger' ? colors.danger : colors.text;
  const body = loading ? (
    <ActivityIndicator color={fg} />
  ) : (
    <View style={styles.btnRow}>
      {icon ? <Ionicons name={icon} size={18} color={fg} /> : null}
      <Text style={[styles.btnText, { color: fg }]}>{title}</Text>
    </View>
  );

  if (variant === 'primary') {
    return (
      <Pressable
        onPress={onPress}
        disabled={disabled || loading}
        style={({ pressed }) => [{ opacity: disabled ? 0.45 : pressed ? 0.9 : 1, borderRadius: radius.md }, style]}
      >
        <LinearGradient
          colors={gradients.primary}
          start={{ x: 0, y: 0 }}
          end={{ x: 1, y: 1 }}
          style={styles.btn}
        >
          {body}
        </LinearGradient>
      </Pressable>
    );
  }

  const isDanger = variant === 'danger';
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || loading}
      style={({ pressed }) => [
        styles.btn,
        styles.btnFlat,
        {
          backgroundColor: isDanger ? colors.danger + '14' : colors.surface2,
          borderColor: isDanger ? colors.danger + '55' : colors.border,
          opacity: disabled ? 0.45 : pressed ? 0.9 : 1,
        },
        style,
      ]}
    >
      {body}
    </Pressable>
  );
}

/** Consistent screen header with brand mark, title, optional subtitle + action. */
export function ScreenHeader({
  title,
  subtitle,
  right,
  brand,
}: {
  title: string;
  subtitle?: string;
  right?: React.ReactNode;
  brand?: boolean;
}) {
  return (
    <View style={styles.header}>
      <View style={styles.headerLeft}>
        {brand ? (
          <LinearGradient colors={gradients.brand} start={{ x: 0, y: 0 }} end={{ x: 1, y: 1 }} style={styles.brandMark}>
            <Text style={styles.brandGlyph}>{'</>'}</Text>
          </LinearGradient>
        ) : null}
        <View style={{ flexShrink: 1 }}>
          <Text style={styles.headerTitle}>{title}</Text>
          {subtitle ? (
            <Text style={styles.headerSubtitle} numberOfLines={1}>
              {subtitle}
            </Text>
          ) : null}
        </View>
      </View>
      {right}
    </View>
  );
}

export function Loading({ label }: { label?: string }) {
  return (
    <View style={styles.center}>
      <ActivityIndicator color={colors.accent} size="large" />
      {label ? <Text style={styles.muted}>{label}</Text> : null}
    </View>
  );
}

export function EmptyState({
  title,
  subtitle,
  icon = 'sparkles-outline',
}: {
  title: string;
  subtitle?: string;
  icon?: keyof typeof Ionicons.glyphMap;
}) {
  return (
    <View style={styles.center}>
      <View style={styles.emptyIcon}>
        <Ionicons name={icon} size={26} color={colors.textMuted} />
      </View>
      <Text style={styles.emptyTitle}>{title}</Text>
      {subtitle ? <Text style={[styles.muted, { textAlign: 'center', maxWidth: 280 }]}>{subtitle}</Text> : null}
    </View>
  );
}

export function Label({ children, style }: { children: React.ReactNode; style?: TextStyle }) {
  return <Text style={[styles.label, style]}>{children}</Text>;
}

/** Inline, non-blocking error strip. Shown when a screen still has (stale)
 *  data to display — a full-screen error state would throw that away. */
export function ErrorBanner({ message }: { message: string }) {
  return (
    <View style={styles.errBanner} accessibilityRole="alert">
      <Ionicons name="cloud-offline-outline" size={15} color={colors.danger} />
      <Text style={styles.errBannerText} numberOfLines={2}>
        {message}
      </Text>
    </View>
  );
}

export { Ionicons };

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.lg,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.lg,
  },
  pressed: { opacity: 0.85, transform: [{ scale: 0.997 }] },
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'flex-start',
    borderRadius: radius.pill,
    borderWidth: 1,
    paddingVertical: 4,
    paddingHorizontal: 10,
  },
  spinner: { marginRight: 6, transform: [{ scale: 0.7 }] },
  dot: { width: 7, height: 7, borderRadius: 4, marginRight: 6 },
  pillText: { fontSize: font.size.xs, fontWeight: '600', textTransform: 'uppercase', letterSpacing: 0.4 },
  btn: {
    height: 46,
    borderRadius: radius.md,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: space.lg,
  },
  btnRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  btnFlat: { borderWidth: 1 },
  btnText: { fontSize: font.size.md, fontWeight: '600' },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: space.lg,
    paddingTop: space.md,
    paddingBottom: space.md,
    gap: space.md,
  },
  headerLeft: { flexDirection: 'row', alignItems: 'center', gap: space.md, flexShrink: 1 },
  brandMark: { width: 32, height: 32, borderRadius: radius.sm, alignItems: 'center', justifyContent: 'center' },
  brandGlyph: { color: colors.accentText, fontSize: 13, fontWeight: '800', fontFamily: font.mono },
  headerTitle: { color: colors.text, fontSize: font.size.xl, fontWeight: '700', letterSpacing: -0.3 },
  headerSubtitle: { color: colors.textMuted, fontSize: font.size.sm, marginTop: 1 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: space.xl, gap: space.md },
  emptyIcon: {
    width: 56,
    height: 56,
    borderRadius: radius.lg,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderColor: colors.border,
    alignItems: 'center',
    justifyContent: 'center',
  },
  muted: { color: colors.textMuted, fontSize: font.size.sm, lineHeight: 20 },
  emptyTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '600' },
  label: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.7,
    marginBottom: space.sm,
  },
  errBanner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    marginHorizontal: space.lg,
    marginBottom: space.md,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.danger + '55',
    backgroundColor: colors.danger + '14',
  },
  errBannerText: { flex: 1, color: colors.danger, fontSize: font.size.sm },
});
