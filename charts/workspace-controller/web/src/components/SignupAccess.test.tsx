import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/preact';
import { ApiError } from '../api/client';
import type { AccessEntry, AccessOverview, GithubAccount } from '../api/allowlist';

// Plain-function module mock (not vi.fn spies), as in ProvisionForm.test: the
// respond* hooks steer each call and `calls` records what the page asked for.
let respondList: () => Promise<AccessOverview>;
let respondLookup: (login: string) => Promise<GithubAccount>;
let respondAllow: () => Promise<{ entry: AccessEntry; created: boolean }>;
let respondRemove: () => Promise<{ removed: boolean }>;
const calls = { list: 0, lookup: [] as string[], allow: [] as unknown[], remove: [] as string[] };
vi.mock('../api/allowlist', async (orig) => ({
  ...(await orig<typeof import('../api/allowlist')>()),
  getAccessList: () => { calls.list += 1; return respondList(); },
  lookupAccount: (login: string) => { calls.lookup.push(login); return respondLookup(login); },
  allowAccount: (login: string, githubId: string, note: string) => {
    calls.allow.push({ login, githubId, note });
    return respondAllow();
  },
  removeAccount: (githubId: string) => { calls.remove.push(githubId); return respondRemove(); },
}));

import { SignupAccess } from './SignupAccess';

const helm: AccessEntry = { githubId: '123', login: null, source: 'helm', workspace: null };
const mona: AccessEntry = {
  githubId: '456', login: 'Mona', source: 'console', addedBy: 'alice', addedAt: Date.now() / 1000 - 2 * 86400,
  note: 'Spring intern', workspace: { slug: 'mona', stage: 'ready' },
};
const overview = (entries: AccessEntry[]): AccessOverview =>
  ({ entries, limit: 1000, noteMax: 200, signupUrl: 'https://signup.example.test/' });
const account = (over: Partial<GithubAccount> = {}): GithubAccount => ({
  githubId: '789', login: 'Hubot', name: 'Hu Bot', avatarUrl: '', allowed: false, source: null, workspace: null, ...over,
});
const failure = (status: number, code: string, error: string, retryable = false) =>
  Promise.reject(new ApiError(error, status, { error, code, retryable, error_id: 'ref123' }));

const rowFor = (text: string) => screen.getByText(text).closest('li') as HTMLElement;
const lookUp = (login: string) => {
  fireEvent.input(screen.getByPlaceholderText('octocat'), { target: { value: login } });
  fireEvent.click(screen.getByRole('button', { name: 'Look up' }));
};

describe('SignupAccess', () => {
  beforeEach(() => {
    respondList = () => Promise.resolve(overview([mona, helm]));
    respondLookup = () => Promise.resolve(account());
    respondAllow = () => Promise.resolve({
      entry: { githubId: '789', login: 'Hubot', source: 'console', addedBy: 'alice', addedAt: Date.now() / 1000, note: '', workspace: null },
      created: true });
    respondRemove = () => Promise.resolve({ removed: true });
    calls.list = 0;
    calls.lookup.length = 0;
    calls.allow.length = 0;
    calls.remove.length = 0;
  });
  afterEach(() => vi.useRealTimers());

  it('lists console entries with who added them, and Helm entries read-only', async () => {
    render(<SignupAccess />);
    expect(screen.getByText('Loading signup access…')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('@Mona')).toBeInTheDocument());
    expect(screen.getByText('2 people')).toBeInTheDocument();
    expect(screen.getByText(/signup\.example\.test/)).toBeInTheDocument();
    const monaRow = rowFor('@Mona');
    expect(within(monaRow).getByText('ID 456 · added by alice · 2 days ago')).toBeInTheDocument();
    expect(within(monaRow).getByText('Spring intern')).toBeInTheDocument();
    expect(within(monaRow).getByRole('link', { name: 'Workspace →' })).toHaveAttribute('href', '#/w/mona');
    const helmRow = rowFor('GitHub ID 123');
    expect(within(helmRow).getByText('Helm')).toBeInTheDocument();
    expect(within(helmRow).getByText('No workspace yet')).toBeInTheDocument();
    expect(within(helmRow).queryByRole('button', { name: /Remove/ })).toBeNull();
  });

  it('never leaves an avatar blank: the initial shows under the photo and after it fails', async () => {
    render(<SignupAccess />);
    await waitFor(() => expect(screen.getByText('@Mona')).toBeInTheDocument());
    const avatar = rowFor('@Mona').querySelector('.access-avatar') as HTMLElement;
    const photo = avatar.querySelector('img') as HTMLImageElement;
    expect(avatar.textContent).toBe('M');
    expect(photo.getAttribute('src')).toBe('https://avatars.githubusercontent.com/u/456?s=80');
    fireEvent.error(photo);
    expect(avatar.querySelector('img')).toBeNull();
    expect(avatar.textContent).toBe('M');
    expect(rowFor('GitHub ID 123').querySelector('.access-avatar')?.textContent).toBe('#');
  });

  it('explains an empty list and how to start', async () => {
    respondList = () => Promise.resolve(overview([]));
    render(<SignupAccess />);
    await waitFor(() => expect(screen.getByText('No one is on the list yet.')).toBeInTheDocument());
    expect(screen.getByText(/Look up a GitHub username above/)).toBeInTheDocument();
  });

  it('looks a person up, confirms the account, and adds them to the top', async () => {
    render(<SignupAccess />);
    await waitFor(() => screen.getByText('@Mona'));
    lookUp('@Hubot');
    await waitFor(() => expect(screen.getByText('Hu Bot')).toBeInTheDocument());
    expect(calls.lookup).toEqual(['Hubot']);
    expect(screen.getByText('@Hubot · ID 789')).toBeInTheDocument();
    fireEvent.input(screen.getByPlaceholderText('e.g. Spring intern'), { target: { value: ' Contractor ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Allow @Hubot' }));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('@Hubot can now sign up.'));
    expect(calls.allow).toEqual([{ login: 'Hubot', githubId: '789', note: 'Contractor' }]);
    expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('@Hubot');
    expect(screen.getAllByRole('listitem')[0]).toHaveClass('fresh');
    expect(screen.getByText('3 people')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('octocat')).toHaveValue('');
    expect(screen.queryByText('Hu Bot')).toBeNull();
  });

  it('takes the note limit from the server instead of its own copy', async () => {
    respondList = () => Promise.resolve({ ...overview([mona, helm]), noteMax: 50 });
    render(<SignupAccess />);
    await waitFor(() => screen.getByText('@Mona'));
    lookUp('Hubot');
    await waitFor(() => screen.getByText('Hu Bot'));
    expect(screen.getByPlaceholderText('e.g. Spring intern')).toHaveAttribute('maxLength', '50');
  });

  it('counts only console entries against the cap, as the server does', async () => {
    respondList = () => Promise.resolve({ ...overview([mona, helm]), limit: 2 });
    const { unmount } = render(<SignupAccess />);
    await waitFor(() => screen.getByText('@Mona'));
    expect(screen.queryByText(/The list is full/)).toBeNull();   // 1 console entry of 2; Helm doesn't count
    unmount();
    respondList = () => Promise.resolve({ ...overview([mona, helm]), limit: 1 });
    render(<SignupAccess />);
    await waitFor(() => expect(screen.getByText('The list is full (1). Remove someone to add another person.'))
      .toBeInTheDocument());
  });

  it('checks the username shape before asking the server', async () => {
    render(<SignupAccess />);
    lookUp('not a user!');
    expect(screen.getByRole('alert')).toHaveTextContent('GitHub usernames use letters, numbers and single hyphens.');
    expect(calls.lookup).toEqual([]);
  });

  it('says plainly when no such account exists, without offering a pointless retry', async () => {
    respondLookup = () => failure(404, 'github_user_not_found', 'No GitHub account has that username.');
    render(<SignupAccess />);
    lookUp('nobody');
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('No GitHub account has that username.'));
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(screen.queryByText(/^Ref /)).toBeNull();
  });

  it('offers Retry when GitHub is down, and the retry recovers', async () => {
    respondLookup = () => failure(502, 'github_unavailable', "GitHub didn't respond. Try again.", true);
    render(<SignupAccess />);
    lookUp('hubot');
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent("GitHub didn't respond."));
    expect(screen.getByText('Ref ref123')).toBeInTheDocument();
    respondLookup = () => Promise.resolve(account());
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByText('Hu Bot')).toBeInTheDocument());
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('tells the admin someone is already allowed instead of offering to add them', async () => {
    respondLookup = () => Promise.resolve(account({ allowed: true, source: 'helm', workspace: { slug: 'hubot', stage: 'deployment' } }));
    render(<SignupAccess />);
    lookUp('hubot');
    await waitFor(() => expect(screen.getByText('Already allowed by the Helm values.')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: /Allow/ })).toBeNull();
    expect(screen.getByRole('link', { name: 'Setting up' })).toHaveAttribute('href', '#/provision/hubot');
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByText('Hu Bot')).toBeNull();
  });

  it('asks the username to be looked up again when it changed hands', async () => {
    respondAllow = () => failure(409, 'github_user_changed', 'That username now belongs to a different account. Look it up again.');
    render(<SignupAccess />);
    lookUp('hubot');
    await waitFor(() => screen.getByRole('button', { name: 'Allow @Hubot' }));
    fireEvent.click(screen.getByRole('button', { name: 'Allow @Hubot' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Look it up again.'));
    expect(screen.queryByText('Hu Bot')).toBeNull();
  });

  it('confirms a removal inline, says what it does not do, and can be cancelled', async () => {
    render(<SignupAccess />);
    await waitFor(() => screen.getByText('@Mona'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove @Mona' }));
    const confirm = screen.getByRole('group', { name: 'Confirm removing @Mona' });
    expect(confirm).toHaveTextContent('Their existing workspace keeps working');
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('group')).toBeNull();
    expect(calls.remove).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Remove @Mona' }));
    fireEvent.click(within(screen.getByRole('group')).getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(screen.queryByText('@Mona')).toBeNull());
    expect(calls.remove).toEqual(['456']);
    expect(screen.getByRole('status')).toHaveTextContent('@Mona removed. Their existing workspace keeps working.');
  });

  it('keeps a failed removal in the row with a reason and Retry', async () => {
    respondRemove = () => failure(503, 'state_unavailable', 'Workspace progress is temporarily unavailable. Try again.', true);
    render(<SignupAccess />);
    await waitFor(() => screen.getByText('@Mona'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove @Mona' }));
    fireEvent.click(within(screen.getByRole('group')).getByRole('button', { name: 'Remove' }));
    const row = rowFor('@Mona');
    await waitFor(() => expect(within(row).getByRole('alert')).toHaveTextContent("Couldn't reach the signup list storage."));
    respondRemove = () => Promise.resolve({ removed: true });
    fireEvent.click(within(row).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByText('@Mona')).toBeNull());
  });

  it('re-reads the list when a write timed out, because it may have landed', async () => {
    respondAllow = () => Promise.reject(new ApiError('The request took too long. Try again.', 0, null, 'request_timeout'));
    render(<SignupAccess />);
    await waitFor(() => screen.getByText('@Mona'));
    lookUp('hubot');
    await waitFor(() => screen.getByRole('button', { name: 'Allow @Hubot' }));
    fireEvent.click(screen.getByRole('button', { name: 'Allow @Hubot' }));
    await waitFor(() => expect(screen.getByRole('alert'))
      .toHaveTextContent("The controller didn't answer in time. Refreshing the list to check."));
    expect(calls.list).toBe(2);
  });

  it('shows why the list could not load, and Retry loads it', async () => {
    respondList = () => failure(503, 'state_unavailable', 'unavailable', true);
    render(<SignupAccess />);
    await waitFor(() => expect(screen.getByText("Couldn't reach the signup list storage. Try again.")).toBeInTheDocument());
    respondList = () => Promise.resolve(overview([mona]));
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByText('@Mona')).toBeInTheDocument());
    expect(screen.queryByText(/Couldn't reach/)).toBeNull();
  });

  it('says so when loading is stuck, with a way to retry', async () => {
    vi.useFakeTimers();
    respondList = () => new Promise(() => {});
    render(<SignupAccess />);
    expect(screen.queryByText(/Still loading/)).toBeNull();
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(screen.getByText(/Still loading — the controller is slow to answer/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(calls.list).toBe(2);
  });

  it('filters a long list by name, ID or note', async () => {
    const many = Array.from({ length: 12 }, (_, i): AccessEntry => ({
      githubId: String(1000 + i), login: `user${i}`, source: 'console', addedBy: 'alice', addedAt: 1, note: i === 7 ? 'design team' : '', workspace: null }));
    respondList = () => Promise.resolve(overview(many));
    render(<SignupAccess />);
    await waitFor(() => screen.getByText('12 people'));
    fireEvent.input(screen.getByRole('searchbox', { name: 'Search people' }), { target: { value: 'design' } });
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByText('@user7')).toBeInTheDocument();
    fireEvent.input(screen.getByRole('searchbox', { name: 'Search people' }), { target: { value: 'nobody' } });
    expect(screen.getByText('No one matches “nobody”.')).toBeInTheDocument();
  });
});
