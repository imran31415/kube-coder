/** Small formatting helpers. */

export function relativeTime(epochSeconds?: number): string {
  if (!epochSeconds) return '';
  const now = Date.now() / 1000;
  const diff = Math.max(0, now - epochSeconds);
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function statusLabel(status: string): string {
  switch (status) {
    case 'waiting':
    case 'waiting_input':
      return 'waiting';
    case 'done':
    case 'completed':
      return 'done';
    default:
      return status;
  }
}
