import { effect, signal } from '@preact/signals';

/**
 * Saved prompt templates for the New Build composer (#94).
 *
 * Deliberately client-side: a template is a personal, low-stakes snippet, so
 * it lives in localStorage under its own versioned `kc.*` key (same pattern as
 * the rail's group state in ui.ts) rather than costing a server endpoint and a
 * round-trip on every open of the drawer.
 */
export interface PromptTemplate {
  id: string;
  name: string;
  prompt: string;
  /** Unix ms — only used to keep the list in a stable newest-first order. */
  created_at: number;
}

const STORAGE_KEY = 'kc.prompt.templates.v1';

/** Cap so a runaway save loop can't blow the localStorage quota. */
export const MAX_TEMPLATES = 30;

function sane(v: unknown): PromptTemplate | null {
  if (!v || typeof v !== 'object') return null;
  const t = v as Record<string, unknown>;
  if (typeof t.id !== 'string' || typeof t.name !== 'string' || typeof t.prompt !== 'string') {
    return null;
  }
  return {
    id: t.id,
    name: t.name,
    prompt: t.prompt,
    created_at: typeof t.created_at === 'number' ? t.created_at : 0,
  };
}

function load(): PromptTemplate[] {
  if (typeof localStorage === 'undefined') return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map(sane)
      .filter((t): t is PromptTemplate => t !== null)
      .slice(0, MAX_TEMPLATES);
  } catch {
    return [];
  }
}

/** Newest-first list of the user's saved prompts. */
export const promptTemplates = signal<PromptTemplate[]>(load());

if (typeof localStorage !== 'undefined') {
  effect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(promptTemplates.value));
    } catch {
      // localStorage may be unavailable (Safari private mode, quota); skip.
    }
  });
}

let seq = 0;
function nextId(): string {
  return `tpl-${Date.now().toString(36)}-${(seq++).toString(36)}`;
}

/**
 * Save `prompt` under `name`. Re-saving an existing name updates it in place
 * (rather than piling up near-duplicates), so "save as template" is safe to
 * hit twice. Returns the stored template, or null when either field is blank.
 */
export function saveTemplate(name: string, prompt: string): PromptTemplate | null {
  const n = name.trim();
  const p = prompt.trim();
  if (!n || !p) return null;
  const existing = promptTemplates.value.find(
    (t) => t.name.toLowerCase() === n.toLowerCase(),
  );
  if (existing) {
    const updated = { ...existing, name: n, prompt: p };
    promptTemplates.value = promptTemplates.value.map((t) =>
      t.id === existing.id ? updated : t,
    );
    return updated;
  }
  const tpl: PromptTemplate = { id: nextId(), name: n, prompt: p, created_at: Date.now() };
  promptTemplates.value = [tpl, ...promptTemplates.value].slice(0, MAX_TEMPLATES);
  return tpl;
}

export function deleteTemplate(id: string): void {
  promptTemplates.value = promptTemplates.value.filter((t) => t.id !== id);
}

/**
 * A sensible default name for "save as template" — the prompt's first line,
 * trimmed to something that fits a chip. Falls back to 'Template'.
 */
export function suggestTemplateName(prompt: string): string {
  const firstLine = (prompt || '').trim().split('\n')[0].trim();
  if (!firstLine) return 'Template';
  return firstLine.length > 40 ? `${firstLine.slice(0, 40).trimEnd()}…` : firstLine;
}
