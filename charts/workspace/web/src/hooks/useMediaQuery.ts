import { useEffect, useState } from 'preact/hooks';

export function useMediaQuery(query: string): boolean {
  const get = () =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query).matches
      : false;
  const [matches, setMatches] = useState<boolean>(get);

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    setMatches(mql.matches);
    if ('addEventListener' in mql) {
      mql.addEventListener('change', onChange);
      return () => mql.removeEventListener('change', onChange);
    }
    // Legacy Safari < 14 fallback.
    (mql as MediaQueryList & { addListener: (cb: () => void) => void }).addListener(onChange);
    return () =>
      (mql as MediaQueryList & { removeListener: (cb: () => void) => void }).removeListener(onChange);
  }, [query]);

  return matches;
}

export const useIsMobile = () => useMediaQuery('(max-width: 720px)');
