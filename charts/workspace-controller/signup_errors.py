"""Public onboarding errors: stable codes, safe messages, no upstream bodies."""
from observability_logging import CONTEXT, event
import logging
import uuid

ERRORS = {
    'unauthorized': (401, 'Sign in with GitHub to continue.', False),
    'not_eligible': (403, "Your GitHub account isn't approved yet.", False),
    'forbidden': (403, 'This request is not allowed. Refresh and try again.', False),
    'invalid_request': (400, 'Check the request and try again.', False),
    'owner_conflict': (409, 'This workspace address is already reserved.', False),
    'identity_conflict': (409, 'Workspace sign-in configuration needs administrator attention.', False),
    'not_configured': (503, 'Workspace signup is not available yet.', False),
    'dex_unavailable': (503, 'Sign-in setup is temporarily unavailable. Try again.', True),
    'state_unavailable': (503, 'Workspace progress is temporarily unavailable. Try again.', True),
    'gitops_failed': (502, 'Workspace configuration could not be saved. Try again.', True),
    'deploy_failed': (502, 'Workspace deployment failed. Try again.', True),
    'readiness_timeout': (503, 'Your workspace is taking longer than expected. Check again.', True),
    'busy': (429, 'Workspace setup is busy. Try again shortly.', True),
    'internal_error': (500, 'Workspace setup could not be completed. Try again.', True),
    # Admin console: the runtime signup allowlist.
    'github_user_not_found': (404, 'No GitHub account has that username.', False),
    'github_unavailable': (502, "GitHub didn't respond. Try again.", True),
    'not_a_person': (422, "That's an organization or bot account. Only personal accounts can sign up.", False),
    'github_user_changed': (409, 'That username now belongs to a different account. Look it up again.', False),
    'allowlist_readonly': (409, 'This person is allowed in the Helm values. Remove them there.', False),
    'allowlist_full': (409, 'The list is full. Remove someone first.', False),
}


class SignupError(RuntimeError):
    def __init__(self, code):
        self.code = code
        self.status, message, self.retryable = ERRORS[code]
        super().__init__(message)

    def public(self, **fields):
        error_id = uuid.uuid4().hex[:12]
        event('controller', self.code, logging.WARNING, error_id=error_id, **fields)
        return {'error': str(self), 'code': self.code, 'retryable': self.retryable,
                'error_id': error_id, 'request_id': CONTEXT.get().get('request_id')}
