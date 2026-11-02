# Added by bable-flint (not part of recursive-oss/rec): the OpenAI Responses API. Apache-2.0, see NOTICE.
"""/v1/responses on top of the chat path, the way anthropic.py serves /v1/messages: a request becomes a chat
request, and the chat answer (JSON or its SSE chunks) becomes a Responses object or Responses events. Codex speaks
only this API. Nothing is stored, so previous_response_id is refused: clients send the whole conversation, as Codex
does with store=false. Reasoning goes out as reasoning items with plain reasoning_text, and comes back to the
model from those items."""

import json
import logging

_MAPPED = {
    "disk_cache",  # Flint's own: false keeps this prompt out of the disk prompt cache (the Codex relay's summaries)
    "input",
    "instructions",
    "max_output_tokens",
    "model",
    "parallel_tool_calls",
    "stream",
    "temperature",
    "text",
    "tool_choice",
    "tools",
    "top_p",
}
# Sent by Codex and other clients; they ask for nothing Flint has to do.
_QUIET = {"client_metadata", "include", "metadata", "prompt_cache_key", "reasoning", "store", "user"}

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
    503: "server_overloaded",
}


def responses_error(status, message):
    return {"error": {"message": message, "type": _ERROR_TYPES.get(status, "server_error"), "param": None,
                      "code": None}}


def responses_to_chat(body):
    """The chat request for a Responses request, and the request fields Flint does not use."""
    if not isinstance(body, dict):
        raise ValueError("Request should be a JSON dictionary")
    if "model" not in body:
        raise ValueError("model is required")
    if body.get("previous_response_id") is not None:
        raise ValueError("previous_response_id is not supported: this server stores nothing; send the whole input")
    ignored = set(body) - _MAPPED - _QUIET - {"previous_response_id"}
    messages = _input(body.get("input"), ignored)
    instructions = body.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        if instructions:
            messages.insert(0, {"role": "system", "content": instructions})
    if not messages:
        raise ValueError("input must hold at least one message")
    chat = {"model": body["model"], "messages": messages}
    if body.get("max_output_tokens") is not None:
        chat["max_tokens"] = body["max_output_tokens"]
    # the chat path checks tool_choice and disk_cache
    for name in ("temperature", "top_p", "parallel_tool_calls", "tool_choice", "disk_cache"):
        if body.get(name) is not None:
            chat[name] = body[name]
    if body.get("stream") is True:
        chat["stream"] = True
        chat["stream_options"] = {"include_usage": True}
    text = body.get("text")
    if text is not None:
        kind = ((text.get("format") or {}).get("type") if isinstance(text, dict) else None) or "text"
        if kind != "text":
            raise ValueError(f"text.format {kind!r} is not supported; only plain text answers")
    if body.get("tools"):
        tools = _tools(body["tools"], ignored)
        if tools:
            chat["tools"] = tools
    return chat, ignored


def chat_to_response(response, *, created_at):
    choice = response["choices"][0]
    message = choice.get("message") or {}
    rid = _response_id(response.get("id"))
    output = []
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if reasoning:
        output.append(_reasoning_item(f"rs_{rid}_0", reasoning))
    if message.get("content"):
        output.append(_message_item(f"msg_{rid}_{len(output)}", message["content"]))
    for call in message.get("tool_calls") or []:
        output.append(_call_item(f"fc_{rid}_{len(output)}", call))
    return _response(rid, response.get("model", "flint"), created_at, output, choice.get("finish_reason"),
                     response.get("usage"))


class ErrorTranslator:
    """Rewrites a chat error body from mlx-lm into the Responses error shape."""

    def __init__(self, raw, status):
        self.raw = raw
        self.status = status

    def write(self, data):
        self.raw.write(_error_body(data, self.status()) if data[:1] == b"{" else data)
        return len(data)

    def flush(self):
        return self.raw.flush()

    def __getattr__(self, name):
        return getattr(self.raw, name)


class ResponseEventStream:
    """Turns mlx-lm's chat SSE chunks into Responses events: response.created, an output_item.added / deltas /
    output_item.done run per reasoning, message or function call, then response.completed (response.incomplete
    when the answer hit its token limit) with the whole output and the usage."""

    def __init__(self, raw, *, created_at, status=lambda: 500):
        self.raw = raw
        self.status = status
        self.created_at = created_at
        self.pending = b""
        self.header_pending = b""
        self.headers_sent = False
        self.started = False
        self.rid = "0"
        self.model = "flint"
        self.output = []
        self.item = None
        self.text = ""
        self.finish = None
        self.usage = None
        self.sequence = 0
        self.done = False

    def write(self, data):
        if not self.headers_sent and (self.header_pending or data.startswith(b"HTTP/")):
            self.header_pending += data
            marker = b"\r\n\r\n"
            if marker not in self.header_pending:
                return len(data)
            head, data = self.header_pending.split(marker, 1)
            self.header_pending = b""
            self.headers_sent = True
            self.raw.write(head + marker)
            self.raw.flush()
            if not data:
                return len(data)
        self.headers_sent = True
        if not self.pending and data[:1] == b"{":
            self.raw.write(_error_body(data, self.status()))
            return len(data)
        self.pending += data
        while b"\n\n" in self.pending:
            event, self.pending = self.pending.split(b"\n\n", 1)
            self._event(event)
        return len(data)

    def flush(self):
        return self.raw.flush()

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def _event(self, event):
        if event.startswith(b":"):
            self.raw.write(event + b"\n\n")
            self.raw.flush()
            return
        if not event.startswith(b"data: "):
            return
        payload = event[6:]
        if payload == b"[DONE]":
            self._finish()
            return
        self._chunk(json.loads(payload))

    def _chunk(self, chunk):
        if not self.started:
            self.rid = _response_id(chunk.get("id"))
            self.model = chunk.get("model", self.model)
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return
        self._start()
        choice = choices[0]
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if reasoning:
            self._open("reasoning")
            self.text += reasoning
            self._emit("response.reasoning_text.delta", {**self._where(), "content_index": 0, "delta": reasoning})
        content = delta.get("content")
        if content:
            self._open("message")
            self.text += content
            self._emit("response.output_text.delta",
                       {**self._where(), "content_index": 0, "delta": content, "logprobs": []})
        for call in delta.get("tool_calls") or []:
            self._close()
            item = _call_item(f"fc_{self.rid}_{len(self.output)}", call)
            arguments = item["arguments"]
            self._emit("response.output_item.added",
                       {"output_index": len(self.output), "item": {**item, "arguments": "", "status": "in_progress"}})
            where = {"item_id": item["id"], "output_index": len(self.output)}
            self._emit("response.function_call_arguments.delta", {**where, "delta": arguments})
            self._emit("response.function_call_arguments.done", {**where, "arguments": arguments})
            self._emit("response.output_item.done", {"output_index": len(self.output), "item": item})
            self.output.append(item)
        if choice.get("finish_reason"):
            self.finish = choice["finish_reason"]

    def _where(self):
        return {"item_id": self.item["id"], "output_index": len(self.output)}

    def _open(self, kind):
        if self.item is not None and self.item["type"] == kind:
            return
        self._close()
        if kind == "reasoning":
            self.item = {"type": "reasoning", "id": f"rs_{self.rid}_{len(self.output)}", "summary": []}
        else:
            self.item = {"type": "message", "id": f"msg_{self.rid}_{len(self.output)}", "status": "in_progress",
                         "role": "assistant", "content": []}
        self.text = ""
        self._emit("response.output_item.added", {"output_index": len(self.output), "item": self.item})
        if kind == "message":
            self._emit("response.content_part.added",
                       {**self._where(), "content_index": 0, "part": _text_part("")})

    def _close(self):
        if self.item is None:
            return
        where = self._where()
        if self.item["type"] == "reasoning":
            self._emit("response.reasoning_text.done", {**where, "content_index": 0, "text": self.text})
            item = _reasoning_item(self.item["id"], self.text)
        else:
            self._emit("response.output_text.done", {**where, "content_index": 0, "text": self.text, "logprobs": []})
            self._emit("response.content_part.done", {**where, "content_index": 0, "part": _text_part(self.text)})
            item = _message_item(self.item["id"], self.text)
        self._emit("response.output_item.done", {"output_index": len(self.output), "item": item})
        self.output.append(item)
        self.item = None
        self.text = ""

    def _start(self):
        if self.started:
            return
        self.started = True
        response = _response(self.rid, self.model, self.created_at, [], None, None)
        response.update(status="in_progress", usage=None)
        self._emit("response.created", {"response": response})
        self._emit("response.in_progress", {"response": response})

    def _finish(self):
        if self.done:
            return
        self.done = True
        self._start()
        self._close()
        response = _response(self.rid, self.model, self.created_at, self.output, self.finish, self.usage)
        self._emit("response.completed" if response["status"] == "completed" else "response.incomplete",
                   {"response": response})

    def _emit(self, name, payload):
        payload = {"type": name, "sequence_number": self.sequence, **payload}
        self.sequence += 1
        self.raw.write(f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
        self.raw.flush()


def _response(rid, model, created_at, output, finish, usage):
    incomplete = finish == "length"
    return {
        "id": f"resp_{rid}",
        "object": "response",
        "created_at": created_at,
        "status": "incomplete" if incomplete else "completed",
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
        "error": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "store": False,
        "usage": _usage(usage),
    }


def _usage(usage):
    usage = usage or {}
    prompt, completion = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": prompt + completion,
    }


def _response_id(value):
    return str(value or "0").removeprefix("chatcmpl-")


def _text_part(text):
    return {"type": "output_text", "text": text, "annotations": [], "logprobs": []}


def _message_item(item_id, text):
    return {"type": "message", "id": item_id, "status": "completed", "role": "assistant",
            "content": [_text_part(text)]}


def _reasoning_item(item_id, text):
    return {"type": "reasoning", "id": item_id, "summary": [], "content": [{"type": "reasoning_text", "text": text}]}


def _call_item(item_id, call):
    function = call.get("function", call)
    arguments = function.get("arguments") or "{}"
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"type": "function_call", "id": item_id, "call_id": call.get("id") or item_id,
            "name": function.get("name") or "", "arguments": arguments, "status": "completed"}


def _error_body(data, status):
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return data
    # mlx-lm's errors are {"error": "..."}; a Response object carries "error": null and passes as it is
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), str):
        return data
    return json.dumps(responses_error(status if status >= 400 else 500, payload["error"])).encode()


def _tools(tools, ignored):
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")
    converted = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("each tool must be an object")
        kind = tool.get("type")
        if kind != "function":
            # Codex also offers web search and namespaced sub-agent tools; Flint cannot run the first and Codex
            # would not route a flat call to the second, so the model never sees them.
            ignored.add(f"tools[type={kind}]")
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("function tool name must be a nonempty string")
        parameters = tool.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise ValueError(f"tool {name!r} parameters must be an object")
        function = {"name": name, "parameters": parameters}
        if isinstance(tool.get("description"), str):
            function["description"] = tool["description"]
        converted.append({"type": "function", "function": function})
    return converted


_media_warned = set()


def _media(kind):
    # Flint reads text only; failing the request would fail every later turn whose history holds the item.
    if kind not in _media_warned:
        _media_warned.add(kind)
        logging.warning("Replacing %s content with a note; this model reads text only", kind)
    return f"[{kind} omitted: this model reads text only]"


def _content(content, where):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError(f"{where} content must be a string or a list of parts")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError(f"{where} content parts must be objects")
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text", "summary_text", "reasoning_text"):
            if not isinstance(part.get("text"), str):
                raise ValueError(f"{kind} parts require a string")
            parts.append(part["text"])
        elif kind == "refusal":
            parts.append(str(part.get("refusal", "")))
        elif kind in ("input_image", "input_file", "input_audio"):
            parts.append(_media(kind))
        else:
            raise ValueError(f"Unsupported {where} content part {kind!r}")
    return "\n\n".join(parts)


def _input(items, ignored):
    if isinstance(items, str):
        return [{"role": "user", "content": items}]
    if not isinstance(items, list):
        raise ValueError("input must be a string or a list of items")
    messages = []
    reasoning = []

    def assistant():
        """The assistant message a call joins: the last message when it is the assistant's, else a new one."""
        if not messages or messages[-1]["role"] != "assistant":
            messages.append({"role": "assistant", "content": ""})
        if reasoning:
            messages[-1]["reasoning_content"] = messages[-1].get("reasoning_content", "") + "".join(reasoning)
            reasoning.clear()
        return messages[-1]

    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each input item must be an object")
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            text = _content(item.get("content"), "message")
            if role == "assistant":
                message = {"role": "assistant", "content": text}
                messages.append(message)
                assistant()
            elif role in ("user", "system", "developer"):
                # Developer text is instructions; Flint's template renders system messages where they stand.
                messages.append({"role": "user" if role == "user" else "system", "content": text})
                reasoning.clear()  # reasoning of a turn that ended without an answer belongs to no later one
            else:
                raise ValueError("message roles must be user, assistant, system, or developer")
        elif kind == "reasoning":
            text = _content(item.get("content") or [], "reasoning") or _content(item.get("summary") or [],
                                                                               "reasoning")
            if text:
                reasoning.append(text)
        elif kind in ("function_call", "custom_tool_call"):
            call_id, name = item.get("call_id"), item.get("name")
            if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                raise ValueError(f"{kind} requires call_id and name")
            if kind == "function_call":
                arguments = item.get("arguments", "{}")
                if not isinstance(arguments, str):
                    raise ValueError("function_call arguments must be a string")
            else:
                arguments = json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
            message = assistant()
            message.setdefault("tool_calls", []).append(
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments or "{}"}}
            )
        elif kind in ("function_call_output", "custom_tool_call_output"):
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ValueError(f"{kind} requires a call_id")
            output = item.get("output", "")
            if isinstance(output, dict):  # {content, success} from some clients
                output = output.get("content", "")
            messages.append({"role": "tool", "tool_call_id": call_id, "content": _content(output, kind)})
        else:
            ignored.add(f"input[type={kind}]")
    return messages
