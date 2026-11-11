import json
import urllib.error

import pytest

import server
from test_url import PDF, download  # noqa: F401


@pytest.fixture
def header_file(tmp_path, monkeypatch):
    path = tmp_path / 'origin-headers.json'
    path.write_text(json.dumps({'https://pdf.example': {
        'CF-Access-Client-Id': 'fixture-client-id',
        'CF-Access-Client-Secret': 'fixture-private-secret',
    }}))
    path.chmod(0o600)
    monkeypatch.setenv('PRINT_URL_ORIGIN_HEADERS_FILE', str(path))
    return path


def test_exact_origin_auth_headers_never_go_elsewhere(download, header_file):
    server.fetch_pdf('https://pdf.example/pdf-download/capability')
    request = download[1].open.call_args.args[0]
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers['cf-access-client-id'] == 'fixture-client-id'
    assert headers['cf-access-client-secret'] == 'fixture-private-secret'
    assert server._origin_headers('https://pdf.example:444/a') == {}
    assert server._origin_headers('http://pdf.example/a') == {}
    assert server._origin_headers('https://other.example/a') == {}


def test_redirect_is_not_followed_with_credentials(download, header_file, cups, caplog):
    download[1].open.side_effect = urllib.error.HTTPError(
        'https://pdf.example/PRIVATE_CAPABILITY', 302, 'redirect', {'Location': 'https://other.example'}, None)
    with pytest.raises(ValueError) as error:
        server.print_url('https://pdf.example/PRIVATE_CAPABILITY', dry_run=True)
    assert download[1].open.call_count == 1
    assert 'fixture-private-secret' not in str(error.value) + caplog.text
    assert 'PRIVATE_CAPABILITY' not in str(error.value) + caplog.text
    cups.assert_not_called()


@pytest.mark.parametrize('mode', ['permissions', 'symlink', 'json', 'host_header', 'newline', 'http', 'too_large'])
def test_invalid_private_configuration_fails_closed(header_file, download, cups, mode):
    if mode == 'permissions':
        header_file.chmod(0o644)
    elif mode == 'symlink':
        real = header_file.with_name('real.json')
        header_file.rename(real)
        header_file.symlink_to(real)
    elif mode == 'json':
        header_file.write_text('{PRIVATE_CONFIG_SECRET')
    elif mode == 'host_header':
        header_file.write_text(json.dumps({'https://pdf.example': {'Host': 'evil.example'}}))
    elif mode == 'newline':
        header_file.write_text(json.dumps({'https://pdf.example': {'Authorization': 'PRIVATE_CONFIG_SECRET\r\nx: y'}}))
    elif mode == 'http':
        header_file.write_text(json.dumps({'http://pdf.example': {'Authorization': 'PRIVATE_CONFIG_SECRET'}}))
    else:
        header_file.write_text('PRIVATE_CONFIG_SECRET' * 5000)
    with pytest.raises(ValueError, match='Invalid private') as error:
        server.fetch_pdf('https://pdf.example/a')
    assert 'PRIVATE_CONFIG_SECRET' not in str(error.value)
    download[1].open.assert_not_called()
    cups.assert_not_called()


def test_url_dry_run_counts_pages_and_never_calls_cups(download, header_file, cups):
    summary = server.print_url('https://pdf.example/a', page_range='2', copies=3, dry_run=True)
    assert summary['dry_run'] and summary['pages'] == 2
    assert summary['selected_pages'] == [2] and summary['copies'] == 3
    assert summary['bytes'] == len(PDF)
    cups.assert_not_called()


def test_header_rotation_reloads_file(header_file):
    assert server._origin_headers('https://pdf.example/a')['cf-access-client-secret'] == 'fixture-private-secret'
    header_file.write_text(json.dumps({'https://pdf.example': {'CF-Access-Client-Secret': 'rotated-fixture'}}))
    assert server._origin_headers('https://pdf.example/a')['cf-access-client-secret'] == 'rotated-fixture'
