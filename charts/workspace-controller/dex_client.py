"""Dex API boundary. Imports are lazy so legacy controllers stay stdlib-only."""
import hmac
from observability_logging import event
from signup_errors import SignupError


class DexClient:
    def __init__(self, address, cert_dir):
        import grpc
        import dex_api_pb2 as messages
        import dex_api_pb2_grpc as bindings
        self.grpc, self.messages = grpc, messages
        def read(name):
            with open(cert_dir + '/' + name, 'rb') as f:
                return f.read()
        credentials = grpc.ssl_channel_credentials(read('ca.crt'), read('tls.key'), read('tls.crt'))
        self.channel = grpc.secure_channel(address, credentials)
        self.api = bindings.DexStub(self.channel)

    def call(self, method, request):
        try:
            return getattr(self.api, method)(request, timeout=10)
        except self.grpc.RpcError as exc:
            event('controller', 'dex_rpc_failed', method=method, rpc_code=exc.code().name)
            raise SignupError('dex_unavailable') from None

    def ensure(self, client_id, secret, callback):
        m = self.messages
        # Create is idempotent only after verifying the existing full identity.
        result = self.call('CreateClient', m.CreateClientReq(client=m.Client(
            id=client_id, secret=secret, redirect_uris=[callback], name='Kube Coder workspace')))
        if result.already_exists:
            current = self.call('GetClient', m.GetClientReq(id=client_id))
            client = current.client
            if (list(client.redirect_uris) != [callback]
                    or not hmac.compare_digest(client.secret, secret)
                    or client.public or client.trusted_peers):
                raise SignupError('identity_conflict')

    def delete(self, client_id):
        self.call('DeleteClient', self.messages.DeleteClientReq(id=client_id))
