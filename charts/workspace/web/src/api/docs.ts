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

export const listDocs = () => apiGet<DocsManifest>('/api/docs');
export const getDocsPage = (id: string) => apiGet<DocsPage>(`/api/docs/${encodeURIComponent(id)}`);
