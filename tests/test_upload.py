import asyncio
import io
from pathlib import Path
import subprocess
from unittest.mock import Mock

import httpx
from PIL import Image
from pypdf import PdfWriter
import pytest
from starlette.testclient import TestClient

import server


def pdf(pages=2):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=400, height=500)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def image(kind):
    buffer = io.BytesIO()
    Image.new('RGB', (30, 20), 'white').save(buffer, format=kind)
    return buffer.getvalue()


@pytest.fixture
def uploads(monkeypatch):
    monkeypatch.setenv('PRINT_UPLOAD_SECRET', 'x' * 32)
    monkeypatch.setenv('PRINT_UPLOAD_BASE_URL', 'https://print.example.com')
    monkeypatch.setenv('PRINT_UPLOAD_MAX_BYTES', '52428800')
    monkeypatch.setenv('PRINT_UPLOAD_MAX_PENDING', '8')
    store = server.UploadStore()
    monkeypatch.setattr(server, '_uploads', store)
    yield store
    store.close()


@pytest.fixture
def client(uploads, cups):
    with TestClient(server.create_app(), base_url='https://print.example.com') as client:
        yield client


def put(client, kind='pdf', data=None, **options):
    created = server.create_upload(kind, options.pop('max_bytes', None))
    body = pdf() if data is None else data
    response = client.put(created['url'], content=body,
                          headers={'Content-Type': server.CONTENT_TYPES[kind]}, **options)
    return created, response


def test_pdf_put_status_dry_run_and_replay(client, uploads, cups):
    created, response = put(client)
    assert response.status_code == 200
    key = created['upload_id']
    status = server.upload_status(key)
    assert status['status'] == 'received' and status['pages'] == 2
    path = uploads.entries[key].path
    assert path.stat().st_mode & 0o777 == 0o600
    assert Path(uploads.directory.name).stat().st_mode & 0o777 == 0o700
    summary = server.print_upload(key, '2', 2, 'color', 'two-sided-long-edge', 'A4', dry_run=True)
    assert summary['dry_run'] is True and summary['selected_pages'] == [2]
    assert summary['pages'] == 2 and summary['copies'] == 2
    assert server.upload_status(key)['status'] == 'received'
    assert path.exists()
    assert client.put(created['url'], content=pdf(), headers={'Content-Type': 'application/pdf'}).status_code == 404
    cups.assert_not_called()


@pytest.mark.parametrize('kind,body', [('png', image('PNG')), ('jpeg', image('JPEG'))])
def test_images_are_converted_in_dry_run_without_cups(client, cups, kind, body):
    created, response = put(client, kind, body)
    assert response.status_code == 200
    summary = server.print_upload(created['upload_id'], dry_run=True)
    assert summary['content_type'] == server.CONTENT_TYPES[kind]
    assert summary['pages'] == 1 and summary['prepared_pdf_bytes'] > 0
    cups.assert_not_called()


def test_mocked_submission_uses_shared_cups_and_deletes(client, cups, uploads):
    created, response = put(client)
    path = uploads.entries[created['upload_id']].path
    def submit(args, **kwargs):
        assert args[0] == 'lp' and 'media=A4' in args and 'page-ranges=2' in args
        assert Path(args[-1]).read_bytes() == pdf()
        return subprocess.CompletedProcess([], 0, 'request id is epson-19', '')
    cups.side_effect = submit
    assert server.print_upload(created['upload_id'], '2', paper_size='A4') == {'job_id': 'epson-19'}
    assert server.upload_status(created['upload_id'])['status'] == 'printed'
    assert not path.exists()
    assert cups.call_count == 1


@pytest.mark.parametrize('mode', ['cups', 'options', 'page_range'])
def test_failure_consumes_and_deletes_without_retry(client, cups, uploads, mode):
    created, response = put(client)
    key = created['upload_id']
    path = uploads.entries[key].path
    cups.side_effect = subprocess.TimeoutExpired('lp', 30)
    with pytest.raises((ValueError, TimeoutError)):
        server.print_upload(key, page_range='99' if mode == 'page_range' else None,
                            copies=0 if mode == 'options' else 1)
    assert not path.exists()
    assert server.upload_status(key)['status'] == 'expired'
    with pytest.raises(ValueError):
        server.print_upload(key)
    assert cups.call_count == (1 if mode == 'cups' else 0)


@pytest.mark.parametrize('kind,body', [('pdf', image('PNG')), ('png', pdf()),
                                       ('jpeg', b'not jpeg'), ('pdf', b'%PDF-broken')])
def test_magic_or_parser_failure_consumes_put(client, uploads, cups, kind, body):
    created, response = put(client, kind, body)
    assert response.status_code == 404
    assert server.upload_status(created['upload_id'])['status'] == 'expired'
    assert uploads.entries[created['upload_id']].path is None
    assert client.put(created['url'], content=body).status_code == 404
    cups.assert_not_called()


def test_declared_and_streaming_caps_and_content_type(client, uploads, cups):
    created = server.create_upload('pdf', 10)
    response = client.put(created['url'], content=pdf(), headers={'Content-Type': 'application/pdf'})
    assert response.status_code == 404
    created = server.create_upload('pdf', 10)
    response = client.put(created['url'], content=iter([b'%PDF-', b'1234567890']),
                          headers={'Content-Type': 'application/pdf'})
    assert response.status_code == 404
    created = server.create_upload('pdf')
    assert client.put(created['url'], content=pdf(), headers={'Content-Type': 'image/png'}).status_code == 404
    assert not list(Path(uploads.directory.name).iterdir())
    cups.assert_not_called()


def test_expiry_sweep_deletes_file_and_expires_pending(client, uploads, monkeypatch):
    created, response = put(client)
    pending = server.create_upload('pdf')
    path = uploads.entries[created['upload_id']].path
    monkeypatch.setattr(server.time, 'time', lambda: created['expires_at'] + 1)
    uploads.sweep()
    assert not path.exists()
    assert server.upload_status(created['upload_id'])['status'] == 'expired'
    assert server.upload_status(pending['upload_id'])['status'] == 'expired'
    assert client.put(created['url'], content=pdf()).status_code == 404


def test_forged_token_unknown_host_and_missing_configuration(client, uploads, monkeypatch):
    created = server.create_upload('pdf')
    assert client.put(created['url'] + 'x', content=pdf()).status_code == 404
    assert client.put(created['url'], headers={'Host': 'evil.example'}, content=pdf()).status_code == 404
    assert server.upload_status(created['upload_id'])['status'] == 'pending'
    monkeypatch.setenv('PRINT_UPLOAD_SECRET', '')
    with pytest.raises(ValueError):
        server.UploadStore()


def test_concurrent_put_claim_cannot_overwrite(client, uploads):
    created = server.create_upload('pdf')
    token = created['url'].rsplit('/', 1)[1]
    first = uploads.reserve(token)
    with pytest.raises(ValueError):
        uploads.reserve(token)
    uploads.discard(first)
    with pytest.raises(ValueError):
        uploads.reserve(token)


def test_upload_capacity_and_invalid_limits(uploads):
    uploads.capacity = 1
    for maximum in (0, -1, True, uploads.limit + 1):
        with pytest.raises(ValueError):
            uploads.create('pdf', maximum)
    uploads.create('pdf', None)
    with pytest.raises(ValueError, match='capacity'):
        uploads.create('pdf', None)
    with pytest.raises(ValueError):
        uploads.create('html', None)


def test_put_retains_access_protection(uploads, cups, monkeypatch):
    monkeypatch.setenv('ACCESS_TEAM_DOMAIN', 'example.cloudflareaccess.com')
    monkeypatch.setenv('ACCESS_AUD', 'main-audience')
    created = server.create_upload('pdf')
    with TestClient(server.create_app(), base_url='https://print.example.com') as client:
        assert client.put(created['url'], content=pdf(), headers={'Content-Type': 'application/pdf'}).status_code == 401
        assert server.upload_status(created['upload_id'])['status'] == 'pending'
    cups.assert_not_called()


def test_upload_audience_is_path_scoped(uploads, cups, monkeypatch):
    monkeypatch.setenv('ACCESS_TEAM_DOMAIN', 'example.cloudflareaccess.com')
    monkeypatch.setenv('ACCESS_AUD', 'main-audience')
    monkeypatch.setenv('PRINT_UPLOAD_ACCESS_AUD', 'upload-audience')
    validator = Mock()
    monkeypatch.setattr(server.AccessJWTMiddleware, 'validate', validator)
    created = server.create_upload('pdf')
    with TestClient(server.create_app(), base_url='https://print.example.com') as client:
        response = client.put(created['url'], content=pdf(), headers={
            'Content-Type': 'application/pdf', 'Cf-Access-Jwt-Assertion': 'test-assertion'})
        assert response.status_code == 200
        assert validator.call_args.args == ('test-assertion', 'upload-audience')
        client.post('/mcp', headers={'Cf-Access-Jwt-Assertion': 'test-assertion'})
        assert validator.call_args.args == ('test-assertion',)
    cups.assert_not_called()


def test_no_capability_logged(client, uploads, cups, capsys, caplog):
    created, response = put(client)
    client.put(created['url'] + 'bad', content=pdf())
    logs = capsys.readouterr().err + caplog.text
    assert created['url'] not in logs
    assert created['url'].rsplit('/', 1)[1] not in logs
    cups.assert_not_called()


def test_client_disconnect_discards_partial_file(uploads, cups):
    created = server.create_upload('pdf')
    token = created['url'].rsplit('/', 1)[1]
    events = iter([{'type': 'http.request', 'body': b'%PDF-', 'more_body': True},
                   {'type': 'http.disconnect'}])
    async def run():
        async def receive():
            return next(events)
        request = server.Request({'type': 'http', 'method': 'PUT', 'path': '/upload/' + token,
                                  'headers': [(b'host', b'print.example.com'), (b'content-type', b'application/pdf')],
                                  'path_params': {'token': token}}, receive)
        assert (await server._receive_upload(request)).status_code == 404
    asyncio.run(run())
    assert server.upload_status(created['upload_id'])['status'] == 'expired'
    assert not list(Path(uploads.directory.name).iterdir())
    cups.assert_not_called()


def test_concurrent_http_puts_only_one_body_is_accepted(uploads, cups):
    created = server.create_upload('pdf')
    async def run():
        entered, resume = asyncio.Event(), asyncio.Event()
        async def first_body():
            entered.set()
            await resume.wait()
            yield pdf()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(server.create_app()),
                                     base_url='https://print.example.com') as client:
            first = asyncio.create_task(client.put(created['url'], content=first_body(),
                                                     headers={'Content-Type': 'application/pdf'}))
            await entered.wait()
            try:
                second = await client.put(created['url'], content=pdf(), headers={'Content-Type': 'application/pdf'})
                assert second.status_code == 404
            finally:
                resume.set()
            assert (await first).status_code == 200
    asyncio.run(run())
    assert server.upload_status(created['upload_id'])['status'] == 'received'
    cups.assert_not_called()


def test_terminal_metadata_is_swept_without_files(client, uploads, cups, monkeypatch):
    created, response = put(client)
    cups.side_effect = None
    cups.return_value = subprocess.CompletedProcess([], 0, 'request id is epson-20', '')
    server.print_upload(created['upload_id'])
    assert server.upload_status(created['upload_id'])['status'] == 'printed'
    retired = uploads.entries[created['upload_id']].terminal_at
    monkeypatch.setattr(server.time, 'time', lambda: retired + 601)
    uploads.sweep()
    assert created['upload_id'] not in uploads.entries
    assert not list(Path(uploads.directory.name).iterdir())


def test_pdf_encryption_and_image_pixel_limit_are_rejected():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt('fixture-password')
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(ValueError):
        server._document_info(buffer.getvalue(), 'application/pdf')
    buffer = io.BytesIO()
    Image.new('1', (5000, 5000)).save(buffer, format='PNG')
    with pytest.raises(ValueError):
        server._document_info(buffer.getvalue(), 'image/png')


def test_lifespan_shutdown_deletes_remaining_payloads(uploads, cups):
    with TestClient(server.create_app(), base_url='https://print.example.com') as client:
        created, response = put(client)
        path = uploads.entries[created['upload_id']].path
        assert path.exists()
    assert not path.exists()
    assert server._uploads is None
    cups.assert_not_called()


def test_upload_audience_with_real_signatures(uploads, cups, monkeypatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from types import SimpleNamespace

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setenv('ACCESS_TEAM_DOMAIN', 'example.cloudflareaccess.com')
    monkeypatch.setenv('ACCESS_AUD', 'main-audience')
    monkeypatch.setenv('PRINT_UPLOAD_ACCESS_AUD', 'upload-audience')
    monkeypatch.setattr(jwt.PyJWKClient, 'get_signing_key_from_jwt',
                        lambda self, token: SimpleNamespace(key=private.public_key()))
    def assertion(audience):
        return jwt.encode({'iss': 'https://example.cloudflareaccess.com', 'aud': audience,
                           'exp': server.time.time() + 300}, private, algorithm='RS256')
    created = server.create_upload('pdf')
    with TestClient(server.create_app(), base_url='https://print.example.com') as client:
        assert client.put(created['url'], content=pdf(), headers={
            'Content-Type': 'application/pdf', 'Cf-Access-Jwt-Assertion': assertion('main-audience')}).status_code == 401
        assert client.put(created['url'], content=pdf(), headers={
            'Content-Type': 'application/pdf', 'Cf-Access-Jwt-Assertion': assertion('upload-audience')}).status_code == 200
        assert client.post('/mcp', headers={
            'Cf-Access-Jwt-Assertion': assertion('upload-audience')}).status_code == 401
    cups.assert_not_called()
