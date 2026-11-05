export function boardRunPath(boardId: string, runId: string): string {
  return `/board/${encodeURIComponent(boardId)}/runs/${encodeURIComponent(runId)}`;
}

export function parseBoardRunPath(path: string): { boardId: string; runId: string } | null {
  const match = /^\/board\/([^/?#]+)\/runs\/(run-[a-z0-9-]+)$/.exec(path);
  if (!match) return null;
  try { return { boardId: decodeURIComponent(match[1]), runId: match[2] }; }
  catch { return null; }
}
