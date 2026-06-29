import { Alert, Platform } from 'react-native';

/**
 * Cross-platform confirmation for destructive actions. Native uses a two-button
 * Alert (with a red "destructive" action on iOS); web falls back to
 * window.confirm so the demo build behaves too.
 */
export function confirmAction(opts: {
  title: string;
  message?: string;
  confirmLabel?: string;
  destructive?: boolean;
  onConfirm: () => void;
}): void {
  if (Platform.OS === 'web') {
    const text = opts.message ? `${opts.title}\n\n${opts.message}` : opts.title;
    if (typeof window !== 'undefined' && window.confirm(text)) opts.onConfirm();
    return;
  }
  Alert.alert(opts.title, opts.message, [
    { text: 'Cancel', style: 'cancel' },
    {
      text: opts.confirmLabel ?? 'Confirm',
      style: opts.destructive ? 'destructive' : 'default',
      onPress: opts.onConfirm,
    },
  ]);
}
