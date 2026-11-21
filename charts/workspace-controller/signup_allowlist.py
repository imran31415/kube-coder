"""Runtime signup allowlist: who may create a workspace at public signup.

The effective list is the Helm seed (`signup.allowedGithubIds`: read-only,
break-glass) united with the entries an admin adds in the console. Console
entries live in one ConfigMap in the signup state namespace and are written
with the store's optimistic concurrency, so both controller replicas see a
change without a restart. Entries are keyed by numeric GitHub ID; a login is
for display only, because usernames can be renamed and recycled.
"""
import json
import logging
import random
import re
import threading
import time

from observability_logging import event
from observability_metrics import REGISTRY
from provision_store import Conflict
from signup_errors import SignupError

RECORD = 'signup-allowlist'
MAX_ENTRIES = 1000   # default console entries; Helm signup.allowlist.maxEntries overrides
NOTE_MAX = 200
# Kubernetes refuses a ConfigMap whose data exceeds 1 MiB. Stop short of it,
# whatever maxEntries says, so a big list answers "full" — not a storage error.
MAX_RECORD_BYTES = 1_000_000
CACHE_SECONDS = 5.0
# Writes from the two replicas can conflict. Each replica writes one at a time
# and retries after a short random pause; on a real cluster, 20 simultaneous
# adds across both replicas all landed (the worst needed 5 of 8 attempts).
WRITE_ATTEMPTS = 8
RETRY_PAUSE = 0.05   # seconds, scaled by the attempt number: <= 1.4 s in total
_GITHUB_ID = re.compile(r'[1-9][0-9]{0,19}')


def valid_id(value):
    return isinstance(value, str) and bool(_GITHUB_ID.fullmatch(value))


class Allowlist:
    """Helm seed plus stored entries.

    Reads are cached for a few seconds per replica. A store failure fails
    closed: it raises `state_unavailable` and never reads as an empty list or
    as an older copy, so a removal can't be undone by an outage.
    """

    def __init__(self, store, seed_ids, max_entries=MAX_ENTRIES, ttl=CACHE_SECONDS, clock=time.monotonic):
        self.store, self.seed, self.max_entries = store, frozenset(seed_ids), max_entries
        self._ttl, self._clock = ttl, clock
        self._lock = threading.Lock()
        # One writer per replica at a time: requests on the same replica queue
        # instead of all colliding on the one record, so only the two replicas
        # can conflict with each other — which the retry pause absorbs.
        self._write_lock = threading.Lock()
        self._cached, self._expires = None, 0.0

    def allows(self, github_id, fresh=False):
        """`fresh` skips the cache: creation paths use it so a removal on the
        other replica blocks new workspaces at once."""
        return github_id in self.seed or github_id in self._stored(fresh)

    def entries(self):
        """Console entries newest first, then the Helm seed by ID. An ID in both
        is shown as `helm` (removing it in the console would change nothing)."""
        stored = self._stored(fresh=True)
        console = [dict(meta, githubId=gid, source='console')
                   for gid, meta in stored.items() if gid not in self.seed]
        console.sort(key=lambda entry: entry.get('addedAt') or 0, reverse=True)
        helm = [dict(stored.get(gid, {}), githubId=gid, source='helm')
                for gid in sorted(self.seed, key=int)]
        return console + helm

    def add(self, github_id, login, actor, note=''):
        """Returns (entry, created). Adding someone already allowed is a no-op."""
        if not valid_id(github_id) or not isinstance(note, str) or len(note) > NOTE_MAX:
            raise SignupError('invalid_request')

        def change(entries):
            if github_id in entries or github_id in self.seed:
                return False, False
            if self._console_count(entries) >= self.max_entries:
                raise SignupError('allowlist_full')
            entries[github_id] = {'login': login, 'addedBy': actor,
                                  'addedAt': time.time(), 'note': note}
            size = len(json.dumps({'entries': entries}))   # as KubeStore.save writes it
            if size > MAX_RECORD_BYTES:
                event('controller', 'signup_allowlist_too_large', logging.WARNING,
                      bytes=size, entries=len(entries), max_bytes=MAX_RECORD_BYTES)
                raise SignupError('allowlist_full')
            return True, True

        created, entries = self._update(change)
        if created:
            event('controller', 'signup_allowlist_added', github_id=github_id, login=login, actor=actor)
        source = 'helm' if github_id in self.seed else 'console'
        return dict(entries.get(github_id, {}), githubId=github_id, source=source), created

    def remove(self, github_id, actor):
        """Stops new workspace creation only. Never touches an existing
        workspace, its owner record or its Dex client. Returns the removed
        entry, or None if the ID was not on the console list."""
        if not valid_id(github_id):
            raise SignupError('invalid_request')
        if github_id in self.seed:
            raise SignupError('allowlist_readonly')
        def change(entries):
            if github_id not in entries:
                return False, None
            return True, entries.pop(github_id)

        removed, _ = self._update(change)
        if removed is not None:
            event('controller', 'signup_allowlist_removed', github_id=github_id,
                  login=removed.get('login'), actor=actor)
        return removed

    def _update(self, change):
        """Read, apply `change(entries) -> (changed, result)`, then
        compare-and-set save. A concurrent write from the other replica makes
        the save conflict; re-read and apply again. Returns (result, entries)."""
        with self._write_lock:
            return self._compare_and_set(change)

    def _compare_and_set(self, change):
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            record = self.store.get(RECORD) or {'entries': {}}
            entries = self._entries_of(record)
            changed, result = change(entries)
            if not changed:
                self._remember(entries)
                return result, entries
            try:
                saved = self.store.save(RECORD, record)
            except Conflict:
                event('controller', 'signup_allowlist_retry', attempt=attempt)
                if attempt < WRITE_ATTEMPTS:
                    time.sleep(random.uniform(0, RETRY_PAUSE * attempt))
                continue
            self._remember(saved['entries'])
            return result, saved['entries']
        raise SignupError('busy')

    def _stored(self, fresh):
        with self._lock:
            if not fresh and self._cached is not None and self._clock() < self._expires:
                return self._cached
        entries = self._entries_of(self.store.get(RECORD) or {'entries': {}})
        self._remember(entries)
        return entries

    def _remember(self, entries):
        with self._lock:
            self._cached, self._expires = entries, self._clock() + self._ttl
        REGISTRY.allowlist_size(len(self.seed), self._console_count(entries))

    def _console_count(self, entries):
        """Entries the console added. The cap, the page and the metric all count
        these; a stored ID that is also in the Helm seed is not one of them."""
        return sum(1 for gid in entries if gid not in self.seed)

    @staticmethod
    def _entries_of(record):
        entries = record.get('entries')
        if not isinstance(entries, dict) or not all(
                valid_id(gid) and isinstance(meta, dict) for gid, meta in entries.items()):
            # A hand edit broke the ConfigMap. Refuse rather than guess: writing
            # back would silently drop whatever the admin meant to keep.
            event('controller', 'signup_allowlist_invalid', logging.WARNING, record=RECORD)
            raise SignupError('state_unavailable')
        return entries


class AllowlistConsole:
    """The admin console's use cases. Returns plain dicts; raises SignupError."""

    def __init__(self, service, signup_url=''):
        self.service, self.allowlist, self.signup_url = service, service.allowlist, signup_url

    def overview(self):
        workspaces = self._workspaces()
        entries = []
        for entry in self.allowlist.entries():
            workspace = workspaces.get(entry['githubId'])
            login = entry.get('login') or (workspace or {}).get('login')
            entries.append(dict(entry, login=login, workspace=_public(workspace)))
        return {'entries': entries, 'limit': self.allowlist.max_entries, 'noteMax': NOTE_MAX,
                'signupUrl': self.signup_url}

    def lookup(self, login):
        info = self._account(login)
        github_id = str(info['id'])
        source = 'helm' if github_id in self.allowlist.seed else (
            'console' if self.allowlist.allows(github_id, fresh=True) else None)
        return {'githubId': github_id, 'login': info['login'], 'name': info.get('name') or '',
                'avatarUrl': info.get('avatar_url') or '', 'allowed': source is not None,
                'source': source, 'workspace': _public(self.service.get(github_id))}

    def add(self, body, actor):
        if not isinstance(body, dict) or set(body) - {'login', 'githubId', 'note'}:
            raise SignupError('invalid_request')
        login, github_id = body.get('login'), body.get('githubId')
        note = body.get('note', '')
        if not isinstance(login, str) or not valid_id(github_id) or not isinstance(note, str):
            raise SignupError('invalid_request')
        # Resolve again: the username may have changed hands since the preview.
        info = self._account(login.strip())
        if str(info['id']) != github_id:
            raise SignupError('github_user_changed')
        entry, created = self.allowlist.add(github_id, info['login'], actor, note.strip())
        return {'entry': dict(entry, workspace=_public(self.service.get(github_id))), 'created': created}

    def remove(self, github_id, actor):
        return {'removed': self.allowlist.remove(github_id, actor) is not None}

    def _account(self, login):
        info = self.service.backend.lookup(login)
        if info.get('type') != 'User':
            raise SignupError('not_a_person')
        return info

    def _workspaces(self):
        """Owner records by GitHub ID, from one LIST call."""
        return {record['owner']: record for _, record in self.service.store.operations()}


def _public(record):
    if not record or record.get('stage') == 'deleted':
        return None
    return {'slug': record['slug'], 'stage': record['stage']}
