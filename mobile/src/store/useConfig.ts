import { useEffect, useState } from 'react';
import { Config, getConfig, subscribe } from './config';

/** Subscribe a component to connection-config changes. */
export function useConfig(): Config {
  const [cfg, setCfg] = useState<Config>(getConfig());
  useEffect(() => {
    const off = subscribe(setCfg);
    // Hydration can land between this render and the subscription — the
    // desktop app's storage answers within a frame (#833) — and a missed
    // update leaves the app on its boot screen. Re-read rather than trust the
    // value from render (the same guard as useBoardFocus).
    setCfg(getConfig());
    return off;
  }, []);
  return cfg;
}
