import { apiGet, apiPost, ApiError } from './client';
import { safeArray, safeString } from './shape';

/**
 * devcontainer.json client (#594).
 *
 * A repo carries `devcontainer.json` to describe its own environment — the
 * same file Codespaces, Coder, Ona and DevPod read. kube-coder interprets it
 * INSIDE the existing workspace pod: forwarded ports become Apps pins, VS Code
 * extensions and settings reach code-server, containerEnv/remoteEnv reach the
 * agent. It does not build a container, and the properties that would require
 * one (`features`, `image`, `build`, `dockerComposeFile`) come back in
 * `unsupported` with a reason and a remedy rather than being dropped silently.
 *
 * Two things the UI must honour:
 *   * `found: false` arrives as 200, not 404 — "no devcontainer here" is a
 *     normal answer. Do not treat it as an error.
 *   * `config_hash` must be echoed back on any apply that runs commands. It is
 *     a compare-and-swap: without it, a `git pull` between the dialog
 *     rendering the command text and the user clicking Confirm means they
 *     approved one command and a different one ran.
 */

export type HookName = 'onCreate' | 'updateContent' | 'postCreate' | 'postStart';
export const HOOKS: HookName[] = ['onCreate', 'updateContent', 'postCreate', 'postStart'];

export type LifecycleState =
  | 'none' | 'pending' | 'running' | 'done' | 'stale' | 'failed' | 'timed_out';

export type UnsupportedSeverity = 'blocking' | 'partial' | 'ignored';

export interface DevcontainerCommand {
  /** '' for the string/array forms; the key for the object form. */
  name: string;
  kind: 'shell' | 'argv';
  command: string | string[];
  /** Exactly what will run — render this VERBATIM in the confirm dialog. */
  display: string;
  needs_root: boolean;
  root_reasons: string[];
  caveats: string[];
}

export interface UnsupportedProperty {
  key: string;
  severity: UnsupportedSeverity;
  detail: string;
  reason: string;
  remedy: string;
}

export interface DevcontainerPort {
  port: number;
  label: string;
}

export interface LifecycleEntry {
  status: LifecycleState;
  count: number;
  ran_at?: number | null;
  exit_code?: number | null;
  log_tail?: string;
  log_path?: string;
}

export interface DevcontainerApplied {
  ports_pinned: number[];
  extensions_installed: string[];
  settings_written: Record<string, unknown>;
  env: Record<string, string>;
  applied_at: number | null;
  auto_apply: boolean;
  consented_hash: string;
}

export interface DevcontainerRecord {
  workdir: string;
  found: boolean;
  path: string;
  rel_path: string;
  /** Non-empty when the file exists but could not be read or parsed. */
  error: string;
  error_line: number;
  error_column: number;
  config_hash: string;
  name: string;
  workspace_folder: string;
  lifecycle: Record<HookName, DevcontainerCommand[]>;
  ports: DevcontainerPort[];
  ports_skipped: { value: unknown; reason: string }[];
  extensions: string[];
  extensions_rejected: { value: string; reason: string }[];
  settings: Record<string, unknown>;
  settings_denied: { key: string; reason: string }[];
  env: Record<string, string>;
  env_denied: { key: string; reason: string }[];
  unsupported: UnsupportedProperty[];
  caveats: string[];
  needs_root: boolean;
  applied: DevcontainerApplied;
  lifecycle_status: Partial<Record<HookName, LifecycleEntry>>;
  busy: boolean;
}

export interface DevcontainerSummary {
  workdir: string;
  label: string;
  found: true;
  path: string;
  name: string;
  config_hash: string;
  ports: number;
  extensions: number;
  settings: number;
  env: number;
  hooks: Partial<Record<HookName, number>>;
  unsupported: number;
  blocking: number;
  blocking_keys: string[];
  needs_root: boolean;
  error?: string;
}

export interface ApplyInput {
  workdir: string;
  /** Empty (the default) applies ports/settings/extensions/env and runs nothing. */
  hooks?: HookName[];
  /** REQUIRED whenever `hooks` is non-empty. See the note at the top. */
  config_hash?: string;
  /** Opt this workdir into the once-per-boot postStart pass. */
  auto_apply?: boolean;
}

export interface ApplyResult {
  workdir: string;
  config_hash: string;
  ports_pinned: number[];
  port_conflicts: { port: number; label: string; existing: string; reason: string }[];
  settings_written: string[];
  settings_skipped: { key: string; reason: string }[];
  extensions_installed: string[];
  extension_failures: { id: string; reason: string }[];
  env: string[];
  hooks_started: HookName[];
  unsupported: UnsupportedProperty[];
}

const emptyLifecycle = (): Record<HookName, DevcontainerCommand[]> =>
  ({ onCreate: [], updateContent: [], postCreate: [], postStart: [] });

const coerceRecord = (raw: DevcontainerRecord): DevcontainerRecord => {
  const life = emptyLifecycle();
  for (const h of HOOKS) life[h] = safeArray(raw?.lifecycle?.[h]) as DevcontainerCommand[];
  return {
    ...raw,
    workdir: safeString(raw?.workdir),
    found: !!raw?.found,
    rel_path: safeString(raw?.rel_path),
    error: safeString(raw?.error),
    name: safeString(raw?.name),
    config_hash: safeString(raw?.config_hash),
    lifecycle: life,
    ports: safeArray(raw?.ports) as DevcontainerPort[],
    ports_skipped: safeArray(raw?.ports_skipped) as DevcontainerRecord['ports_skipped'],
    extensions: safeArray(raw?.extensions) as string[],
    extensions_rejected: safeArray(raw?.extensions_rejected) as DevcontainerRecord['extensions_rejected'],
    settings: raw?.settings ?? {},
    settings_denied: safeArray(raw?.settings_denied) as DevcontainerRecord['settings_denied'],
    env: raw?.env ?? {},
    env_denied: safeArray(raw?.env_denied) as DevcontainerRecord['env_denied'],
    unsupported: safeArray(raw?.unsupported) as UnsupportedProperty[],
    caveats: safeArray(raw?.caveats) as string[],
    lifecycle_status: raw?.lifecycle_status ?? {},
    applied: {
      ports_pinned: safeArray(raw?.applied?.ports_pinned) as number[],
      extensions_installed: safeArray(raw?.applied?.extensions_installed) as string[],
      settings_written: raw?.applied?.settings_written ?? {},
      env: raw?.applied?.env ?? {},
      applied_at: raw?.applied?.applied_at ?? null,
      auto_apply: !!raw?.applied?.auto_apply,
      consented_hash: safeString(raw?.applied?.consented_hash),
    },
  };
};

export const getDevcontainer = (workdir: string) =>
  apiGet<DevcontainerRecord>('/api/devcontainer', { workdir }).then(coerceRecord);

export const scanDevcontainers = () =>
  apiGet<{ devcontainers: DevcontainerSummary[]; count: number }>('/api/devcontainer/scan')
    .then((r) => safeArray(r?.devcontainers) as DevcontainerSummary[]);

/**
 * Thrown when devcontainer.json changed between load and confirm (409
 * hash_mismatch), or when a run is already in progress (409 busy). Typed so
 * the dialog can re-load and re-prompt instead of showing a raw error.
 */
export class DevcontainerConflictError extends Error {
  code: 'hash_mismatch' | 'busy' | string;
  constructor(code: string, message: string) {
    super(message);
    this.name = 'DevcontainerConflictError';
    this.code = code;
  }
}

export const applyDevcontainer = (input: ApplyInput) =>
  apiPost<ApplyResult>('/api/devcontainer/apply', input).catch((err) => {
    const status = (err as ApiError)?.status;
    const body = (err as { body?: { code?: string; error?: string } })?.body;
    if (status === 409 && body?.code) {
      throw new DevcontainerConflictError(body.code, body.error ?? 'conflict');
    }
    throw err;
  });

export const resetDevcontainer = (workdir: string, unpinPorts = false) =>
  apiPost<{ workdir: string; cleared: boolean; unpinned: number[] }>(
    '/api/devcontainer/reset', { workdir, unpin_ports: unpinPorts });
