import type { HypervisorCommand } from '../../api/hypervisor';

/**
 * Pure logic for the composer's `/` command/skill picker (issue #302). Kept out
 * of Chat.tsx so it's unit-testable without rendering — mirrors chatTabs.ts.
 */

/** Which agents expand a leading `/name` into a skill/slash command in the
 *  headless mode the Hypervisor runs them in. Only Claude is confirmed today
 *  (`claude --help`: "Skills still resolve via /skill-name.") — other adapters
 *  don't resolve inline slash tokens, so the picker stays hidden for them. When
 *  a provider is verified, add its assistant id here and widen the server-side
 *  `commands` source to carry its skills. */
export function supportsSlash(assistant?: string | null): boolean {
  return assistant === 'claude';
}

/** The command token being typed, or null when the composer isn't in "picking a
 *  command" state. Non-null only while the WHOLE draft is a single leading-slash
 *  token with no whitespace yet (`/kc-i`) — once a space/newline or a second
 *  line is added the command is complete and the menu closes on its own. */
export function slashToken(draft: string): string | null {
  const m = /^\/([a-z0-9:_-]*)$/i.exec(draft);
  return m ? m[1].toLowerCase() : null;
}

/** Rank picker entries for a typed prefix: prefix matches first, then substring
 *  matches, each group preserving the source order (already alphabetical from
 *  the server). Empty query → all (capped). Capped at 8 so the popover never
 *  overwhelms the composer on mobile. */
export function matchCommands(
  commands: HypervisorCommand[],
  query: string,
): HypervisorCommand[] {
  const q = query.toLowerCase();
  if (!q) return commands.slice(0, 8);
  const prefix: HypervisorCommand[] = [];
  const infix: HypervisorCommand[] = [];
  for (const c of commands) {
    const n = c.name.toLowerCase();
    if (n.startsWith(q)) prefix.push(c);
    else if (n.includes(q)) infix.push(c);
  }
  return [...prefix, ...infix].slice(0, 8);
}
