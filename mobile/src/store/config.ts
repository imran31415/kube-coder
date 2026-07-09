/**
 * Connection config: the workspace host URL and the Bearer API token, plus a
 * "mock" flag used by the demo/screenshot build. Backed by secure storage on
 * native (token) and AsyncStorage (host/flags).
 *
 * A tiny pub/sub lets React screens re-render when config changes without
 * pulling in a heavier state library.
 */
import { getItem, getSecret, setItem, setSecret, deleteSecret } from './storage';

const HOST_KEY = 'kc.apiBase';
const TOKEN_KEY = 'kc.devToken';
// Optional SECOND connection: an admin controller (list/start/stop workspaces,
// capacity). Fully independent of the workspace connection — different host +
// token, its own reveal flow. Absent by default; the Controller tab only
// appears once both are set.
const CTRL_HOST_KEY = 'kc.controllerBase';
const CTRL_TOKEN_KEY = 'kc.controllerToken';

// The project's public, read-only demo workspace (AUTH_MODE=none) — reached via
// the "Explore the public demo" button on onboarding. Override for a fork with
// EXPO_PUBLIC_DEMO_HOST. Shared so onboarding can connect to it and Settings can
// recognise a demo connection (and offer a clear way back to host/token entry).
export const DEMO_HOST = (
  process.env.EXPO_PUBLIC_DEMO_HOST || 'https://demo-public.dev.scalebase.io'
).replace(/\/+$/, '');

/** True when the given host is the public demo (saveConnection strips the trailing slash). */
export function isDemoHost(host: string): boolean {
  return !!host && host.replace(/\/+$/, '') === DEMO_HOST;
}

export interface Config {
  host: string; // e.g. https://imran.kube-coder.example.com  ('' when unset)
  token: string; // Bearer token ('' when unset)
  controllerHost: string; // admin controller host ('' when unset)
  controllerToken: string; // admin controller Bearer token ('' when unset)
  mock: boolean; // demo mode — serve fake data, no network
  loaded: boolean; // hydrated from storage yet?
}

/** True when a controller connection is available (both host + token, or mock).
 *  Gates the Controller tab + API calls. */
export function hasController(c: Config = state): boolean {
  return c.mock || (!!c.controllerHost && !!c.controllerToken);
}

// EXPO_PUBLIC_MOCK=1 forces demo mode at build time (screenshot/web build).
const FORCE_MOCK = process.env.EXPO_PUBLIC_MOCK === '1';

// EXPO_PUBLIC_HOST + EXPO_PUBLIC_TOKEN pre-seed a real connection at launch,
// skipping onboarding — for CI/e2e, a kiosk device, or a quick local test.
// Both must be set; the token comes from the env, never hardcoded. Ignored when
// mock mode is on.
const FORCE_HOST = (process.env.EXPO_PUBLIC_HOST ?? '').trim().replace(/\/+$/, '');
const FORCE_TOKEN = (process.env.EXPO_PUBLIC_TOKEN ?? '').trim();
const FORCE_CONN = !FORCE_MOCK && !!FORCE_HOST && !!FORCE_TOKEN;

let state: Config = {
  host: FORCE_MOCK ? 'https://demo.kube-coder.app' : FORCE_CONN ? FORCE_HOST : '',
  token: FORCE_MOCK ? 'demo-token' : FORCE_CONN ? FORCE_TOKEN : '',
  // Mock build shows the Controller tab from mock data; real launches start
  // without a controller until the user adds one.
  controllerHost: FORCE_MOCK ? 'https://controller.kube-coder.app' : '',
  controllerToken: FORCE_MOCK ? 'demo-controller-token' : '',
  mock: FORCE_MOCK,
  loaded: FORCE_MOCK || FORCE_CONN, // a seeded build needs no async hydration
};

type Listener = (c: Config) => void;
const listeners = new Set<Listener>();

function emit() {
  for (const l of listeners) l(state);
}

export function getConfig(): Config {
  return state;
}

export function subscribe(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export async function hydrate(): Promise<void> {
  if (state.loaded) return;
  const [host, token, ctrlHost, ctrlToken] = await Promise.all([
    getItem(HOST_KEY),
    getSecret(TOKEN_KEY),
    getItem(CTRL_HOST_KEY),
    getSecret(CTRL_TOKEN_KEY),
  ]);
  state = {
    host: host ?? '',
    token: token ?? '',
    controllerHost: ctrlHost ?? '',
    controllerToken: ctrlToken ?? '',
    mock: false,
    loaded: true,
  };
  emit();
}

export async function saveConnection(host: string, token: string): Promise<void> {
  const cleanHost = host.trim().replace(/\/+$/, '');
  const cleanToken = token.trim();
  await Promise.all([setItem(HOST_KEY, cleanHost), setSecret(TOKEN_KEY, cleanToken)]);
  state = { ...state, host: cleanHost, token: cleanToken, loaded: true };
  emit();
}

export async function clearConnection(): Promise<void> {
  await Promise.all([setItem(HOST_KEY, ''), deleteSecret(TOKEN_KEY)]);
  state = { ...state, host: '', token: '', loaded: true };
  emit();
}

export async function saveControllerConnection(host: string, token: string): Promise<void> {
  const cleanHost = host.trim().replace(/\/+$/, '');
  const cleanToken = token.trim();
  await Promise.all([setItem(CTRL_HOST_KEY, cleanHost), setSecret(CTRL_TOKEN_KEY, cleanToken)]);
  state = { ...state, controllerHost: cleanHost, controllerToken: cleanToken };
  emit();
}

export async function clearControllerConnection(): Promise<void> {
  await Promise.all([setItem(CTRL_HOST_KEY, ''), deleteSecret(CTRL_TOKEN_KEY)]);
  state = { ...state, controllerHost: '', controllerToken: '' };
  emit();
}

export function isConfigured(): boolean {
  return state.mock || (!!state.host && !!state.token);
}
