import { apiGet } from './client';

export interface DocsPageMeta {
  id: string;
  title: string;
  file: string;
  summary?: string;
}

export interface DocsSection {
  id: string;
  title: string;
  pages: DocsPageMeta[];
}

export interface DocsManifest {
  version: number;
  sections: DocsSection[];
}

export interface DocsPage {
  id: string;
  title: string;
  summary?: string;
  section_id: string;
  section_title: string;
  file: string;
  edited_at: number;
  markdown: string;
}

export interface DocsSearchHit {
  id: string;
  title: string;
  section_title: string;
  snippet: string;
  score: number;
}

export interface DocsSearchResponse {
  q: string;
  results: DocsSearchHit[];
}

export const listDocs = () => apiGet<DocsManifest>('/api/docs');
export const getDocsPage = (id: string) => apiGet<DocsPage>(`/api/docs/${encodeURIComponent(id)}`);
export const searchDocs = (q: string) => apiGet<DocsSearchResponse>('/api/docs/search', { q });
