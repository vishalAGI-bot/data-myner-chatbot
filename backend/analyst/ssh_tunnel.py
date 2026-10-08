"""A small SSH port-forwarder built on paramiko (replaces the unmaintained ``sshtunnel`` package,
which fails with paramiko 4+ because it references the removed ``paramiko.DSSKey``).

    tunnel = SSHTunnel("ssh.example.com", 22, "user", password="...", remote=("127.0.0.1", 5432))
    tunnel.start()                 # raises SourceError with a clear message on failure
    psycopg.connect(host="127.0.0.1", port=tunnel.local_port, ...)
    tunnel.stop()

Host keys: keys in ~/.ssh/known_hosts are enforced (a changed key is refused). Unknown hosts are accepted
and their fingerprint is logged and shown on the Settings page, or pinned with ``SSH_HOST_KEY_SHA256``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import select
import socket
import threading

log = logging.getLogger("analyst.ssh")


class TunnelError(RuntimeError):
    pass


def _log_unknown_host_policy():
    import paramiko

    class LogUnknownHost(paramiko.MissingHostKeyPolicy):
        """Accept a host that is not in known_hosts, but record its fingerprint (pin it to enforce)."""

        def missing_host_key(self, client, hostname, key):
            log.info("SSH host %s is not in known_hosts; its key is %s", hostname, fingerprint(key))

    return LogUnknownHost()


def fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class SSHTunnel:
    def __init__(self, host: str, port: int, username: str, *, password: str = "", pkey_path: str = "",
                 remote: tuple[str, int], timeout: float = 10.0, expected_fingerprint: str = ""):
        self.host, self.port, self.username = host, port, username
        self.password, self.pkey_path = password, pkey_path
        self.remote = remote
        self.timeout = timeout
        self.expected_fingerprint = expected_fingerprint.strip()
        self.host_fingerprint = ""
        self.local_port = 0
        self.last_error = ""                # why the SSH server could not open the database connection
        self._client = None
        self._server: socket.socket | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ state
    @property
    def is_active(self) -> bool:
        transport = self._client.get_transport() if self._client else None
        return bool(transport and transport.is_active() and not self._stop.is_set())

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        import paramiko

        try:   # separate "cannot reach" from "login failed"
            socket.create_connection((self.host, self.port), timeout=self.timeout).close()
        except OSError as exc:
            raise TunnelError(f"Cannot reach the SSH server {self.host}:{self.port} ({exc.strerror or exc}). "
                              "Check the SSH host, port and network/VPN.") from exc

        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
        except (OSError, paramiko.SSHException):
            pass
        client.set_missing_host_key_policy(_log_unknown_host_policy())
        kwargs = dict(hostname=self.host, port=self.port, username=self.username, timeout=self.timeout,
                      banner_timeout=self.timeout, auth_timeout=self.timeout, allow_agent=False, look_for_keys=False)
        if self.pkey_path:
            kwargs["key_filename"] = self.pkey_path
            if self.password:
                kwargs["passphrase"] = self.password
        else:
            kwargs["password"] = self.password
        try:
            client.connect(**kwargs)
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise TunnelError(f"The SSH server's host key has CHANGED ({fingerprint(exc.key)}). This can mean "
                              "someone is intercepting the connection. Check with your server admin.") from exc
        except paramiko.AuthenticationException as exc:
            client.close()
            raise TunnelError(f"SSH login to {self.host} failed for user '{self.username}': check the SSH "
                              "username and password or key file.") from exc
        except (paramiko.SSHException, OSError, ValueError) as exc:
            client.close()
            raise TunnelError(f"SSH connection to {self.host}:{self.port} failed: {exc}") from exc

        transport = client.get_transport()
        self.host_fingerprint = fingerprint(transport.get_remote_server_key())
        if self.expected_fingerprint and self.expected_fingerprint != self.host_fingerprint:
            client.close()
            raise TunnelError(f"SSH host key {self.host_fingerprint} does not match SSH_HOST_KEY_SHA256.")
        transport.set_keepalive(30)
        self._client = client

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))           # loopback only: never exposed to the network
        server.listen(16)
        server.settimeout(1.0)
        self._server = server
        self.local_port = server.getsockname()[1]
        threading.Thread(target=self._accept, name="ssh-tunnel-accept", daemon=True).start()
        log.info("SSH tunnel %s@%s:%s -> %s:%s on 127.0.0.1:%s (host key %s)", self.username, self.host,
                 self.port, *self.remote, self.local_port, self.host_fingerprint)

    # --------------------------------------------------------------- forward
    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, peer = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._forward, args=(conn, peer), name="ssh-tunnel-conn", daemon=True).start()

    def _forward(self, conn: socket.socket, peer) -> None:
        transport = self._client.get_transport() if self._client else None
        try:
            if transport is None or not transport.is_active():
                raise OSError("SSH connection is closed")
            channel = transport.open_channel("direct-tcpip", self.remote, peer, timeout=self.timeout)
        except Exception as exc:
            self.last_error = str(exc) or type(exc).__name__
            log.warning("SSH server could not open %s:%s: %s", *self.remote, exc)
            conn.close()
            return
        self.last_error = ""
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([conn, channel], [], [], 1.0)
                if conn in ready:
                    data = conn.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in ready:
                    data = channel.recv(65536)
                    if not data:
                        break
                    conn.sendall(data)
        except OSError:
            pass
        finally:
            channel.close()
            conn.close()

    # ------------------------------------------------------------------ stop
    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._client is not None:
            self._client.close()
