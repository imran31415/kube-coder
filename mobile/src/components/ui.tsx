/** Small shared UI primitives, styled with the app theme. */
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
import { colors, font, radius, space, statusColor } from '../theme';
import { statusLabel } from '../util/format';

export function Card({
  children,
  style,
  onPress,
}: {
  children: React.ReactNode;
  style?: ViewStyle;
  onPress?: () => void;
}) {
  const content = <View style={[styles.card, style]}>{children}</View>;
  if (!onPress) return content;
  return (
    <Pressable onPress={onPress} style={({ pressed }) => (pressed ? styles.pressed : undefined)}>
      {content}
    </Pressable>
  );
}

export function StatusPill({ status }: { status: string }) {
  const c = statusColor(status);
  return (
    <View style={[styles.pill, { backgroundColor: c + '22', borderColor: c + '55' }]}>
      {status === 'running' ? (
        <ActivityIndicator size="small" color={c} style={{ marginRight: 6, transform: [{ scale: 0.7 }] }} />
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
  style,
}: {
  title: string;
  onPress: () => void;
  variant?: 'primary' | 'secondary' | 'danger';
  disabled?: boolean;
  loading?: boolean;
  style?: ViewStyle;
}) {
  const bg =
    variant === 'primary' ? colors.accent : variant === 'danger' ? colors.danger : 'transparent';
  const fg = variant === 'secondary' ? colors.text : colors.accentText;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || loading}
      style={({ pressed }) => [
        styles.btn,
        { backgroundColor: bg, opacity: disabled ? 0.5 : pressed ? 0.85 : 1 },
        variant === 'secondary' && styles.btnSecondary,
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator color={fg} />
      ) : (
        <Text style={[styles.btnText, { color: fg }]}>{title}</Text>
      )}
    </Pressable>
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

export function EmptyState({ title, subtitle }: { title: string; subtitle?: string }) {
  return (
    <View style={styles.center}>
      <Text style={styles.emptyTitle}>{title}</Text>
      {subtitle ? <Text style={[styles.muted, { textAlign: 'center' }]}>{subtitle}</Text> : null}
    </View>
  );
}

export function Label({ children, style }: { children: React.ReactNode; style?: TextStyle }) {
  return <Text style={[styles.label, style]}>{children}</Text>;
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: colors.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: colors.border,
    padding: space.lg,
  },
  pressed: { opacity: 0.7 },
  pill: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'flex-start',
    borderRadius: radius.pill,
    borderWidth: 1,
    paddingVertical: 3,
    paddingHorizontal: 10,
  },
  dot: { width: 7, height: 7, borderRadius: 4, marginRight: 6 },
  pillText: { fontSize: font.size.xs, fontWeight: '600', textTransform: 'uppercase', letterSpacing: 0.5 },
  btn: {
    height: 48,
    borderRadius: radius.md,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: space.lg,
  },
  btnSecondary: { borderWidth: 1, borderColor: colors.borderStrong },
  btnText: { fontSize: font.size.md, fontWeight: '600' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: space.xl, gap: space.sm },
  emptyTitle: { color: colors.text, fontSize: font.size.lg, fontWeight: '600' },
  muted: { color: colors.textMuted, fontSize: font.size.sm },
  label: {
    color: colors.textMuted,
    fontSize: font.size.xs,
    fontWeight: '600',
    textTransform: 'uppercase',
    letterSpacing: 0.6,
    marginBottom: space.xs,
  },
});
