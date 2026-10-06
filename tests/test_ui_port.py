"""Restart readiness must distinguish a live listener from a closed TCP connection."""
import os
import socket

import pytest

from igs.cli import _port_in_use


def test_active_listener_is_detected():
    with socket.socket() as server:
        server.bind(('127.0.0.1',0))
        server.listen()
        assert _port_in_use('127.0.0.1',server.getsockname()[1])


@pytest.mark.skipif(os.name=='nt',reason='POSIX socket reuse semantics')
def test_closed_connection_does_not_block_dashboard_restart():
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        server.bind(('127.0.0.1',0))
        port=server.getsockname()[1]
        server.listen()
        with socket.create_connection(('127.0.0.1',port),timeout=2) as client:
            accepted,_=server.accept()
            accepted.close()
            assert client.recv(1)==b''
    assert not _port_in_use('127.0.0.1',port)
