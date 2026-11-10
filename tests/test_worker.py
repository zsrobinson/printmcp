import asyncio
import os
import sys
import threading
import time

import anyio
import pytest
import server


def test_commands_drain_and_bound_output():
    result = server._bounded_run((sys.executable, '-c',
        'import os; os.write(1,b"x"*200000); os.write(2,b"y"*200000)'),
        timeout=3, check=True, env=dict(os.environ))
    assert len(result.stdout) == len(result.stderr) == 65536


def test_command_deadline():
    with pytest.raises(TimeoutError):
        server._bounded_run((sys.executable,'-c','import time; time.sleep(5)'),
                            timeout=0.05, check=True, env=dict(os.environ))


def test_worker_bound_and_event_loop(monkeypatch):
    active = 0
    peak = 0
    threads = set()
    lock = threading.Lock()
    def slow():
        nonlocal active,peak
        with lock:
            active += 1
            peak = max(peak,active)
            threads.add(threading.get_ident())
        time.sleep(0.03)
        with lock:
            active -= 1
        return {}
    slow.__name__ = 'list_jobs'
    monkeypatch.setattr(server,'list_jobs',slow)
    async def run():
        mcp=server.create_server()
        ticks=0
        done=False
        async def ticker():
            nonlocal ticks
            while not done:
                ticks += 1
                await anyio.sleep(0.001)
        task=asyncio.create_task(ticker())
        await asyncio.gather(*(mcp.call_tool('list_jobs',{}) for _ in range(6)))
        done=True
        await task
        assert ticks > 10
    asyncio.run(run())
    assert peak==2
    assert threading.get_ident() not in threads


def test_font_registration_cached(monkeypatch,tmp_path):
    calls=[]
    original=server.pdfmetrics.registerFont
    def register(font):
        calls.append(font)
        original(font)
    monkeypatch.setattr(server.pdfmetrics,'registerFont',register)
    server.text_pdf('sample',tmp_path/'one.pdf','Letter')
    server.text_pdf('sample',tmp_path/'two.pdf','Letter')
    assert len(calls)<=1
