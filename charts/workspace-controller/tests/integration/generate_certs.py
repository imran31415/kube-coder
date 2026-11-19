"""Generate short-lived test-only mTLS material using the existing OpenSSL CLI."""
import os
import pathlib
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parent
certs = root / '.certs'
certs.mkdir(exist_ok=True)
openssl = sys.argv[1] if len(sys.argv) > 1 else 'openssl'

def run(*args):
    subprocess.run([openssl, *args], cwd=certs, check=True, capture_output=True)

run('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
    '-subj', '/CN=fixture-ca', '-keyout', 'ca.key', '-out', 'ca.crt')
for name, extension in [('server', 'subjectAltName=IP:127.0.0.1,DNS:localhost\nextendedKeyUsage=serverAuth\n'),
                        ('tls', 'extendedKeyUsage=clientAuth\n')]:
    (certs / (name + '.ext')).write_text(extension)
    run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=fixture-' + name,
        '-keyout', name + '.key', '-out', name + '.csr')
    run('x509', '-req', '-days', '2', '-in', name + '.csr', '-CA', 'ca.crt',
        '-CAkey', 'ca.key', '-CAcreateserial', '-extfile', name + '.ext', '-out', name + '.crt')
    # Container Dex is non-root; this directory contains test-only keys.
    os.chmod(certs / (name + '.key'), 0o644)
(certs / 'dex.yaml').write_text((root / 'dex.yaml').read_text() + '''
grpc:
  addr: 0.0.0.0:5557
  tlsCert: /certs/server.crt
  tlsKey: /certs/server.key
  tlsClientCA: /certs/ca.crt
''')
print('Generated two-day fixture certificates; never use these in production.')
