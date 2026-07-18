import { useEffect } from 'preact/hooks';

/**
 * Reflects the on-screen keyboard into the DOM so CSS can respond to it.
 *
 * When a mobile keyboard opens, the page's fixed bottom nav is hidden behind
 * it — but layout that reserves the nav's height (and pads for the
 * home-indicator safe area) leaves the chat composer floating well above the
 * keyboard instead of flush against it. There's no CSS media query for
 * "keyboard is open", and `100dvh` is unreliable here (on iOS Safari the
 * keyboard is an overlay and `dvh` doesn't shrink), so we read
 * `window.visualViewport` — the one signal that tracks the keyboard on both iOS
 * and Android — and toggle `html[data-keyboard-open]`. CSS keys off that
 * attribute to reclaim the hidden nav's space and drop the safe-area padding.
 *
 * Mounted once from the Shell so every route (Hypervisor chat, Build chat, …)
 * shares the same signal.
 */
export function useKeyboardInset() {
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const root = document.documentElement;
    const update = () => {
      // Height of the layout viewport hidden below the visual viewport ≈ the
      // on-screen keyboard. offsetTop covers the case where the page is scrolled
      // within the visual viewport.
      const overlap = window.innerHeight - vv.height - vv.offsetTop;
      // Ignore small overlaps — URL-bar collapse and sub-pixel rounding, not a
      // keyboard (which is always a few hundred px tall).
      root.dataset.keyboardOpen = overlap > 120 ? 'true' : 'false';
    };
    update();
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => {
      vv.removeEventListener('resize', update);
      vv.removeEventListener('scroll', update);
      delete root.dataset.keyboardOpen;
    };
  }, []);
}
