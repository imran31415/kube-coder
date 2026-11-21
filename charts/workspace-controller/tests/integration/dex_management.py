"""Exercise the production adapter against real Dex, including missing client TLS."""
import pathlib
import sys
import uuid
import grpc

root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from dex_client import DexClient
from signup_errors import SignupError
import dex_api_pb2 as messages
import dex_api_pb2_grpc as bindings

certs = pathlib.Path(__file__).resolve().parent / '.certs'
client = DexClient('127.0.0.1:5557', str(certs))
identifier = 'fixture-' + uuid.uuid4().hex
try:
    client.ensure(identifier, 'fixture-secret', 'https://owner.test/oauth2/callback')
    client.ensure(identifier, 'fixture-secret', 'https://owner.test/oauth2/callback')
    try:
        client.ensure(identifier, 'wrong-secret', 'https://owner.test/oauth2/callback')
    except SignupError as exc:
        assert exc.code == 'identity_conflict'
    else:
        raise AssertionError('Existing identity must not be overwritten')
    with grpc.secure_channel('127.0.0.1:5557', grpc.ssl_channel_credentials((certs / 'ca.crt').read_bytes())) as anonymous:
        try:
            bindings.DexStub(anonymous).GetClient(messages.GetClientReq(id=identifier), timeout=3)
        except grpc.RpcError:
            pass
        else:
            raise AssertionError('Management API accepted a caller without a client certificate')
    client.delete(identifier)
    client.delete(identifier)
    print('PASS: mTLS management, create/replay, conflict protection, idempotent delete, anonymous denial')
finally:
    client.delete(identifier)
    client.channel.close()
