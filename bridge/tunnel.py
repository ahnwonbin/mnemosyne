"""Restore the explicitly configured SSH forward from the background sender."""
import os
import re
import socket
import subprocess
import time
from urllib.parse import urlsplit


def _listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.5):
            return True
    except OSError:
        return False


def ensure_tunnel(config, endpoint, directory):
    tunnel = config.get('ssh_tunnel')
    if not tunnel:
        return
    url = urlsplit(endpoint)
    host = tunnel['host']
    remote_port = tunnel.get('remote_port', 37777)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
            or url.path not in ('', '/') or url.query or url.fragment
            or url.username or url.password):
        raise ValueError('SSH tunnel requires a loopback HTTP endpoint with an explicit port')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+', host):
        raise ValueError('SSH tunnel host must be user@hostname')
    if not isinstance(remote_port, int) or not 1 <= remote_port <= 65535:
        raise ValueError('Invalid SSH tunnel remote port')
    if _listening(url.port):
        return
    argv = [tunnel.get('ssh', 'ssh'), '-N', '-T', '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=10', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
            '-L', f'127.0.0.1:{url.port}:127.0.0.1:{remote_port}', host]
    options = ({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == 'nt' else {'start_new_session': True})
    log_path = directory / 'tunnel.log'
    with log_path.open('a', encoding='utf-8') as log:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=log, close_fds=True, **options)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if _listening(url.port):
            print(f'[hermes-bridge] SSH tunnel restored on port {url.port}', flush=True)
            return
        if process.poll() is not None:
            raise ConnectionError(f'SSH tunnel exited; see {log_path}')
        time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    raise ConnectionError(f'SSH tunnel startup timed out; see {log_path}')
