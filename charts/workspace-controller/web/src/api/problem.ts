export interface Problem {
  error: string;
  code: string;
  retryable: boolean;
  error_id?: string;
  request_id?: string;
}

const MESSAGES: Record<string, string> = {
  metrics_disabled: 'Monitoring is not configured.',
  metrics_timeout: 'Monitoring took too long to respond.',
  metrics_unavailable: 'Monitoring is temporarily unavailable.',
  metrics_not_reporting: 'No workspace metrics received yet.',
  network_error: "Can't reach the server.",
  request_timeout: 'The request took too long. Try again.',
  unauthorized: 'Your session expired. Sign in again.',
  forbidden: "You don't have access to these metrics.",
  internal_error: "Couldn't load metrics. Try again.",
};

export function responseProblem(value: unknown): Problem | null {
  if (!value || typeof value !== 'object') return null;
  const data = value as Record<string, unknown>;
  if ((data.problem && typeof data.problem === 'object') || typeof data.code === 'string') {
    const p = (data.problem ?? data) as Partial<Problem>;
    const code = typeof p.code === 'string' && p.code in MESSAGES ? p.code : 'internal_error';
    return { code, error: MESSAGES[code], retryable: p.retryable !== false,
      error_id: typeof p.error_id === 'string' ? p.error_id : undefined,
      request_id: typeof p.request_id === 'string' ? p.request_id : undefined };
  }
  if (data.metricsError || data.error) {
    const disabled = /disabled|unset|not configured/i.test(String(data.metricsError || data.error));
    const code = disabled ? 'metrics_disabled' : 'metrics_unavailable';
    return { code, error: MESSAGES[code], retryable: !disabled };
  }
  if (data.scrapeHint) return { code: 'metrics_not_reporting', error: MESSAGES.metrics_not_reporting, retryable: false };
  return null;
}

export function failureProblem(error: unknown): Problem {
  const e = error as { status?: number; body?: unknown; code?: string } | null;
  const body = responseProblem(e?.body);
  if (body && e?.status !== 401 && e?.status !== 403) return body;
  const code = e?.status === 401 ? 'unauthorized' : e?.status === 403 ? 'forbidden'
    : e?.code === 'request_timeout' ? 'request_timeout' : e?.status === 0 ? 'network_error' : 'internal_error';
  return { code, error: MESSAGES[code], retryable: !['unauthorized', 'forbidden'].includes(code) };
}
