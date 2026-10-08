"""A minimal SSH server for tests (paramiko): password login + port forwarding (direct-tcpip).

Forward requests to FAKE_DB_PORT are connected to a Unix socket (pgserver listens on one), anything else
is refused, so tests can cover "SSH works but the database is unreachable".
"""

from __future__ import annotations

import select
import socket
import threading

import paramiko

FAKE_DB_PORT = 55432


class _Server(paramiko.ServerInterface):
    def __init__(self, username, password):
        self.username, self.password = username, password

    def check_auth_password(self, username, password):
        ok = username == self.username and password == self.password
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.destination = destination
        return paramiko.OPEN_SUCCEEDED if destination[1] == FAKE_DB_PORT else \
            paramiko.OPEN_FAILED_CONNECT_FAILED


class SSHTestServer:
    def __init__(self, unix_socket: str, username="tester", password="ssh-pass"):
        self.unix_socket, self.username, self.password = unix_socket, username, password
        self.key = paramiko.RSAKey.generate(2048)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client):
        transport = paramiko.Transport(client)
        transport.add_server_key(self.key)
        server = _Server(self.username, self.password)
        try:
            transport.start_server(server=server)
        except paramiko.SSHException:
            return
        while transport.is_active():
            channel = transport.accept(1.0)
            if channel is None:
                continue
            threading.Thread(target=self._pipe, args=(channel,), daemon=True).start()

    def _pipe(self, channel):
        db = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        db.connect(self.unix_socket)
        try:
            while True:
                ready, _, _ = select.select([db, channel], [], [], 1.0)
                if db in ready:
                    data = db.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in ready:
                    data = channel.recv(65536)
                    if not data:
                        break
                    db.sendall(data)
        except OSError:
            pass
        finally:
            channel.close()
            db.close()

    def close(self):
        self.sock.close()
