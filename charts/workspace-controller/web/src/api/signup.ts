export interface SignupProblem {
  code: string; error: string; retryable: boolean; error_id?: string;
}
export interface SignupOperation {
  operationId: string; slug: string;
  stage: 'identity' | 'configuration' | 'deployment' | 'readiness' | 'ready' | 'failed' | 'deleted';
  updatedAt: number; startedAt?: number; url: string | null; error: SignupProblem | null;
}
export interface SignupMe {
  login: string; eligible: boolean; csrfToken: string; operation: SignupOperation | null;
}
export class SignupFailure extends Error {
  constructor(public problem: SignupProblem, public status = 0) { super(problem.error); }
}

// Signup never uses the admin API prefix, dev token or automatic login redirect.
export async function signupRequest<T>(path: string, csrf?: string, signal?: AbortSignal): Promise<T> {
  const abort = new AbortController();
  const cancel = () => abort.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) cancel();
  const timer = setTimeout(cancel, 15000);
  try {
    const res = await fetch(`/api/signup/${path}`, { method: csrf ? 'POST' : 'GET',
      signal: abort.signal, redirect: 'error',
      headers: { Accept: 'application/json', ...(csrf ? { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf } : {}) },
      ...(csrf ? { body: '{}' } : {}) });
    if (res.status === 401) throw new SignupFailure({ code: 'unauthorized', error: 'Sign in with GitHub to continue.', retryable: false }, 401);
    if (!res.headers.get('Content-Type')?.includes('application/json')) throw new Error('Unexpected response');
    const data = await res.json();
    if (!res.ok) {
      if (!data || typeof data.error !== 'string' || typeof data.code !== 'string' || typeof data.retryable !== 'boolean') throw new Error('Unexpected error response');
      throw new SignupFailure(data, res.status);
    }
    return data as T;
  } catch (error) {
    if (error instanceof SignupFailure) throw error;
    throw new SignupFailure({ code: 'network_error', error: "Can't reach the server. Check your connection and try again.", retryable: true });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', cancel);
  }
}
