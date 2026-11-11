import io
from pathlib import Path
import subprocess
import socket
from unittest.mock import Mock

import pytest

import server
from pypdf import PdfWriter

_buffer = io.BytesIO()
_writer = PdfWriter()
_writer.add_blank_page(width=400, height=500)
_writer.add_blank_page(width=400, height=500)
_writer.write(_buffer)
PDF = _buffer.getvalue()

pytestmark = pytest.mark.usefixtures('cups')


@pytest.fixture
def download(monkeypatch):
    monkeypatch.setenv('PRINT_URL_ALLOWED_ORIGINS', 'https://pdf.example')
    response = Mock(status=200, headers={})
    response.read1.side_effect = io.BytesIO(PDF).read
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(server, '_PinnedHTTPSConnection', factory)
    monkeypatch.setattr(server.socket, 'getaddrinfo', Mock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]))
    return response, connection, factory


def test_fetch_and_print_same_bytes_options_and_cleanup(download, cups):
    paths = []
    def submit(args, **kwargs):
        path = Path(args[-1])
        paths.append(path)
        assert path.read_bytes() == PDF
        assert 'page-ranges=2' in args
        assert 'sides=two-sided-long-edge' in args
        assert 'print-color-mode=color' in args
        assert 'media=A4' in args
        assert args[5:7] == ('-n', '2')
        return subprocess.CompletedProcess([], 0, 'request id is epson-8', '')
    cups.side_effect = submit
    assert server.print_url('https://pdf.example/pdf-download/capability', '2', 2, 'color',
                            'two-sided-long-edge', 'A4') == {'job_id': 'epson-8'}
    assert not paths[0].exists()
    download[1].request.assert_called_once_with('GET', '/pdf-download/capability', headers={'Accept': 'application/pdf'})
    assert download[2].call_args.args[0:2] == ('pdf.example', 443)
    assert download[2].call_args.args[3] <= 10


@pytest.mark.parametrize('url', [
    'http://pdf.example/a', 'https://pdf.example:444/a', 'https://pdf.example.evil/a',
    'file:///tmp/document.pdf', 'https://user:password@pdf.example/a',
    'https://pdf.example/a#fragment', 'https://localhost/a', 'https://pdf.example:0/a',
    'https://pdf.example/a\r\nsecret', 'https://pdf.example/a secret',
])
def test_allowlist_before_network(download, cups, url):
    with pytest.raises(ValueError):
        server.print_url(url)
    download[1].request.assert_not_called()
    cups.assert_not_called()


def test_unset_allowlist_accepts_public_https(download, monkeypatch, cups):
    monkeypatch.delenv('PRINT_URL_ALLOWED_ORIGINS')
    result = server.print_url('https://any-public.example/paper', dry_run=True)
    assert result['pages'] == 2 and result['dry_run']
    cups.assert_not_called()


def test_explicit_empty_allowlist_fails_closed(download, monkeypatch):
    monkeypatch.setenv('PRINT_URL_ALLOWED_ORIGINS', '')
    with pytest.raises(ValueError):
        server.fetch_pdf('https://pdf.example/a')
    download[1].request.assert_not_called()


@pytest.mark.parametrize('mode', ['redirect', 'html', 'declared_size', 'stream_size', 'timeout'])
def test_failed_fetch_never_prints(download, cups, monkeypatch, mode):
    response, opener, factory = download
    if mode == 'redirect':
        response.status = 302
    elif mode == 'html':
        response.read1.side_effect = io.BytesIO(b'<html>Access login</html>').read
    elif mode == 'declared_size':
        response.headers = {'content-length': '999999999'}
    elif mode == 'stream_size':
        monkeypatch.setenv('PRINT_URL_MAX_BYTES', '8')
    else:
        opener.request.side_effect = TimeoutError('secret URL')
    with pytest.raises(ValueError) as error:
        server.print_url('https://pdf.example/a')
    assert 'secret' not in str(error.value)
    cups.assert_not_called()


def test_failed_submission_cleans_without_retry(download, cups):
    paths = []
    def fail(args, **kwargs):
        paths.append(Path(args[-1]))
        raise subprocess.TimeoutExpired(args, 30)
    cups.side_effect = fail
    with pytest.raises(TimeoutError):
        server.print_url('https://pdf.example/a')
    assert not paths[0].exists()
    assert cups.call_count == 1


def test_invalid_options_before_fetch(download):
    with pytest.raises(ValueError):
        server.print_url('https://pdf.example/a', copies=0)
    download[1].request.assert_not_called()


def test_total_download_budget(download, cups, monkeypatch):
    clock = iter([0, 1, 61])
    monkeypatch.setattr(server.time, 'monotonic', lambda: next(clock))
    with pytest.raises(ValueError, match='time limit'):
        server.print_url('https://pdf.example/a')
    cups.assert_not_called()


@pytest.mark.parametrize('dry_run', [True, False])
def test_print_url_accepts_damaged_xref_and_trailing_bytes(download, cups, dry_run):
    import re

    damaged = re.sub(rb'(startxref\s+)(\d+)',
                     lambda match: match[1] + str(int(match[2]) + 13).encode(), PDF)
    assert damaged != PDF
    damaged += b'\ntrailing bytes after EOF\n'
    download[0].read1.side_effect = io.BytesIO(damaged).read

    def submit(args, **kwargs):
        assert Path(args[-1]).read_bytes() == damaged
        return subprocess.CompletedProcess([], 0, 'request id is epson-23', '')

    cups.side_effect = submit
    result = server.print_url('https://pdf.example/paper', dry_run=dry_run)
    if dry_run:
        assert result['pages'] == 2
        assert result['selected_pages'] == [1, 2]
        cups.assert_not_called()
    else:
        assert result == {'job_id': 'epson-23'}
        assert cups.call_count == 1
