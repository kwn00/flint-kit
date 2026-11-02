# Modified from recursive-oss/rec 0.1.1 (Apache-2.0) for bable-flint: server defaults from REC_TEMPERATURE,
# REC_TOP_P, REC_TOP_K, REC_PRESENCE_PENALTY, an answer cap from REC_OUTPUT_LIMIT, and /v1/responses (responses.py)
# for Codex, the model name claude-flint for the Claude desktop app, tool_choice=none keeping the tools in the prompt
# under Flint grammar, and disk_cache=false. See NOTICE.
import dataclasses
# Modified for Flint Local (2026-10-30): strict loopback Host/Origin checks and
# loopback-only binding. No browser-origin inference requests are accepted.
import errno
import hmac
import os
import io
import json
import logging
import select
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from socketserver import TCPServer

from .anthropic import (
    ErrorTranslator,
    MessageEventStream,
    anthropic_error,
    chat_to_message,
    messages_to_chat,
)
from .engine import Sampling
from .responses import ErrorTranslator as ResponsesErrorTranslator
from .responses import ResponseEventStream, chat_to_response, responses_error, responses_to_chat


class RequestError(ValueError):
    pass


class Server(ThreadingHTTPServer):
    # The default backlog of 5 resets a burst of connections before the accept loop takes them
    # (recursive-oss/rec main after 0.1.1, "Raise the HTTP server's listen backlog to 32").
    request_queue_size = 32

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


# The Claude desktop app takes only model IDs that look like Claude's, so Flint.app hands it "claude-flint"
SERVED_MODELS = ("flint", "claude-flint")


def known_thinking_display(body):
    """`body` with a thinking.display rec does not know left out, and the value dropped (None when none was).
    Claude Code sends newer display values on some background requests; rec refuses anything but summarized or
    omitted, and without the key Flint's reasoning is shown as with summarized."""
    thinking = body.get("thinking") if isinstance(body, dict) else None
    if not isinstance(thinking, dict) or thinking.get("display", "summarized") in ("summarized", "omitted"):
        return body, None
    kept = {key: value for key, value in thinking.items() if key != "display"}
    return {**body, "thinking": kept}, thinking["display"]


def served_model(name, anthropic):
    """Whether `name` is served. On /v1/messages a Claude model name is too: Claude Code sends some of its own
    background requests under its built-in names even when every alias points at flint, and Flint is the only model
    here, so they run on it instead of failing."""
    return name in SERVED_MODELS or (anthropic and isinstance(name, str) and name.startswith("claude-"))


def env_defaults(defaults):
    """The app's server-wide settings. REC_TEMPERATURE, REC_TOP_P, REC_TOP_K and REC_PRESENCE_PENALTY replace the
    sampling a request leaves out (a value the request sends still wins); REC_OUTPUT_LIMIT caps every answer, even
    when a request asks for more. A bad value stops the server at start rather than being ignored."""
    changes = {}
    for name, key, kind in (
        ("temperature", "REC_TEMPERATURE", float),
        ("top_p", "REC_TOP_P", float),
        ("top_k", "REC_TOP_K", int),
        ("presence_penalty", "REC_PRESENCE_PENALTY", float),
    ):
        raw = os.environ.get(key, "")
        if raw:
            try:
                changes[name] = kind(raw)
            except ValueError:
                raise ValueError(f"{key}={raw!r} is not {'an integer' if kind is int else 'a number'}") from None
    try:
        defaults = dataclasses.replace(defaults, **changes)  # Sampling checks the ranges
    except ValueError as error:
        raise ValueError(f"{error} (from {', '.join(f'REC_{n.upper()}' for n in changes)})") from None
    raw = os.environ.get("REC_OUTPUT_LIMIT", "")
    limit = None
    if raw:
        limit = int(raw) if raw.isdigit() else 0
        if limit < 1:
            raise ValueError(f"REC_OUTPUT_LIMIT={raw!r} must be a positive integer")
    return defaults, limit


def answer_tokens(requested, output_limit, remaining):
    """Tokens an answer may use: what the request asked for (None when it did not ask), held to REC_OUTPUT_LIMIT
    (None when unset) and to the room the prompt leaves in the context."""
    return min(tokens for tokens in (requested, output_limit, remaining) if tokens is not None)


def make_server(
    engine, address, metrics_path, *, defaults=Sampling(), api_key=None, grammar_mode="flint"
):
    from mlx_lm.server import APIHandler

    from .scheduler import Generator

    defaults, output_limit = env_defaults(defaults)

    admission = threading.BoundedSemaphore(9)
    ignored_fields = set()
    ignored_fields_lock = threading.Lock()

    def note_ignored(fields):
        with ignored_fields_lock:
            for field in sorted(fields - ignored_fields):
                logging.warning("Ignoring unsupported request field %r", field)
            ignored_fields.update(fields)

    class Handler(APIHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(
                generator, *args, system_fingerprint=engine.identity["model_id"], **kwargs
            )

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authenticated(self):
            hosts = self.headers.get_all("Host") or []
            if (hosts != [f"127.0.0.1:{self.server.server_port}"]
                    or self.headers.get("Origin") is not None
                    or self.headers.get("Upgrade") is not None
                    or self.headers.get("Sec-Fetch-Site", "none") not in ("none", "same-origin")):
                self.reply(403, {"error": "Local CLI requests only"})
                return False
            benchmark = os.environ.get("FLINT_BENCHMARK_TOKEN", "")
            if benchmark and not hmac.compare_digest(
                    self.headers.get("X-Flint-Benchmark", "").encode(), benchmark.encode()):
                self.reply(503, {"error": "Exclusive local benchmark in progress"})
                return False
            if api_key is None:
                return True
            expected = ("Bearer " + api_key).encode()
            if hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
                return True
            if self.route() == "/v1/messages" and hmac.compare_digest(
                self.headers.get("x-api-key", "").encode(), api_key.encode()
            ):
                return True
            self._reject(401, "Invalid or missing bearer token")
            return False

        def route(self):
            # The Anthropic SDK and Claude Code post to /v1/messages?beta=true.
            return self.path.split("?", 1)[0]

        def _reject(self, status, message):
            if status == 400:  # the client often shows only the status; the reason stays in serve.log
                logging.warning("Refused %s %s: %s", self.command, self.route(), message)
            if self.route() == "/v1/messages" or getattr(self, "anthropic", False):
                self.reply(status, anthropic_error(status, message))
            elif self.route() == "/v1/responses" or getattr(self, "responses_api", False):
                self.reply(status, responses_error(status, message))
            else:
                self.reply(status, {"error": message})

        def do_HEAD(self):
            # Claude Code checks that the server answers (HEAD /api/hello) before it starts; nothing is read or sent.
            if not self.authenticated():
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self.authenticated():
                return
            # Codex asks for /v1/models?client_version=...
            if self.route() == "/health":
                stopping = generator.stopping.is_set()
                self.reply(
                    503 if stopping else 200,
                    {
                        "status": "stopping" if stopping else "ready",
                        "busy": generator.busy.is_set(),
                        "queued": generator.requests.qsize(),
                    },
                )
            elif self.route() == "/v1/models":
                self.reply(200, {"object": "list", "data": [{"id": "flint", "object": "model"}]})
            else:
                self.reply(404, {"error": "Unknown endpoint"})

        def do_POST(self):
            if not self.authenticated():
                return
            if self.route() not in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
                self._reject(404, "Unknown endpoint")
                return
            try:
                if not 0 < int(self.headers["Content-Length"]) <= 8 * 1024 * 1024:
                    raise ValueError("Request body must be between 1 byte and 8 MiB")
            except (ValueError, TypeError) as error:
                self._reject(400, str(error))
                return
            if generator.stopping.is_set():
                self._reject(503, "The server is stopping")
                return
            if not admission.acquire(blocking=False):
                self._reject(429, "The request queue is full; retry after a generation finishes")
                return
            try:
                if self.route() == "/v1/messages":
                    self.anthropic = True
                    self._prepare_messages()
                elif self.route() == "/v1/responses":
                    self.responses_api = True
                    self.created_at = int(time.time())
                    self._prepare(responses_to_chat)
                super().do_POST()
            except RequestError as error:
                self._reject(400, str(error))
            except (BrokenPipeError, ConnectionResetError):
                logging.info("Client disconnected; cancelling its generation")
            finally:
                admission.release()

        def _prepare_messages(self):
            def translate(body):
                body, dropped = known_thinking_display(body)
                if dropped is not None:
                    note_ignored({f"thinking.display={dropped!r}"})
                chat, ignored, self.anthropic_thinking = messages_to_chat(body)
                return chat, ignored

            chat = self._prepare(translate)
            self.anthropic_stops = tuple(chat.get("stop") or ())

        def _prepare(self, translate):
            """Reads the body, turns it into a chat request with `translate` and serves it as one."""
            length = int(self.headers["Content-Length"])
            try:
                body = json.loads(self.rfile.read(length).decode())
                chat, ignored = translate(body)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise RequestError(f"Invalid JSON in request body: {error}") from error
            except (ValueError, TypeError, KeyError, AttributeError) as error:  # e.g. text.format as a string
                raise RequestError(str(error)) from error
            note_ignored(ignored)
            encoded = json.dumps(chat).encode()
            self.rfile = io.BytesIO(encoded)
            self.headers.replace_header("Content-Length", str(len(encoded)))
            self.path = "/v1/chat/completions"
            return chat

        def is_disconnected(self):
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)
            except ValueError:
                # The handler can close its socket while the generation worker checks it.
                return True
            except OSError as error:
                if error.errno not in (errno.EBADF, errno.ECONNRESET):
                    raise
                return True

        def _set_completion_headers(self, status):
            # mlx-lm maps failures before generation to 404, including a stopped worker.
            if status == 404 and generator.stopping.is_set():
                status = 503
            self.completion_status = status
            super()._set_completion_headers(status)

        def generate_response(self, *args, **kwargs):
            response = super().generate_response(*args, **kwargs)
            for choice in response["choices"]:
                if "logprobs" in choice:
                    for entry in choice["logprobs"]["content"]:
                        entry.update(
                            token=engine.tokenizer.decode([entry["id"]], skip_special_tokens=False),
                            bytes=None,
                            top_logprobs=[],
                        )
            if getattr(self, "anthropic", False) and not self.stream:
                return chat_to_message(
                    response,
                    stop_sequences=self.anthropic_stops,
                    include_thinking=self.anthropic_thinking,
                )
            if getattr(self, "responses_api", False) and not self.stream:
                return chat_to_response(response, created_at=self.created_at)
            return response

        def validate_model_parameters(self):
            try:
                self.validate_request()
            except (ValueError, TypeError, KeyError) as error:
                raise RequestError(str(error)) from error

        def drop_unknown_fields(self):
            allowed = {
                "model",
                "messages",
                "tools",
                "tool_choice",
                "stream",
                "stream_options",
                "temperature",
                "top_p",
                "top_k",
                "max_tokens",
                "max_completion_tokens",
                "seed",
                "stop",
                "frequency_penalty",
                "repetition_penalty",
                "presence_penalty",
                "logprobs",
                "min_p",
                "store",
                "grammar",
                "parallel_tool_calls",
                "disk_cache",
            }
            unknown = set(self.body) - allowed
            for field in unknown:
                del self.body[field]
            note_ignored(unknown)
            self.requested_draft_model = "default_model"
            self.num_draft_tokens = self.response_generator.cli_args.num_draft_tokens
            self.adapter = None
            self.repetition_context_size = 20
            self.presence_context_size = 20
            self.frequency_context_size = 20
            self.xtc_probability = 0.0
            self.xtc_threshold = 0.0
            self.logit_bias = None
            self.top_logprobs = -1
            self.chat_template_kwargs = None

        def validate_request(self):
            self.drop_unknown_fields()
            if "presence_penalty" not in self.body:  # mlx-lm reads 0 here, not the server default
                self.presence_penalty = defaults.presence_penalty
            super().validate_model_parameters()
            if self.stream and self.logprobs:
                raise ValueError("Token logprobs require stream=false")
            if not served_model(self.requested_model, getattr(self, "anthropic", False)):
                raise ValueError("The served model is flint")
            for key in ("frequency_penalty", "min_p"):
                if self.body.get(key, 0) != 0:
                    raise ValueError(f"Nonzero {key} is not supported by this decoder")
            if self.body.get("repetition_penalty", 1) != 1:
                raise ValueError("Only neutral repetition_penalty=1 is supported")
            if self.body.get("store", False) is not False:
                raise ValueError("Stored-completion retrieval is not supported; use store=false")
            stop = self.body.get("stop")
            if stop is not None:
                stops = [stop] if isinstance(stop, str) else stop
                if not isinstance(stops, list) or any(
                    not isinstance(word, str) or not word for word in stops
                ):
                    raise ValueError("stop must be a nonempty string or a list of nonempty strings")
            choice = self.body.get("tool_choice", "auto")
            if choice not in ("auto", "none"):
                raise ValueError(
                    "tool_choice currently supports auto and none; forced calls are not implemented"
                )
            mode = self.body["grammar"] if "grammar" in self.body else grammar_mode
            if mode not in ("none", "flint"):
                raise ValueError("grammar must be none or flint")
            # The template renders the tools ahead of the conversation, so dropping them would share no prefix
            # with the turns before (a compaction summary read the whole history again). Flint grammar keeps tool
            # calls out instead; without it, they leave the prompt as in rec.
            self.tools_in_prompt_only = choice == "none" and mode == "flint"
            if choice == "none" and mode != "flint":
                self.body.pop("tools", None)
            if type(self.body.get("disk_cache", True)) is not bool:
                raise ValueError("disk_cache must be a boolean")
            if mode == "flint" and self.body.get("stop"):
                raise ValueError("Flint grammar cannot be combined with custom stop strings")
            if (
                "parallel_tool_calls" in self.body
                and type(self.body["parallel_tool_calls"]) is not bool
            ):
                raise ValueError("parallel_tool_calls must be a boolean")
            if self.body.get("parallel_tool_calls") is False:
                raise ValueError("parallel_tool_calls=false is not supported")
            messages = self.body["messages"]
            if not isinstance(messages, list) or not messages:
                raise ValueError("messages must be a nonempty list")
            for message in messages:
                if message["role"] not in ("system", "developer", "user", "assistant", "tool"):
                    raise ValueError("Unsupported message role")
            if self.stream_options is not None and self.stream_options != {"include_usage": True}:
                raise ValueError("stream_options must be {include_usage: true}")

        def handle_completion(self, request, stop_words):
            from mlx_lm.server import process_message_content

            from .engine import ContextLimitError

            try:
                for message in request.messages:
                    if "reasoning" in message:
                        if "reasoning_content" in message:
                            raise ValueError("Use one reasoning field per message")
                        message["reasoning_content"] = message.pop("reasoning")
                process_message_content(request.messages)
                mode = self.body["grammar"] if "grammar" in self.body else grammar_mode
                request.constraint = None
                if mode == "flint":
                    try:
                        request.constraint = engine.constraints.tools(
                            None if getattr(self, "tools_in_prompt_only", False) else request.tools
                        )
                    except RuntimeError as error:
                        raise ValueError(f"Grammar compilation failed: {error}") from error
                ids = engine.prompt(request.messages, tools=request.tools)
                remaining = engine.context_limit - len(ids)
                if remaining < 1:
                    raise ContextLimitError("The prompt fills the configured context limit")
                explicit_maximum = self.body.get("max_completion_tokens")
                if explicit_maximum is None:
                    explicit_maximum = self.body.get("max_tokens")
                self.max_tokens = answer_tokens(explicit_maximum, output_limit, remaining)
                Sampling(
                    temperature=self.temperature,
                    top_p=self.top_p,
                    top_k=self.top_k,
                    presence_penalty=self.presence_penalty,
                    max_tokens=self.max_tokens,
                    seed=defaults.seed if self.seed is None else self.seed,
                )
            except (ValueError, TypeError, KeyError) as error:
                self._reject(400, str(error))
                return
            if getattr(self, "anthropic", False):
                status = lambda: getattr(self, "completion_status", 500)  # noqa: E731
                if self.stream:
                    self.wfile = MessageEventStream(
                        self.wfile,
                        input_tokens=len(ids),
                        status=status,
                        stop_sequences=self.anthropic_stops,
                        include_thinking=self.anthropic_thinking,
                    )
                else:
                    self.wfile = ErrorTranslator(self.wfile, status)
            elif getattr(self, "responses_api", False):
                status = lambda: getattr(self, "completion_status", 500)  # noqa: E731
                if self.stream:
                    self.wfile = ResponseEventStream(self.wfile, created_at=self.created_at, status=status)
                else:
                    self.wfile = ResponsesErrorTranslator(self.wfile, status)
            request.prompt_tokens = ids
            request.disk_cache = self.body.get("disk_cache", True)  # false: a prompt never sent again
            request.is_disconnected = self.is_disconnected
            request.generation_context = None
            try:
                super().handle_completion(request, stop_words)
            finally:
                # Headers can fail before mlx-lm enters its streaming cleanup block.
                if request.generation_context is not None:
                    request.generation_context.stop()

    # rec always binds 127.0.0.1; the app sets REC_HOST=0.0.0.0 when 내부망 접속 is on. Requests still need the key.
    address = ("127.0.0.1", address[1])
    server = Server(address, Handler)
    generator = Generator(engine, metrics_path, defaults)
    server.generator = generator
    return server
