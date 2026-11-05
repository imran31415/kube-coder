"""Strict-auth socket fixture for the browser acceptance test.

Only external OAuth, tracker and agent execution are deterministic adapters.
Production API handlers, bearer verification, Keeper decisions, JsonRecord
locks, runner, staging and review decisions are real. Never use live credentials.
"""
import argparse
import copy
import http.server
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True)
    parser.add_argument('--keeper', required=True)
    args = parser.parse_args()
    state = Path(args.state).resolve()
    state.mkdir(parents=True, exist_ok=True)
    workspace = Path(__file__).resolve().parents[1]
    os.environ.update(KC_WORKSPACE_HOME=str(state), AUTH_MODE='oauth2', TRUSTED_PROXY='true',
                      KC_BOARD_HANDOFF_ENABLED='true', KEEPER_CONTROL_SECRET='fixture-control',
                      DASHBOARD_DIST_DIR=str(workspace / 'web' / 'dist'), KC_MAX_TASKS='1')
    sys.path.insert(0, str(workspace))
    import server
    import hypervisor_session as hv
    import approval_auth
    hv.WORKSPACE_HOME = str(state)
    hv.HYPERVISOR_DIR = str(state / '.claude-tasks' / 'hypervisor')
    server.HYPERVISOR_DIR = hv.HYPERVISOR_DIR
    from handlers import board_handoff
    from boards import templates
    from boards.store import real_flock
    from tests.boards_e2e_test import VENDOR, _VendorHandler
    if not real_flock():
        raise RuntimeError('Browser acceptance must run on Linux with real flock')
    for manager in (server.BoardsManager, server.BoardCredentialsManager,
                    server.ProjectsManager, server.WorktreeManager):
        manager.HOME_ROOT = str(state)
    server.ProjectsManager.PROJECTS_DIR = str(state / '.claude-projects')
    server.FeedManager.emit = lambda *a, **kw: None  # unrelated product feed, outside fixture
    server.HYPERVISOR_ENABLED = True
    server.READONLY_MODE = False
    # The deterministic agent adapter does not use provider credentials.
    server.SubscriptionStatusManager.claude_credential_present = classmethod(lambda cls: True)
    server.BoardRunsManager.POLL_INTERVAL = .1
    server.ClaudeTaskManager.TASKS_DIR = str(state / '.claude-tasks')
    server.ClaudeTaskManager.TOKEN_FILE = str(state / '.claude-tasks' / '.api-token')
    server.ClaudeTaskManager.get_or_create_token()
    token = Path(server.ClaudeTaskManager.TOKEN_FILE).read_text().strip()
    stats = {'workers': 0, 'errors': [], 'mcp_calls': 0}
    statuses = {}

    def start_http(handler):
        httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, f'http://127.0.0.1:{httpd.server_address[1]}'

    vendor, vendor_url = start_http(_VendorHandler)
    backend, backend_url = start_http(server.BrowserHandler)
    repo = state / 'api'
    repo.mkdir(exist_ok=True)
    if not (repo / '.git').exists():
        (repo / 'README.md').write_text('# Disposable Board fixture\n')
        for command in (['git', 'init', '-q', str(repo)], ['git', '-C', str(repo), 'add', '.'],
                        ['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
                         'commit', '-qm', 'Fixture'], ['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/acme/api.git']):
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    server.ProjectsManager.ensure_dir()
    Path(server.ProjectsManager._config_path('api')).write_text(json.dumps(dict(
        id='api', name='API', repo='https://github.com/acme/api', workdirs=[str(repo)])))
    cfg = copy.deepcopy(templates.GITHUB_ISSUES)
    cfg.update(id='e2e', display_name='Billing backlog', base_url=vendor_url,
               credential_ref='@board-creds/VENDOR_TOKEN', allow_internal=True)
    cfg['list']['request']['url'] = '${base_url}/repos/acme/support/issues'
    cfg['list']['request']['query'] = {'state': 'all', 'per_page': '50'}
    cfg['map']['ref'] = {'owner': {'template': 'acme'}, 'repo': {'template': 'support'}, 'number': 'number'}
    server.BoardCredentialsManager.set('VENDOR_TOKEN', 'fixture-vendor-token')
    _, error = server.BoardsManager.create_or_update(cfg)
    if error:
        raise RuntimeError(error)

    class Gateway(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_data(self, data, status=200, headers=None):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            for key, value in (headers or {}).items(): self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == '/oauth2/auth':
                if self.headers.get('Cookie') == 'fixture-session=human':
                    self.send_data({}, 202, {'X-Auth-Request-User': 'fixture-human'})
                else:
                    self.send_data({'error': 'sign in'}, 401)
                return
            if self.path == '/__test__/stats':
                return self.send_data(dict(stats, vendor_reads=sum(m == 'GET' for m, _ in VENDOR.requests),
                    vendor_writes=sum(m != 'GET' for m, _ in VENDOR.requests),
                    run_count=len(server.BoardRunsManager.list_runs('e2e')),
                    peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
            self.proxy()

        def do_POST(self):
            self.proxy()

        def proxy(self):
            # Models the existing authenticated ingress. The backend still runs
            # its real auth methods; the decision authority revalidates cookies.
            if self.headers.get('Cookie') != 'fixture-session=human':
                return self.send_data({'error': 'sign in'}, 401)
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in ('host', 'authorization', 'connection', 'x-auth-request-user', 'remote-user')}
            headers['X-Auth-Request-User'] = 'fixture-human'
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(backend_url + self.path, data=body, method=self.command, headers=headers)
            try:
                response = urllib.request.urlopen(req, timeout=30)
            except urllib.error.HTTPError as exc:
                response = exc
            try:
                with response:
                    self.send_response(response.status)
                    for key, value in response.headers.items():
                        if key.lower() not in ('connection', 'transfer-encoding', 'server', 'date'):
                            self.send_header(key, value)
                    self.end_headers()
                    if 'text/event-stream' in response.headers.get('Content-Type', ''):
                        while True:
                            chunk = response.read1(4096)
                            if not chunk: break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    else:
                        self.wfile.write(response.read())
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    gateway, origin = start_http(Gateway)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        keeper_port = sock.getsockname()[1]
    authority = state / 'authority'
    authority.mkdir(exist_ok=True)
    control_file = authority / 'control-secret'
    control_file.write_text('fixture-control')
    keeper_env = dict(os.environ, KEEPER_STATE_DIR=str(authority / 'decisions'),
        KEEPER_BOARD_AUTH_URL=origin+'/oauth2/auth', KEEPER_BOARD_ORIGIN=origin,
        KEEPER_CONTROL_SECRET_FILE=str(control_file), KEEPER_BOARD_PORT=str(keeper_port))
    keeper = subprocess.Popen([args.keeper, 'board-approval-local'], env=keeper_env)
    os.environ['KC_BOARD_APPROVAL_URL'] = f'http://127.0.0.1:{keeper_port}/api/keeper/board-approvals'
    for _ in range(100):
        try:
            approval_auth.authority_call('human', {}, {'Cookie': 'fixture-session=human', 'Origin': origin})
            break
        except Exception:
            if keeper.poll() is not None: raise RuntimeError('Keeper exited during startup')
            time.sleep(.05)
    else:
        raise RuntimeError('Keeper did not become ready')

    def call(path, body, *, worker=None):
        headers = {'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'}
        if worker: headers['X-KC-Board-Worker'] = worker
        req = urllib.request.Request(backend_url+path, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)

    def create_task(prompt, **kwargs):
        stats['workers'] += 1
        task_id = 'task-'+str(stats['workers'])
        statuses[task_id] = 'running'
        context = approval_auth.issue_worker_context(kwargs['board_run_context'], kwargs['board_id'], str(kwargs['board_item_id']))
        def complete():
            try:
                time.sleep(1.5)  # expose starting/running UI, after the driver records task ID
                path = f"/api/boards/e2e/items/{kwargs['board_item_id']}"
                call(path+'/actions', {'action': 'comment', 'params': {'body': 'Disposable fixture proposal'}}, worker=context)
                call(path+'/disposition', {'disposition': 'needs_review', 'reason': 'Review the fixture proposal.'}, worker=context)
                statuses[task_id] = 'completed'
            except Exception as exc:
                stats['errors'].append(type(exc).__name__ + ': ' + str(exc))
                statuses[task_id] = 'error'
        threading.Thread(target=complete, daemon=True).start()
        return {'task_id': task_id, 'status': 'running'}
    server.ClaudeTaskManager.create_task = staticmethod(create_task)
    server.ClaudeTaskManager.task_status = staticmethod(lambda task_id: statuses.get(task_id))
    server.ClaudeTaskManager.count_live_tasks = staticmethod(lambda: sum(s == 'running' for s in statuses.values()))

    def deterministic_turn(self, text, first, meta):
        try:
            context = approval_auth.issue_context(self.id)
            env = dict(os.environ, KC_DASHBOARD_URL=backend_url, KC_RUNNER_CONTEXT=context,
                       KC_HYPERVISOR_THREAD_ID=self.id)
            body = dict(board_id='e2e', repository='https://github.com/acme/api',
                        options=dict(workdir=str(repo), isolate=True, select={'limit': 1}, concurrency=1))
            request = dict(jsonrpc='2.0', id=1, method='tools/call',
                           params={'name': 'request_board_run', 'arguments': body})
            result = subprocess.run([sys.executable, str(workspace/'mcp_dashboard.py')],
                input=json.dumps(request)+'\n', capture_output=True, text=True, env=env, timeout=15, check=True)
            parsed = json.loads(result.stdout.strip())
            if parsed.get('error') or parsed.get('result', {}).get('isError'):
                raise RuntimeError(result.stdout[:1000])
            stats['mcp_calls'] += 1
            self._append([{'role': 'assistant', 'type': 'message',
                           'text': 'The Board request is ready. Review the card and choose Approve & run.'}])
        except Exception as exc:
            stats['errors'].append(str(exc))
            self._append([{'role': 'system', 'type': 'error', 'text': 'Fixture agent could not prepare the request.'}])
        finally:
            with hv._RUNLOCK: hv._RUNNING.pop(self.id, None)
            current = self.read_meta() or meta
            current['status'] = 'idle'
            self._write_meta(current)
    hv.HypervisorSession._run_turn = deterministic_turn
    previous = hv.HypervisorSession.list()
    session = (hv.HypervisorSession.get(previous[0]['id']) if previous else
               hv.HypervisorSession.create('claude', str(repo), 'claude', title='Board handoff acceptance'))
    server.BoardRunsManager.start()
    board_handoff.manager().recover()
    print('E2E_READY '+json.dumps(dict(origin=origin, backend_url=backend_url,
        thread_id=session.id, token=token)), flush=True)  # synthetic test token only, captured by harness
    try:
        sys.stdin.read()  # harness closes stdin to stop; no orphan subprocesses
    finally:
        keeper.terminate()
        keeper.wait(timeout=10)
        for httpd in (gateway, backend, vendor):
            httpd.shutdown()
            httpd.server_close()


if __name__ == '__main__':
    main()
