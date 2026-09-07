/** Keyboard visibility and size. */
import { useEffect, useState } from 'react';
import { Keyboard, Platform } from 'react-native';

/**
 * True while the software keyboard is on screen.
 *
 * Composers pad their bottom with the safe-area inset so they clear the home
 * indicator when the keyboard is down. Once the keyboard is up it already
 * covers that area, so keeping the inset leaves a dead gap between the input and
 * the keys (the classic iOS "input floats above the keyboard" bug). Screens use
 * this to collapse that padding while the keyboard is showing. iOS fires the
 * `Will` events ahead of the animation so the padding changes in step with the
 * keyboard rather than a frame late.
 */
export function useKeyboardVisible(): boolean {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const showEvt = Platform.OS === 'ios' ? 'keyboardWillShow' : 'keyboardDidShow';
    const hideEvt = Platform.OS === 'ios' ? 'keyboardWillHide' : 'keyboardDidHide';
    const show = Keyboard.addListener(showEvt, () => setVisible(true));
    const hide = Keyboard.addListener(hideEvt, () => setVisible(false));
    return () => {
      show.remove();
      hide.remove();
    };
  }, []);
  return visible;
}

/**
 * Height of the software keyboard in points, 0 while it is down.
 *
 * `useKeyboardVisible` is enough to collapse a safe-area inset, but a sheet
 * that is sized as a fraction of the *screen* still overflows the space the
 * keyboard leaves: a 75%-tall sheet on a device whose keyboard takes 40% has
 * most of its list underneath the keys (#662). Sizing needs the number, not
 * the boolean.
 *
 * iOS reports the real height via the `Will` events, ahead of the animation,
 * so a sheet resizes in step with the keyboard rather than a frame late.
 */
export function useKeyboardHeight(): number {
  const [height, setHeight] = useState(0);
  useEffect(() => {
    const showEvt = Platform.OS === 'ios' ? 'keyboardWillShow' : 'keyboardDidShow';
    const hideEvt = Platform.OS === 'ios' ? 'keyboardWillHide' : 'keyboardDidHide';
    const show = Keyboard.addListener(showEvt, (e) =>
      setHeight(e.endCoordinates?.height ?? 0),
    );
    const hide = Keyboard.addListener(hideEvt, () => setHeight(0));
    return () => {
      show.remove();
      hide.remove();
    };
  }, []);
  return height;
}
