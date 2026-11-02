import argparse
import atexit
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import signal
import threading
from socketserver import TCPServer
from urllib.parse import parse_qs, urlsplit

from .core import Manager, Problem, ROOT, web_port
from .monitor import SERIES_BUCKET_SECONDS, SERIES_INTERVALS


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def make_server(manager, port=11539):
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    origin = f"http://127.0.0.1:{port}"
    allowed_host = f"127.0.0.1:{port}"
    assets = {"/": ("index.html", "text/html; charset=utf-8"),
              "/favicon.svg": ("favicon.svg", "image/svg+xml"),
              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
              "/navigation.js": ("navigation.js", "text/javascript; charset=utf-8"),
              "/visuals.js": ("visuals.js", "text/javascript; charset=utf-8"),
              "/style.css": ("style.css", "text/css; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        server_version = "FlintKit"
        timeout = 10

        def log_message(self, *args):
            # HTTP paths/bodies are not activity logs.
            pass

        def reply(self, status, value, mime="application/json", cookie=False):
            body = json.dumps(value, ensure_ascii=False).encode() if mime == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                             "base-uri 'none'; form-action 'self'; object-src 'none'")
            if cookie:
                self.send_header("Set-Cookie", f"flint_session={session}; HttpOnly; SameSite=Strict; Path=/")
            self.end_headers()
            self.wfile.write(body)

        def valid_site(self):
            hosts = self.headers.get_all("Host") or []
            origins = self.headers.get_all("Origin") or []
            return (hosts == [allowed_host] and (not origins or origins == [origin])
                    and self.headers.get("Sec-Fetch-Site", "none") in ("none", "same-origin")
                    and not self.headers.get("Upgrade"))

        def authorized(self):
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                token = cookies.get("flint_session")
                return (token is not None and secrets.compare_digest(token.value, session)
                        and secrets.compare_digest(self.headers.get("X-Flint-CSRF", ""), csrf))
            except (ValueError, TypeError):
                return False

        def do_GET(self):
            if not self.valid_site():
                self.reply(403, {"error": "Host / Origin 거부"})
                return
            # Screen navigation keeps its step in the query string; assets ignore it.
            url = urlsplit(self.path)
            asset = url.path
            if self.path.startswith("/") and asset in assets:
                file, mime = assets[asset]
                self.reply(200, (ROOT / "static" / file).read_bytes(), mime)
            elif self.path == "/api/session":
                self.reply(200, dict(csrf=csrf), cookie=True)
            elif self.path.startswith("/") and asset == "/api/state":
                if not self.authorized():
                    self.reply(401, {"error": "로컬 세션 인증 필요"})
                    return
                try:
                    query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
                    if query and (set(query) != {"chart_seconds"}
                                  or query["chart_seconds"][0] not in {str(n) for n in SERIES_INTERVALS}):
                        raise ValueError("unsupported chart interval")
                    seconds = int(query["chart_seconds"][0]) if query else SERIES_BUCKET_SECONDS
                except ValueError:
                    self.reply(400, {"error": "chart_seconds는 5, 60, 300초 중 하나여야 합니다."})
                    return
                try:
                    self.reply(200, manager.status(chart_seconds=seconds))
                except (OSError, ValueError) as error:
                    manager.log("상태 읽기 실패: " + str(error))
                    self.reply(500, {"error": "상태 읽기 실패. 활동 로그를 확인하세요."})
            elif self.path.startswith("/api/tune/history/"):
                if not self.authorized():
                    self.reply(401, {"error": "로컬 세션 인증 필요"})
                    return
                try:
                    self.reply(200, manager.historical_tuning(self.path.removeprefix("/api/tune/history/")))
                except Problem as error:
                    self.reply(400, {"error": str(error)})
                except (OSError, ValueError) as error:
                    manager.log("튜닝 기록 읽기 실패: " + str(error))
                    self.reply(500, {"error": "튜닝 기록 읽기 실패. 활동 로그를 확인하세요."})
            else:
                self.reply(404, {"error": "Not found"})

        def do_POST(self):
            if not self.valid_site():
                self.reply(403, {"error": "Host / Origin 거부"})
                return
            if not self.authorized() or self.headers.get("Origin") != origin:
                self.reply(403, {"error": "세션 / CSRF 인증 거부"})
                return
            if self.headers.get("Content-Type") != "application/json" or self.headers.get("Transfer-Encoding"):
                self.reply(415, {"error": "application/json만 허용"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32768:
                    raise Problem("요청 크기 오류")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise Problem("JSON 객체가 필요합니다.")
                if self.path == "/api/tune/apply":
                    try:
                        manager.apply_tuning(body)
                    except (Problem, OSError, ValueError) as error:
                        self.reply(409, {"error": str(error)})
                        return
                    self.reply(200, {"ok": True})
                    return
                if self.path == "/api/cancel":
                    manager.cancel_work()
                    self.reply(200, {"ok": True})
                    return
                if self.path == "/api/install" and body.get("source", "download") != "download":
                    raise Problem("공식 다운로드 설치만 지원합니다.")
                if self.path == "/api/connection" and (set(body) != {"name", "enable"}
                        or body["name"] not in ("pi", "opencode") or type(body["enable"]) is not bool):
                    raise Problem("Pi/opencode 이름과 boolean enable만 전달하세요. 경로·설정값은 받지 않습니다.")
                actions = {
                    "/api/install": manager.install,
                    "/api/models/pull": lambda: manager.model_command("pull"),
                    "/api/models/verify": lambda: manager.model_command("doctor"),
                    "/api/engine/start": manager.start,
                    "/api/engine/stop": manager.stop,
                    "/api/cache/clear": manager.clear_cache,
                    "/api/tune": lambda: manager.tune(current_only=body.get("mode") == "baseline"),
                    "/api/settings": lambda: manager.save(body),
                    "/api/integration": lambda: manager.integration(body.get("name"), body.get("enable")),
                    "/api/connection": lambda: manager.connection(body["name"], body["enable"]),
                    "/api/alias": lambda: manager.alias(body.get("name"), body.get("enable")),
                }
                action = actions.get(self.path)
                if action is None:
                    self.reply(404, {"error": "Not found"})
                    return
                manager.submit(self.path, action)
                self.reply(202, {"ok": True})
            except (Problem, ValueError, TypeError) as error:
                self.reply(400, {"error": str(error)})

        def do_OPTIONS(self):
            self.reply(403, {"error": "Cross-origin access disabled"})

    return Server(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Flint Kit loopback control server")
    parser.add_argument("--port", type=int, default=web_port())
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    os.environ["FLINT_WEB_PORT"] = str(args.port)
    os.umask(0o077)
    manager = Manager()
    # A process-wide flock prevents two dashboards managing the same data directory.
    import fcntl
    lock = (manager.home / "controller.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, "Flint Kit이 이미 실행 중입니다.\n")
    try:
        server = make_server(manager, args.port)
    except OSError as error:
        parser.exit(1, f"웹 포트를 열 수 없습니다: {error}\n")

    def shutdown(signum, frame):
        manager.closing = True
        manager.cancel.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    atexit.register(manager.stop)
    print(f"Flint Kit → http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        manager.closing = True
        manager.cancel.set()
        # Do not let a tuning worker restart the engine after shutdown.
        with manager.lock:
            manager.stop()
        server.server_close()
        lock.close()


if __name__ == "__main__":
    main()
