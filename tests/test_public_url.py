import io
import socket
from unittest.mock import Mock

import pytest

import server
from test_url import PDF, download  # noqa: F401

REAL_CONNECTION = server._PinnedHTTPSConnection
REAL_CONNECT = REAL_CONNECTION.connect


def answer(address, port=443):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    endpoint = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', endpoint


@pytest.mark.parametrize('address', [
    '127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1', '169.254.169.254',
    '100.64.0.1', '100.127.255.254', '0.0.0.0', '224.0.0.1', '240.0.0.1',
    '192.0.2.1', '192.0.0.8', '198.18.0.1', '::1', '::', 'fc00::1', 'fd00::1', 'fe80::1',
    'fec0::1', 'ff02::1', '2001:db8::1', '::ffff:127.0.0.1', '::ffff:8.8.8.8',
    '2002:7f00:1::', '64:ff9b::7f00:1',
])
def test_non_global_addresses_never_connect(download, monkeypatch, cups, address):
    monkeypatch.setattr(server.socket, 'getaddrinfo', Mock(return_value=[answer(address)]))
    with pytest.raises(ValueError, match='public addresses'):
        server.print_url('https://pdf.example/a', dry_run=True)
    download[2].assert_not_called()
    cups.assert_not_called()


def test_one_private_dns_answer_rejects_entire_host(download, monkeypatch):
    monkeypatch.setattr(server.socket, 'getaddrinfo', Mock(return_value=[answer('8.8.8.8'), answer('127.0.0.1')]))
    with pytest.raises(ValueError):
        server.fetch_pdf('https://pdf.example/a')
    download[2].assert_not_called()


def test_empty_dns_and_dns_failure(download, monkeypatch):
    resolver = Mock(return_value=[])
    monkeypatch.setattr(server.socket, 'getaddrinfo', resolver)
    with pytest.raises(ValueError):
        server.fetch_pdf('https://pdf.example/a')
    resolver.side_effect = socket.gaierror('PRIVATE_TOKEN')
    with pytest.raises(ValueError) as error:
        server.fetch_pdf('https://pdf.example/a')
    assert 'PRIVATE_TOKEN' not in str(error.value)
    download[2].assert_not_called()


def test_rebinding_cannot_change_pinned_peer_or_tls_hostname(monkeypatch, cups):
    monkeypatch.delenv('PRINT_URL_ALLOWED_ORIGINS', raising=False)
    resolver = Mock(side_effect=[[answer('8.8.8.8')], [answer('127.0.0.1')]])
    monkeypatch.setattr(server.socket, 'getaddrinfo', resolver)
    raw, tls = Mock(), Mock()
    socket_factory = Mock(return_value=raw)
    context = Mock()
    context.wrap_socket.return_value = tls
    monkeypatch.setattr(server.socket, 'socket', socket_factory)
    monkeypatch.setattr(server.ssl, 'create_default_context', Mock(return_value=context))
    monkeypatch.setattr(REAL_CONNECTION, 'connect', REAL_CONNECT)
    response = Mock(status=200, headers={})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read1.side_effect = io.BytesIO(PDF).read
    monkeypatch.setattr(REAL_CONNECTION, 'getresponse', Mock(return_value=response))
    result = server.print_url('https://paper.example/document?download=1', dry_run=True)
    assert result['pages'] == 2
    resolver.assert_called_once_with('paper.example', 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    raw.connect.assert_called_once_with(('8.8.8.8', 443))
    context.wrap_socket.assert_called_once_with(raw, server_hostname='paper.example')
    wire = b''.join(call.args[0] for call in tls.sendall.call_args_list)
    assert b'Host: paper.example\r\n' in wire
    assert b'GET /document?download=1 HTTP/1.1' in wire
    assert b'Authorization:' not in wire and b'Cookie:' not in wire
    cups.assert_not_called()


def test_public_ipv6_and_port_are_preserved(download, monkeypatch):
    monkeypatch.delenv('PRINT_URL_ALLOWED_ORIGINS', raising=False)
    endpoint = answer('2606:4700:4700::1111', 8443)
    monkeypatch.setattr(server.socket, 'getaddrinfo', Mock(return_value=[endpoint]))
    assert server.fetch_pdf('https://paper.example:8443/a') == PDF
    assert download[2].call_args.args[1:3] == (8443, (socket.AF_INET6, endpoint[-1]))


def test_redirects_do_not_connect_again(download):
    download[0].status = 302
    download[0].headers = {'location': 'https://127.0.0.1/private'}
    with pytest.raises(ValueError, match='redirects are refused'):
        server.fetch_pdf('https://pdf.example/a')
    assert download[2].call_count == 1
    assert download[1].request.call_count == 1


def test_public_address_fallback_is_bounded(download, monkeypatch):
    monkeypatch.setattr(server.socket, 'getaddrinfo', Mock(return_value=[answer('8.8.8.8'), answer('1.1.1.1')]))
    first = Mock()
    first.connect.side_effect = OSError('unreachable')
    download[2].side_effect = [first, download[1]]
    assert server.fetch_pdf('https://pdf.example/a') == PDF
    first.close.assert_called_once()
    assert download[2].call_count == 2


def test_reject_http_even_without_allowlist(download, monkeypatch):
    monkeypatch.delenv('PRINT_URL_ALLOWED_ORIGINS', raising=False)
    with pytest.raises(ValueError, match='HTTPS'):
        server.fetch_pdf('http://paper.example/a')
    download[2].assert_not_called()


def test_tls_uses_certificate_and_hostname_checks():
    import ssl
    connection = REAL_CONNECTION('paper.example', 443, (socket.AF_INET, ('8.8.8.8', 443)), 10)
    assert connection.tls_context.check_hostname is True
    assert connection.tls_context.verify_mode == ssl.CERT_REQUIRED
    connection.close()


def test_malformed_url_does_not_echo_capability(download):
    with pytest.raises(ValueError) as error:
        server.fetch_pdf('https://pdf.example:PRIVATE_CAPABILITY/a')
    assert 'PRIVATE_CAPABILITY' not in str(error.value)
    download[2].assert_not_called()
