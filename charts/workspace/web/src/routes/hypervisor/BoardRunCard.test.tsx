import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/preact';
import type { BoardRequest } from '../../api/boardRequests';
import { createBoardRequestsStore } from '../../store/boardRequests';
import { BoardRunCard } from './BoardRunCard';
import { boardRunPath, parseBoardRunPath } from '../board/runLinks';
import { serverMode } from '../../store/server-mode';

vi.mock('../../api/boardRequests', () => ({
  decideBoardRequest: vi.fn(), listBoardRequests: vi.fn(), prepareBoardRequest: vi.fn(),
}));
import { decideBoardRequest, listBoardRequests, prepareBoardRequest } from '../../api/boardRequests';
const decide = vi.mocked(decideBoardRequest);
const list = vi.mocked(listBoardRequests);
const prepare = vi.mocked(prepareBoardRequest);

function fixture(overrides: Partial<BoardRequest> = {}): BoardRequest {
  return { id:'brq-'+ 'a'.repeat(32), thread_id:'thread-1', board_id:'billing', revision:'revision', version:1,
    state:'pending', created_at:1, expires_at:900, run_id:null, decision_id:null, error:{},
    scope:{ board_id:'billing', board_name:'Billing backlog', repository:'https://github.com/acme/api',
      operation:'start', options:{ select:{limit:3}, concurrency:1, workdir:'/home/dev/api', base_ref:'', isolate:true, mode:'propose' } }, ...overrides };
}
afterEach(() => { cleanup(); vi.resetAllMocks(); sessionStorage.clear(); });
beforeEach(() => { serverMode.value = {...serverMode.value, readOnly:false}; });

describe('Board permission card', () => {
  it('does not offer decisions in a read-only workspace', () => {
    serverMode.value = {...serverMode.value, readOnly:true};
    render(<BoardRunCard record={fixture()} store={createBoardRequestsStore('thread-1')} />);
    expect(screen.queryByRole('button', {name:'Approve & run'})).not.toBeInTheDocument();
    expect(screen.getByText(/Open a writable workspace/)).toBeInTheDocument();
  });
  it('does not link a failed reservation to a nonexistent run', () => {
    render(<BoardRunCard record={fixture({state:'failed',run_id:'run-1-abc',error:{error:'All workers are busy.'}})} store={createBoardRequestsStore('thread-1')} />);
    expect(screen.queryByRole('link', {name:'Open Board'})).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('All workers are busy.');
  });
  it('shows bounded scope without approving anything on render', () => {
    render(<BoardRunCard record={fixture()} store={createBoardRequestsStore('thread-1')} />);
    expect(screen.getByRole('button', {name:'Approve & run'})).toBeEnabled();
    expect(screen.getByText(/Up to 3 items/)).toBeInTheDocument();
    expect(screen.getByText(/No push or merge is approved/)).toBeInTheDocument();
    expect(decide).not.toHaveBeenCalled();
  });
  it('cancels without requesting a run', async () => {
    decide.mockResolvedValue(fixture({state:'rejected',version:2}));
    render(<BoardRunCard record={fixture()} store={createBoardRequestsStore('thread-1')} />);
    await act(async()=>{ fireEvent.click(screen.getByRole('button',{name:'Cancel'})); });
    expect(decide.mock.calls[0][1]).toBe('reject');
    expect(prepare).not.toHaveBeenCalled();
  });
  it('disables all decisions while saving and preserves one decision ID on retry', async () => {
    const store=createBoardRequestsStore('thread-1');
    let reject!: (reason: Error)=>void;
    decide.mockReturnValueOnce(new Promise((_, fail)=>{reject=fail;}));
    render(<BoardRunCard record={fixture()} store={store} />);
    await act(async()=>{ fireEvent.click(screen.getByRole('button',{name:'Approve & run'})); });
    expect(screen.getByRole('button',{name:'Saving decision…'})).toBeDisabled();
    expect(screen.getByRole('button',{name:'Cancel'})).toBeDisabled();
    await act(async()=>reject(new Error('connection lost')));
    expect(store.busy.value).toBeNull();
    expect(store.error.value).toMatch(/Refresh status/);
    decide.mockResolvedValueOnce(fixture({state:'started',version:3}));
    await store.decide(fixture(),'approve');
    expect(decide.mock.calls[0][2]).toBe(decide.mock.calls[1][2]);
  });
  it('adjusts by rejecting the original scope and preparing another pending request', async () => {
    decide.mockResolvedValue(fixture({state:'rejected',version:2}));
    prepare.mockResolvedValue(fixture({id:'brq-'+ 'b'.repeat(32)}));
    const store=createBoardRequestsStore('thread-1');
    await store.adjust(fixture(),2,1);
    expect(decide.mock.calls[0][1]).toBe('reject');
    expect(prepare.mock.calls[0][0]).toMatchObject({thread_id:'thread-1', options:{select:{limit:2}}});
    expect(store.requests.value[0].state).toBe('pending');
  });
  it('does not turn a completed run with failed items into successful work', () => {
    const record=fixture({state:'started',version:3,run_id:'run-1-abc',review_needed:1,
      run:{id:'run-1-abc',board_id:'billing',status:'done',counts:{pending:0,working:0,claimed:0,done:1,failed:1,skipped:1},failed:1,skipped:1} as BoardRequest['run']});
    render(<BoardRunCard record={record} store={createBoardRequestsStore('thread-1')} />);
    expect(screen.getByText(/1 failed/)).toBeInTheDocument();
    expect(screen.getByText('1 item needs review.')).toBeInTheDocument();
    expect(screen.getByRole('link',{name:'Open Board'})).toHaveAttribute('href','/board/billing/runs/run-1-abc');
  });
  it('explains stop semantics and interrupted startup', () => {
    const record=fixture(); record.scope.operation='stop';
    render(<BoardRunCard record={record} store={createBoardRequestsStore('thread-1')} />);
    expect(screen.getByText('Stop starting new items. Running items will finish.')).toBeInTheDocument();
    expect(screen.getByRole('button',{name:'Approve stop'})).toBeInTheDocument();
  });
});

describe('authoritative status and routing',()=>{
  it('keeps last good progress and timestamp when a refresh fails',async()=>{
    const store=createBoardRequestsStore('thread-1');
    list.mockResolvedValueOnce({requests:[fixture()]});await store.refresh();
    const checked=store.lastChecked.value;
    list.mockRejectedValueOnce(new Error('offline'));await store.refresh();
    expect(store.requests.value).toHaveLength(1);
    expect(store.lastChecked.value).toBe(checked);
    expect(store.error.value).toBeTruthy();
  });
  it('does not overwrite a decision with an older list response',async()=>{
    const store=createBoardRequestsStore('thread-1');
    let resolve!: (value:{requests:BoardRequest[]})=>void;
    list.mockReturnValueOnce(new Promise(done=>{resolve=done;}));
    const refresh=store.refresh();
    decide.mockResolvedValueOnce(fixture({state:'starting',version:2,run_id:'run-1-abc'}));
    await store.decide(fixture(),'approve');
    resolve({requests:[fixture()]});await refresh;
    expect(store.requests.value[0].state).toBe('starting');
  });
  it('encodes Board IDs and rejects malformed run links',()=>{
    expect(parseBoardRunPath(boardRunPath('billing team','run-1-abc'))).toEqual({boardId:'billing team',runId:'run-1-abc'});
    expect(parseBoardRunPath('/board/%ZZ/runs/run-1-abc')).toBeNull();
    expect(parseBoardRunPath('/board/billing/runs/../../secret')).toBeNull();
  });
});
