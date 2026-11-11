import io
from pathlib import Path
import subprocess
import urllib.request
from unittest.mock import Mock

import pytest

import server

pytestmark = pytest.mark.usefixtures('cups')


@pytest.fixture
def download(monkeypatch):
    monkeypatch.setenv('PRINT_URL_ALLOWED_ORIGINS', 'https://pdf.example,http://private.example:8788')
    response = Mock(status=200, headers={})
    response.read1.side_effect = io.BytesIO(b'%PDF-1.7\nfixture').read
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    factory = Mock(return_value=opener)
    monkeypatch.setattr(urllib.request, 'build_opener', factory)
    return response, opener, factory


def test_fetch_and_print_same_bytes_options_and_cleanup(download, cups):
    paths = []
    def submit(args, **kwargs):
        path = Path(args[-1])
        paths.append(path)
        assert path.read_bytes() == b'%PDF-1.7\nfixture'
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
    assert download[1].open.call_count == 1
    assert download[1].open.call_args.kwargs['timeout'] == 10
    handlers = download[2].call_args.args
    assert handlers[0].proxies == {}
    assert handlers[1].redirect_request(None, None, 302, '', None, 'https://evil.example') is None


@pytest.mark.parametrize('url', [
    'http://pdf.example/a', 'https://pdf.example:444/a', 'https://pdf.example.evil/a',
    'file:///tmp/document.pdf', 'https://user:password@pdf.example/a',
    'https://pdf.example/a#fragment', 'https://localhost/a', 'https://pdf.example:0/a',
    'https://pdf.example/a\r\nsecret', 'https://pdf.example/a secret',
])
def test_allowlist_before_network(download, cups, url):
    with pytest.raises(ValueError):
        server.print_url(url)
    download[1].open.assert_not_called()
    cups.assert_not_called()


def test_empty_allowlist(download, monkeypatch):
    monkeypatch.delenv('PRINT_URL_ALLOWED_ORIGINS')
    with pytest.raises(ValueError):
        server.fetch_pdf('https://pdf.example/a')
    download[1].open.assert_not_called()


@pytest.mark.parametrize('mode', ['redirect', 'html', 'declared_size', 'stream_size', 'timeout'])
def test_failed_fetch_never_prints(download, cups, monkeypatch, mode):
    response, opener, factory = download
    if mode == 'redirect':
        opener.open.side_effect = server.urllib.error.HTTPError('https://pdf.example/secret', 302, 'redirect', {}, None)
    elif mode == 'html':
        response.read1.side_effect = io.BytesIO(b'<html>Access login</html>').read
    elif mode == 'declared_size':
        response.headers = {'content-length': '999999999'}
    elif mode == 'stream_size':
        monkeypatch.setenv('PRINT_URL_MAX_BYTES', '8')
    else:
        opener.open.side_effect = TimeoutError('secret URL')
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
    download[1].open.assert_not_called()


def test_total_download_budget(download, cups, monkeypatch):
    clock = iter([0, 61])
    monkeypatch.setattr(server.time, 'monotonic', lambda: next(clock))
    with pytest.raises(ValueError, match='time limit'):
        server.print_url('https://pdf.example/a')
    cups.assert_not_called()
