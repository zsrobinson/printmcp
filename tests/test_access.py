"""Real RS256 signatures against a generated local JWKS; never contact Access."""
import io
import json
import time
import urllib.request
from urllib.error import URLError
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
import pytest
from starlette.testclient import TestClient

import server

pytestmark = pytest.mark.usefixtures("cups")

TEAM = 'test-team.cloudflareaccess.com'
AUD = 'test-print-audience'
BASE = 'http://127.0.0.1:8765'
HEADERS = {'Accept': 'application/json, text/event-stream'}
CALL = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'list_jobs', 'arguments': {}}}


@pytest.fixture(scope='module')
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def access(monkeypatch, key):
    monkeypatch.setenv('ACCESS_TEAM_DOMAIN', TEAM)
    monkeypatch.setenv('ACCESS_AUD', AUD)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk.update(kid='local-key', alg='RS256', use='sig')
    body = json.dumps({'keys': [jwk]}).encode()
    def fetch(request, timeout):
        assert request.full_url == f'https://{TEAM}/cdn-cgi/access/certs'
        assert timeout == 5
        return io.BytesIO(body)
    network = Mock(side_effect=fetch)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', network)
    return network


def token(key, **claims):
    payload = {'iss': f'https://{TEAM}', 'aud': [AUD], 'exp': int(time.time()) + 300}
    payload.update(claims)
    return jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'local-key'})


def test_valid_and_cached(access, key, cups):
    cups.side_effect = None
    cups.return_value = server.subprocess.CompletedProcess([], 0, 'epson-7 Owner 123', '')
    headers = {**HEADERS, 'Cf-Access-Jwt-Assertion': token(key)}
    with TestClient(server.create_app(), base_url=BASE) as client:
        for _ in range(2):
            response = client.post('/mcp', headers=headers, json=CALL)
            assert response.status_code == 200 and 'epson-7' in response.text
    assert cups.call_count == 2
    assert access.call_count == 1  # Second request uses the cached JWKS.


@pytest.mark.parametrize('kind', ['missing', 'wrong-aud', 'expired', 'bad-signature',
                                  'wrong-issuer', 'missing-exp', 'malformed', 'wrong-alg'])
def test_rejected_before_tools(access, key, cups, kind):
    headers = dict(HEADERS)
    if kind == 'wrong-aud':
        assertion = token(key, aud='different-app')
    elif kind == 'expired':
        assertion = token(key, exp=int(time.time()) - 60)
    elif kind == 'bad-signature':
        assertion = token(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    elif kind == 'wrong-issuer':
        assertion = token(key, iss='https://other-team.cloudflareaccess.com')
    elif kind == 'missing-exp':
        assertion = jwt.encode({'iss': f'https://{TEAM}', 'aud': AUD}, key,
                               algorithm='RS256', headers={'kid': 'local-key'})
    elif kind == 'malformed':
        assertion = 'not-a-jwt'
    elif kind == 'wrong-alg':
        assertion = jwt.encode({'aud': AUD}, 'x' * 32, algorithm='HS256')
    elif kind == 'missing':
        assertion = None
    if assertion:
        headers['Cf-Access-Jwt-Assertion'] = assertion
    with TestClient(server.create_app(), base_url=BASE) as client:
        response = client.post('/mcp', headers=headers, json=CALL)
        assert response.status_code == 401
        # Guard every HTTP method, including streaming and session cleanup.
        assert client.get('/mcp', headers=headers).status_code == 401
        assert client.delete('/mcp', headers=headers).status_code == 401
    cups.assert_not_called()
    if kind in ('missing', 'malformed', 'wrong-alg'):
        access.assert_not_called()


def test_jwks_outage_fails_closed(access, key, cups):
    access.side_effect = URLError('offline')
    with TestClient(server.create_app(), base_url=BASE) as client:
        response = client.post('/mcp', headers={**HEADERS, 'Cf-Access-Jwt-Assertion': token(key)},
                               json=CALL)
    assert response.status_code == 401 and 'offline' not in response.text
    cups.assert_not_called()


@pytest.mark.parametrize('settings', [
    {'ACCESS_TEAM_DOMAIN': TEAM}, {'ACCESS_AUD': AUD},
    {'ACCESS_TEAM_DOMAIN': 'https://bad.example/path', 'ACCESS_AUD': AUD},
])
def test_bad_configuration_fails_startup(monkeypatch, settings):
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        server.create_app()


def test_unconfigured_warns(caplog):
    server.create_app()
    assert 'Access JWT validation is DISABLED' in caplog.text
