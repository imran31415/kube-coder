/**
 * A picker you can type into.
 *
 * Replaces the horizontally-scrolling chip rails. A rail shows maybe three of
 * N options and hides the rest off the right edge, with no way to jump and no
 * way to filter — so it degrades exactly as a workspace gets more useful, when
 * the options are folders, projects and models. On a phone it is also the
 * fiddliest possible target: a sideways drag inside a vertically-scrolling
 * screen.
 *
 * This is a field showing the current choice, opening a sheet with a search
 * box and a vertical list. Reads like every other picker on the platform.
 */
import { Ionicons } from '@expo/vector-icons';
import React, { useMemo, useState } from 'react';
import {
  FlatList,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  useWindowDimensions,
  View,
} from 'react-native';
import { colors, font, radius, space } from '../theme';
import { canUseCustom, filterOptions, type PickerOption } from '../util/pickerFilter';
import { sheetMaxHeight } from '../util/sheetSizing';
import { useKeyboardHeight } from '../util/useKeyboard';

export type { PickerOption };

export function SearchPicker({
  label,
  icon,
  value,
  options,
  onChange,
  disabled = false,
  emptyLabel,
  allowCustom = false,
  placeholder = 'Select…',
}: {
  /** Shown as the field's eyebrow and as the sheet's title. */
  label: string;
  icon?: keyof typeof Ionicons.glyphMap;
  value: string;
  options: PickerOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  /** Adds a row that clears the selection. */
  emptyLabel?: string;
  /** Let text that matches nothing be committed as typed. */
  allowCustom?: boolean;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  // The search field autofocuses, so the keyboard is up for the entire life
  // of this sheet. A Modal mounts into its own native view hierarchy, so the
  // screen-level KeyboardAvoidingView never applied here and the option list
  // sat under the keys (#662). Size against the space actually left.
  const { height: screenHeight } = useWindowDimensions();
  const keyboardHeight = useKeyboardHeight();
  const maxHeight = sheetMaxHeight(screenHeight, keyboardHeight);

  // Filtering lives in util/pickerFilter so it can be tested directly — this
  // app's suite is pure-logic, with no React Native render harness.
  const rows = useMemo(
    () => filterOptions(options, query, emptyLabel),
    [options, query, emptyLabel],
  );
  const offerCustom = canUseCustom(query, rows, allowCustom);

  const selected = options.find((o) => o.value === value);
  const shown = selected?.label ?? (value || emptyLabel || placeholder);

  function close() {
    setOpen(false);
    setQuery('');
  }

  function pick(next: string) {
    onChange(next);
    close();
  }

  return (
    <>
      <Pressable
        onPress={() => !disabled && setOpen(true)}
        disabled={disabled}
        accessibilityRole="button"
        accessibilityLabel={label}
        accessibilityValue={{ text: shown }}
        accessibilityState={{ disabled }}
        style={({ pressed }) => [
          styles.field,
          pressed && !disabled && styles.fieldPressed,
          disabled && styles.fieldDisabled,
        ]}
      >
        {icon ? <Ionicons name={icon} size={14} color={colors.textFaint} /> : null}
        <Text style={styles.eyebrow}>{label}</Text>
        <Text style={[styles.value, !selected && !value && styles.valueEmpty]} numberOfLines={1}>
          {shown}
        </Text>
        <Ionicons name="chevron-down" size={13} color={colors.textFaint} />
      </Pressable>

      <Modal visible={open} animationType="slide" transparent onRequestClose={close}>
        <Pressable style={styles.backdrop} onPress={close} accessibilityLabel="Close picker" />
        {/* KeyboardAvoidingView goes INSIDE the Modal — matching the sheets
            that already handle this (ControllerConnectModal, ProviderKeysCard,
            TriggersScreen…). It lifts the sheet; maxHeight is what stops the
            lifted sheet from running off the top instead. */}
        <KeyboardAvoidingView
          style={styles.sheetWrap}
          behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        >
        <View style={[styles.sheet, { maxHeight }]}>
          <View style={styles.sheetHead}>
            <Text style={styles.sheetTitle}>{label}</Text>
            <Pressable onPress={close} hitSlop={10} accessibilityRole="button" accessibilityLabel="Close">
              <Ionicons name="close" size={18} color={colors.textMuted} />
            </Pressable>
          </View>

          <TextInput
            style={styles.search}
            value={query}
            onChangeText={setQuery}
            placeholder="Type to search…"
            placeholderTextColor={colors.textFaint}
            accessibilityLabel={`Search ${label}`}
            autoCorrect={false}
            autoCapitalize="none"
            autoFocus
            returnKeyType="done"
            onSubmitEditing={() => {
              if (rows.length) pick(rows[0].value);
              else if (offerCustom) pick(query.trim());
            }}
          />

          <FlatList
            data={rows}
            keyExtractor={(o) => o.value || '__empty__'}
            keyboardShouldPersistTaps="handled"
            style={styles.list}
            ListEmptyComponent={
              <Text style={styles.none}>
                {offerCustom ? `Use “${query.trim()}”` : 'No matches'}
              </Text>
            }
            renderItem={({ item }) => {
              const on = item.value === value;
              return (
                <Pressable
                  onPress={() => pick(item.value)}
                  accessibilityRole="button"
                  accessibilityLabel={item.label}
                  accessibilityState={{ selected: on }}
                  style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
                >
                  <Text style={[styles.rowLabel, on && styles.rowLabelOn]} numberOfLines={1}>
                    {item.label}
                  </Text>
                  {item.hint ? <Text style={styles.rowHint}>{item.hint}</Text> : null}
                  {on ? <Ionicons name="checkmark" size={15} color={colors.text} /> : null}
                </Pressable>
              );
            }}
          />

          {offerCustom ? (
            <Pressable
              onPress={() => pick(query.trim())}
              accessibilityRole="button"
              accessibilityLabel={`Use ${query.trim()}`}
              style={styles.custom}
            >
              <Ionicons name="return-down-forward" size={14} color={colors.textMuted} />
              <Text style={styles.customText} numberOfLines={1}>
                Use “{query.trim()}”
              </Text>
            </Pressable>
          ) : null}
        </View>
        </KeyboardAvoidingView>
      </Modal>
    </>
  );
}

const styles = StyleSheet.create({
  field: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.xs,
    paddingVertical: space.xs,
    paddingHorizontal: space.sm,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    // A full touch target: these sit in a dense toolbar and were previously
    // chips small enough to miss.
    minHeight: 44,
  },
  fieldPressed: { backgroundColor: colors.surface3 },
  fieldDisabled: { opacity: 0.5 },
  eyebrow: { color: colors.textFaint, fontSize: font.size.xs },
  value: { flex: 1, color: colors.text, fontSize: font.size.sm },
  valueEmpty: { color: colors.textMuted },

  backdrop: { flex: 1, backgroundColor: 'rgba(0,0,0,0.5)' },
  sheetWrap: { position: 'absolute', left: 0, right: 0, bottom: 0 },
  sheet: {
    // maxHeight is applied inline from sheetMaxHeight() — with the keyboard
    // up, a percentage of the full screen is the wrong denominator.
    backgroundColor: colors.bgElevated,
    borderTopLeftRadius: radius.lg,
    borderTopRightRadius: radius.lg,
    borderTopWidth: 1,
    borderColor: colors.border,
    paddingBottom: space.lg,
  },
  sheetHead: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: space.lg,
    paddingTop: space.lg,
    paddingBottom: space.sm,
  },
  sheetTitle: { color: colors.text, fontSize: font.size.md, fontWeight: '600' },
  search: {
    marginHorizontal: space.lg,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: radius.sm,
    color: colors.text,
    fontSize: font.size.sm,
  },
  list: { marginTop: space.sm, flexShrink: 1 },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    minHeight: 44,
  },
  rowPressed: { backgroundColor: colors.surface2 },
  rowLabel: { flex: 1, color: colors.textMuted, fontSize: font.size.sm },
  rowLabelOn: { color: colors.text, fontWeight: '600' },
  rowHint: { color: colors.textFaint, fontSize: font.size.xs },
  none: { color: colors.textMuted, fontSize: font.size.sm, padding: space.lg, textAlign: 'center' },
  custom: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    marginHorizontal: space.lg,
    paddingVertical: space.md,
    borderTopWidth: 1,
    borderColor: colors.border,
  },
  customText: { color: colors.textMuted, fontSize: font.size.sm, flex: 1 },
});
