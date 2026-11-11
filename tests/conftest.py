from pathlib import Path
import sys
from unittest.mock import Mock
import urllib.request

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


@pytest.fixture
def cups(monkeypatch):
    # Every test fails closed against real CUPS calls and JWKS network access.
    fake = Mock(side_effect=AssertionError('Unexpected external command'))
    monkeypatch.setattr(server, '_bounded_run', fake)
    return fake


@pytest.fixture(autouse=True)
def local_environment(monkeypatch):
    monkeypatch.setenv('PRINT_PUBLIC_HOST', 'print.example.com')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open',
                        Mock(side_effect=AssertionError('Unexpected network request')))
    monkeypatch.setattr(server.socket, 'getaddrinfo', Mock(side_effect=AssertionError('Unexpected DNS request')))
    monkeypatch.setattr(server._PinnedHTTPSConnection, 'connect', Mock(side_effect=AssertionError('Unexpected HTTPS connection')))
    monkeypatch.delenv('ACCESS_TEAM_DOMAIN', raising=False)
    monkeypatch.delenv('ACCESS_AUD', raising=False)
    for name in ('PRINT_URL_ALLOWED_ORIGINS', 'PRINT_UPLOAD_ACCESS_AUD', 'PRINT_STATUS_URI', 'PRINT_INK_LEVELS_ESTIMATED', 'PRINT_MEDIA_REASONS_UNRELIABLE'):
        monkeypatch.delenv(name, raising=False)
