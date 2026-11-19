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
