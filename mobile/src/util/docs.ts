/**
 * Pure helpers for the Docs viewer (#250).
 *
 * The manifest is a two-level tree (sections → pages). The mobile viewer shows
 * it as a single sectioned list, so the tree is flattened once here and the
 * search filter runs over the flat rows — no react-native imports, so vitest
 * covers it without a RN runtime.
 */
import type { DocsManifest, DocsPageMeta } from '../api/types';

/** A manifest page with its section carried along, for a flat sectioned list. */
export interface DocsRow extends DocsPageMeta {
  section_id: string;
  section_title: string;
}

export function flattenDocs(manifest: DocsManifest | null): DocsRow[] {
  if (!manifest) return [];
  return (manifest.sections ?? []).flatMap((s) =>
    (s.pages ?? []).map((p) => ({ ...p, section_id: s.id, section_title: s.title })),
  );
}

/** Title/summary/section substring filter. Body text isn't available client
 *  side (each page is fetched on open), so this stays a manifest-level search
 *  — instant, offline, and honest about what it matched. */
export function filterDocs(rows: DocsRow[], query: string): DocsRow[] {
  const q = query.trim().toLowerCase();
  if (!q) return rows;
  return rows.filter((r) =>
    `${r.title} ${r.summary ?? ''} ${r.section_title} ${r.id}`.toLowerCase().includes(q),
  );
}

/**
 * Drop a leading `# Heading` when it just repeats the page title the article
 * screen already renders — most docs pages open with one, and showing both
 * costs a phone screen its first fold to a duplicate.
 */
export function stripLeadingTitle(markdown: string, title: string): string {
  const src = markdown.replace(/^﻿/, '');
  const m = src.match(/^\s*#\s+(.+?)\s*(?:\n|$)/);
  if (!m) return markdown;
  const norm = (s: string) => s.trim().toLowerCase().replace(/\s+/g, ' ');
  if (norm(m[1]) !== norm(title)) return markdown;
  return src.slice(m[0].length).replace(/^\n+/, '');
}

/** Group flat rows back into sections, dropping any section left empty by the
 *  filter. Preserves manifest order. */
export function groupDocs(rows: DocsRow[]): { id: string; title: string; pages: DocsRow[] }[] {
  const out: { id: string; title: string; pages: DocsRow[] }[] = [];
  for (const r of rows) {
    let section = out.find((s) => s.id === r.section_id);
    if (!section) {
      section = { id: r.section_id, title: r.section_title, pages: [] };
      out.push(section);
    }
    section.pages.push(r);
  }
  return out;
}
