import { computed, signal } from '@preact/signals';
import { getDocsPage, listDocs, type DocsManifest, type DocsPage } from '../api/docs';

export const manifest = signal<DocsManifest | null>(null);
export const manifestLoading = signal(false);
export const manifestError = signal<string | null>(null);

export const currentPageId = signal<string | null>(null);
export const currentPage = signal<DocsPage | null>(null);
export const pageLoading = signal(false);
export const pageError = signal<string | null>(null);

/** Default page when the user lands on /docs without a sub-path. */
export const defaultPageId = computed(() => {
  const m = manifest.value;
  if (!m) return null;
  for (const sec of m.sections) {
    for (const p of sec.pages) return p.id;
  }
  return null;
});

let manifestLoaded = false;
let inflight: Promise<void> | null = null;
export async function loadManifest(force = false): Promise<void> {
  if (manifestLoaded && !force) return;
  if (inflight) return inflight;
  manifestLoading.value = true;
  manifestError.value = null;
  inflight = (async () => {
    try {
      manifest.value = await listDocs();
      manifestLoaded = true;
    } catch (e) {
      manifestError.value = e instanceof Error ? e.message : String(e);
    } finally {
      manifestLoading.value = false;
      inflight = null;
    }
  })();
  return inflight;
}

const pageCache = new Map<string, DocsPage>();
let pageInflight: { id: string; promise: Promise<void> } | null = null;
export async function loadPage(id: string): Promise<void> {
  currentPageId.value = id;
  const cached = pageCache.get(id);
  if (cached) {
    currentPage.value = cached;
    pageError.value = null;
    return;
  }
  if (pageInflight && pageInflight.id === id) return pageInflight.promise;
  pageLoading.value = true;
  pageError.value = null;
  const promise = (async () => {
    try {
      const page = await getDocsPage(id);
      pageCache.set(id, page);
      // Only commit if the user hasn't navigated away while the fetch ran.
      if (currentPageId.value === id) currentPage.value = page;
    } catch (e) {
      if (currentPageId.value === id) {
        pageError.value = e instanceof Error ? e.message : String(e);
        currentPage.value = null;
      }
    } finally {
      if (pageInflight && pageInflight.id === id) pageInflight = null;
      pageLoading.value = false;
    }
  })();
  pageInflight = { id, promise };
  return promise;
}

/** Flat list of pages in manifest order — useful for prev/next + palette. */
export const flatPages = computed(() => {
  const out: { id: string; title: string; section: string }[] = [];
  const m = manifest.value;
  if (!m) return out;
  for (const sec of m.sections) {
    for (const p of sec.pages) out.push({ id: p.id, title: p.title, section: sec.title });
  }
  return out;
});

/** Test-only reset. */
export function _resetDocsStore(): void {
  manifest.value = null;
  manifestLoading.value = false;
  manifestError.value = null;
  currentPageId.value = null;
  currentPage.value = null;
  pageLoading.value = false;
  pageError.value = null;
  manifestLoaded = false;
  inflight = null;
  pageInflight = null;
  pageCache.clear();
}
