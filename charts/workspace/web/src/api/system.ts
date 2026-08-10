import { apiGet } from './client';

export interface Health {
  vscode?: boolean;
  terminal?: boolean;
  browser?: boolean;
  /** server.py adds an aggregate 'ok' field in some shapes; allow any */
  [k: string]: unknown;
}

export const getHealth = () => apiGet<Health>('/health');

export interface ServerMode {
  readOnly: boolean;
  authed: boolean;
  authMode: 'basic' | 'oauth2' | 'none' | string;
  /** Public-demo "show everything" hint. When true (only on the read-only
   *  demo deploy), MutatorOnly renders mutation controls disabled instead of
   *  hiding them, so visitors see the full UI. The server still 403s writes. */
  demoShowAll?: boolean;
  /** AI CTO feature gate (#467). Undefined/true → the /cto nav item + route
   *  are shown; false hides them (deployment set `cto.enabled: false`, or the
   *  Hypervisor it rides is off). */
  ctoEnabled?: boolean;
  /** devcontainer.json support (#594). Independent of ctoEnabled — reading a
   *  repo's own environment file is a workspace capability, not part of the
   *  CTO page. False hides the Dev container card and 404s /api/devcontainer*. */
  devcontainerEnabled?: boolean;
  /** Board Processor (#588/#589). Independent of ctoEnabled — working an
   *  external tracker and running an AI CTO over our own projects are separate
   *  capabilities. False hides the /board nav item and route. */
  boardEnabled?: boolean;
}

export const getMode = () => apiGet<ServerMode>('/api/mode');
