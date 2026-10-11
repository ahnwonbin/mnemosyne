from unittest.mock import Mock
import pytest
from bridge.tunnel import ensure_tunnel

CONFIG = {'ssh_tunnel': {'host': 'owner@memory.example', 'ssh': 'ssh', 'remote_port': 37777}}
ENDPOINT = 'http://127.0.0.1:37778'

def test_missing_tunnel_is_restored_and_existing_tunnel_is_reused(tmp_path, monkeypatch):
    ready = iter([False, True, True])
    monkeypatch.setattr('bridge.tunnel._listening', lambda port: next(ready))
    process = Mock()
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr('bridge.tunnel.subprocess.Popen', launch)
    ensure_tunnel(CONFIG, ENDPOINT, tmp_path)
    ensure_tunnel(CONFIG, ENDPOINT, tmp_path)
    assert launch.call_count == 1
    argv = launch.call_args.args[0]
    assert '127.0.0.1:37778:127.0.0.1:37777' in argv
    assert 'ExitOnForwardFailure=yes' in argv
    assert 'BatchMode=yes' in argv
    assert launch.call_args.kwargs.get('shell', False) is False

def test_disconnected_tunnel_can_be_restarted(tmp_path, monkeypatch):
    ready = iter([False, True, False, True])
    monkeypatch.setattr('bridge.tunnel._listening', lambda port: next(ready))
    process = Mock()
    process.poll.return_value = None
    launch = Mock(return_value=process)
    monkeypatch.setattr('bridge.tunnel.subprocess.Popen', launch)
    ensure_tunnel(CONFIG, ENDPOINT, tmp_path)
    ensure_tunnel(CONFIG, ENDPOINT, tmp_path)
    assert launch.call_count == 2

def test_failed_ssh_is_reported_and_can_retry(tmp_path, monkeypatch):
    monkeypatch.setattr('bridge.tunnel._listening', lambda port: False)
    process = Mock()
    process.poll.return_value = 255
    monkeypatch.setattr('bridge.tunnel.subprocess.Popen', Mock(return_value=process))
    for _ in range(2):
        with pytest.raises(ConnectionError, match='tunnel.log'):
            ensure_tunnel(CONFIG, ENDPOINT, tmp_path)

@pytest.mark.parametrize('endpoint', ['http://example.com:37778', 'https://127.0.0.1:37778', 'http://127.0.0.1:37778/path'])
def test_only_loopback_http_is_forwarded(endpoint, tmp_path):
    with pytest.raises(ValueError):
        ensure_tunnel(CONFIG, endpoint, tmp_path)

def test_unconfigured_transport_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr('bridge.tunnel.subprocess.Popen', Mock(side_effect=AssertionError))
    ensure_tunnel({}, ENDPOINT, tmp_path)
