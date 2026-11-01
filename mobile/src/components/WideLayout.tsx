/** The live wide-layout check (#833) — see src/util/wideLayout.ts for the rule. */
import { useWindowDimensions } from 'react-native';
import { isDesktop } from '../desktop/bridge';
import { isWideLayout } from '../util/wideLayout';

// Fixed for the life of the page: the bridge is injected before any script.
const DESKTOP = isDesktop();

export function useWideLayout(): boolean {
  const { width } = useWindowDimensions();
  return isWideLayout(DESKTOP, width);
}
