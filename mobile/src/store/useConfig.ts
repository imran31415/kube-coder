import { useEffect, useState } from 'react';
import { Config, getConfig, subscribe } from './config';

/** Subscribe a component to connection-config changes. */
export function useConfig(): Config {
  const [cfg, setCfg] = useState<Config>(getConfig());
  useEffect(() => subscribe(setCfg), []);
  return cfg;
}
