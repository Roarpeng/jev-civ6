"""No-admin TCP forwarder: 0.0.0.0:14318 -> 127.0.0.1:4318 (Civ6 FireTuner).

Civ6's tuner only listens on 127.0.0.1, so Docker containers (and any other
machine process that must not fight for the single tuner slot) reach it
through this relay. netsh portproxy would also work but needs an elevated
shell; this needs nothing.

Run on the game host:  python scripts/host_forwarder.py [listen_port] [game_port]
"""
import socket
import threading
import sys

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 14318
GAME_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 4318
GAME_HOST = "127.0.0.1"


def pump(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        try:
            a.close()
            b.close()
        except OSError:
            pass


def handle(client: socket.socket) -> None:
    try:
        upstream = socket.create_connection((GAME_HOST, GAME_PORT), timeout=5)
    except OSError as e:
        print(f"[forwarder] game not reachable: {e}", flush=True)
        client.close()
        return
    threading.Thread(target=pump, args=(client, upstream), daemon=True).start()
    threading.Thread(target=pump, args=(upstream, client), daemon=True).start()


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", LISTEN_PORT))
    srv.listen(8)
    print(f"[forwarder] 0.0.0.0:{LISTEN_PORT} -> {GAME_HOST}:{GAME_PORT}", flush=True)
    while True:
        conn, addr = srv.accept()
        print(f"[forwarder] client {addr[0]}:{addr[1]}", flush=True)
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
