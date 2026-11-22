/** A "Done" bar above the iOS keyboard, for inputs whose keyboard has no
 *  return key.
 *
 *  `number-pad`, `decimal-pad` and `phone-pad` keyboards on iOS have no
 *  return key, so `returnKeyType` and `onSubmitEditing` never fire and the
 *  keyboard cannot be put away from the keyboard itself. A field like that
 *  inside a screen or sheet that does not scroll leaves the user stuck behind
 *  the keys (the Security new-scan spending limit did exactly that).
 *
 *  Usage: render the bar once, next to the input, in the same tree (inside the
 *  Modal when the input is in one), and point the input at it:
 *
 *    <TextInput keyboardType="decimal-pad" returnKeyType="done"
 *               inputAccessoryViewID="scan-budget" … />
 *    <KeyboardDoneBar nativeID="scan-budget" />
 *
 *  Give each bar its own `nativeID`, so two mounted bars never share one.
 *  Android needs no bar: its numeric keyboards carry a done key (which
 *  `returnKeyType="done"` labels) and the system back gesture closes them.
 *  On Android and web this renders nothing. */
import React from 'react';
import { InputAccessoryView, Keyboard, Platform, Pressable, StyleSheet, Text, View } from 'react-native';
import { colors, font, space } from '../theme';

export function KeyboardDoneBar({ nativeID }: { nativeID: string }) {
  // InputAccessoryView is iOS-only (react-native-web does not provide it), so
  // it must not even be rendered elsewhere.
  if (Platform.OS !== 'ios') return null;
  return (
    <InputAccessoryView nativeID={nativeID}>
      <View style={styles.bar}>
        <Pressable
          onPress={() => Keyboard.dismiss()}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel="Done, hide keyboard"
          style={({ pressed }) => [styles.done, pressed && { opacity: 0.6 }]}
        >
          <Text style={styles.doneText}>Done</Text>
        </Pressable>
      </View>
    </InputAccessoryView>
  );
}

const styles = StyleSheet.create({
  bar: {
    flexDirection: 'row',
    justifyContent: 'flex-end',
    backgroundColor: colors.bgElevated,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
    paddingHorizontal: space.md,
  },
  // 44pt tall so the button meets the minimum touch target.
  done: { minHeight: 44, justifyContent: 'center', paddingHorizontal: space.sm },
  doneText: { color: colors.accent, fontSize: font.size.md, fontWeight: '700' },
});
