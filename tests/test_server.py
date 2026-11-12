import base64
import plistlib
import io
from pypdf import PdfWriter
from pathlib import Path
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

pytestmark = pytest.mark.usefixtures("cups")


def result(stdout=''):
    return subprocess.CompletedProcess([], 0, stdout, '')


def test_pdf_options_and_cleanup(cups):
    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=612, height=792)
    pdf = io.BytesIO()
    writer.write(pdf)
    payload = pdf.getvalue()
    paths = []
    def submit(args, **kwargs):
        assert args[:7] == ('lp', '-h', 'localhost:631', '-d', 'epson', '-n', '2')
        assert 'sides=two-sided-long-edge' in args
        assert 'print-color-mode=color' in args
        assert 'media=A3' in args
        assert 'page-ranges=1-3,5' in args
        path = Path(args[-1])
        assert path.read_bytes() == payload
        paths.append(path)
        assert kwargs['timeout'] == 30 and kwargs['check']
        assert kwargs['env']['CUPS_SERVER'] == 'localhost:631'
        return result('request id is epson-42 (1 file(s))')
    cups.side_effect = submit
    job = server.print_document(base64.b64encode(payload).decode(), 'pdf',
                                2, 'two-sided-long-edge', 'color', '1-3,5', 'A3')
    assert job == {'job_id': 'epson-42'}
    assert not paths[0].exists()


@pytest.mark.parametrize('options', [
    {'copies': 0}, {'copies': True}, {'copies': 101}, {'duplex': 'evil'},
    {'color': 'evil'}, {'paper_size': 'evil'}, {'page_range': '-1'},
    {'page_range': '3-1'}, {'page_range': '1;shutdown'}, {'format': 'html'},
    {'content': ''}, {'content': 'x' * 200001},
    {'content': 'not base64', 'format': 'pdf'},
    {'content': base64.b64encode(b'not a pdf').decode(), 'format': 'pdf'},
])
def test_reject_invalid(cups, options):
    with pytest.raises(ValueError):
        server.print_document(**({'content': 'draft'} | options))
    cups.assert_not_called()


@pytest.mark.parametrize('format', ['text', 'markdown'])
def test_text_rendered_pdf(cups, format):
    def submit(args, **kwargs):
        data = Path(args[-1]).read_bytes()
        assert data.startswith(b'%PDF-') and b'%%EOF' in data
        return result('request id is epson-1')
    cups.side_effect = submit
    assert server.print_document('# Draft\nHello café <script>\n' + 'wrap ' * 1000,
                                 format)['job_id'] == 'epson-1'


def test_failed_submission_cleans_up(cups):
    paths = []
    def fail(args, **kwargs):
        paths.append(Path(args[-1]))
        raise subprocess.TimeoutExpired(args, 30)
    cups.side_effect = fail
    with pytest.raises(TimeoutError, match='before retrying'):
        server.print_document('draft')
    assert not paths[0].exists()
    assert cups.call_count == 1  # No automatic retries.


def test_status_and_markers(cups):
    attributes = {'marker-names': ['Black', 'Cyan'], 'marker-levels': [75, 80],
                  'printer-state-reasons': 'none'}
    response = plistlib.dumps({'Successful': True, 'Tests': [
        {'Successful': True, 'ResponseAttributes': [attributes]}]}).decode()
    cups.side_effect = [result('printer epson is idle.'), result(response)]
    status = server.printer_status()
    assert status['markers_reported']
    assert [s['level_percent'] for s in status['supplies']] == [75, 80]
    assert status['source'] == 'cups-cache' and status['fresh'] is False
    assert 'cached' in status['fresh_error']


def test_ipp_unavailable(cups):
    cups.side_effect = [result('printer epson disabled: paper jam'), FileNotFoundError('ipptool')]
    status = server.printer_status()
    assert 'paper jam' in status['state'] and not status['markers_reported']


def test_list_cancel(cups):
    cups.side_effect = [result('epson-23 Owner 1024'), result('')]
    assert 'epson-23' in server.list_jobs()['jobs']
    assert server.cancel_job('epson-23') == {'cancelled': 'epson-23'}
    assert cups.call_args.args[0] == ('cancel', '-h', 'localhost:631', 'epson-23')


@pytest.mark.parametrize('job', ['-a', 'other-1', 'epson-1;id', 'epson-0'])
def test_cancel_rejects_other_jobs(cups, job):
    with pytest.raises(ValueError):
        server.cancel_job(job)
    cups.assert_not_called()


def test_cups_failure(cups):
    cups.side_effect = subprocess.CalledProcessError(1, 'lpstat', stderr='scheduler unavailable')
    with pytest.raises(RuntimeError, match='scheduler unavailable'):
        server.list_jobs()


def test_http_protocol(cups):
    mcp = server.create_server()
    assert mcp.settings.host == '127.0.0.1'
    assert mcp.settings.port == 8765
    headers = {'Accept': 'application/json, text/event-stream'}
    with TestClient(server.create_app(), base_url='http://127.0.0.1:8765') as client:
        def rpc(method, params):
            return client.post('/mcp', headers=headers, json={
                'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        response = rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                      'clientInfo': {'name': 'test', 'version': '1'}})
        assert response.status_code == 200
        tools = rpc('tools/list', {}).json()['result']['tools']
        assert {tool['name'] for tool in tools} == {
            'print_document', 'print_url', 'create_upload', 'print_upload', 'upload_status',
            'printer_status', 'list_jobs', 'cancel_job'}
        cups.return_value = result('epson-7 Owner 123')
        cups.side_effect = None
        response = rpc('tools/call', {'name': 'list_jobs', 'arguments': {}})
        assert 'epson-7' in response.text
        blocked = client.post('/mcp', headers={**headers, 'Host': 'untrusted.example'},
                              json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert blocked.status_code == 421


@pytest.mark.parametrize('host,origin,status', [
    ('print.example.com', 'https://print.example.com', 200),
    ('other.print.example.com', 'https://print.example.com', 421),
    ('print.example.com', 'https://other.print.example.com', 403),
])
def test_public_host_and_origin_are_exact(cups, host, origin, status):
    with TestClient(server.create_app(), base_url='http://127.0.0.1:8765') as client:
        response = client.post('/mcp', headers={
            'Accept': 'application/json, text/event-stream', 'Host': host, 'Origin': origin,
        }, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}})
    assert response.status_code == status
    cups.assert_not_called()


def test_configured_queue_job_identity(cups, monkeypatch):
    cups.side_effect = None
    monkeypatch.setattr(server, 'QUEUE', 'lab.queue')
    cups.return_value = result('request id is lab.queue-9')
    assert server.print_document('Synthetic page')['job_id'] == 'lab.queue-9'
    assert cups.call_args.args[0][3:5] == ('-d', 'lab.queue')
    cups.return_value = result('')
    assert server.cancel_job('lab.queue-9') == {'cancelled': 'lab.queue-9'}
    with pytest.raises(ValueError):
        server.cancel_job('labXqueue-9')


def test_unconfigured_public_host_is_not_allowed(monkeypatch):
    monkeypatch.delenv('PRINT_PUBLIC_HOST', raising=False)
    settings = server.create_server().settings.transport_security
    assert settings.allowed_hosts == ['127.0.0.1:*', 'localhost:*']
