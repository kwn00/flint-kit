# Modified from recursive-oss/rec 0.1.1 (Apache-2.0) for bable-flint: pins Claude Code's billing stamp and moves opencode's <env> block so the prefix cache reuses system prompts. See NOTICE.
import hashlib
import json
import re
import uuid
from array import array
from importlib.resources import files
from itertools import count

from transformers import PreTrainedTokenizerFast


def load_tokenizer(directory):
    tokenizer = PreTrainedTokenizerFast.from_pretrained(
        directory, local_files_only=True, fix_mistral_regex=False
    )
    tokenizer.chat_template = files("rec_mlx").joinpath("chat_template.jinja").read_text()
    return tokenizer


BILLING_HEADER = "x-anthropic-billing-header:"
ENV_HEADER = "environment you are running in:"


def pin_billing_stamp(system):
    """Claude Code starts its system prompt with `x-anthropic-billing-header: cc_version=2.1.170.bf4;
    cc_entrypoint=cli; cch=b145e;`. cch changes on every request and the version's 4th part on every
    session, so the prefix cache could never reuse the system prompt. Both stamps are pinned to f's
    (as llama.cpp and Strata do); only a header at the very start, within its first 160 characters."""
    if not system.startswith(BILLING_HEADER):
        return system
    s = list(system)
    cch = system.find("cch=", len(BILLING_HEADER))
    if 0 <= cch <= 160:
        v, end = cch + 4, system.find(";", cch + 4)
        if end > v and end - v <= 16:
            s[v:end] = "f" * (end - v)
    cv = system.find("cc_version=")
    if 0 <= cv <= 160:
        v, end = cv + len("cc_version="), system.find(";", cv)
        if v < end and end - v <= 64:
            parts = system[v:end].split(".")
            if len(parts) > 3:
                tail = v + len(".".join(parts[:3])) + 1
                s[tail:end] = "f" * (end - tail)
    return "".join(s)


def _env_span(system):
    """Span of the first `<env>\n...\n</env>` block with its trailing newline, and the line above it when
    that line ends with ENV_HEADER. str.find keeps this linear: the regex it replaces began with `[^\n]*`
    and rescanned a long line from every position (3.4 s on an 83K-character one-line system prompt)."""
    start = system.find("<env>\n")
    if start < 0:
        return None
    end = system.find("\n</env>", start + 6)
    if end < 0:
        return None
    end += len("\n</env>")
    if system.startswith("\n", end):
        end += 1
    if start and system[start - 1] == "\n":
        line = system.rfind("\n", 0, start - 1) + 1
        if system.endswith(ENV_HEADER, line, start - 1):
            start = line
    return start, end


def env_to_user(messages):
    """opencode writes an <env> block (working directory, today's date) near the start of its system
    message, ahead of instructions and tools that are the same in every session. Moved to the front of the
    first user message, the system turn is the same for every folder and day and the prefix cache keeps it."""
    if not messages:
        return messages
    system = messages[0].get("content")
    if messages[0].get("role") != "system" or not isinstance(system, str):
        return messages
    span = _env_span(system)
    if span is None:
        return messages
    start, end = span
    for index, message in enumerate(messages[1:], 1):
        if message.get("role") != "user":
            continue
        block = system[start:end].rstrip("\n")
        content = message.get("content")
        if isinstance(content, str):
            content = block + "\n\n" + content
        elif isinstance(content, list):
            content = [{"type": "text", "text": block + "\n\n"}] + content
        else:
            return messages
        out = list(messages)
        out[0] = {**messages[0], "content": system[:start] + system[end:]}
        out[index] = {**message, "content": content}
        return out
    return messages


def normalize_messages(messages):
    if messages and messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str):
        messages = [{**messages[0], "content": pin_billing_stamp(messages[0]["content"])}, *messages[1:]]
    messages = env_to_user(messages)
    normalized = []
    for original in messages:
        message = original
        if message.get("role") != "assistant" and isinstance(message.get("content"), str):
            text = (
                message["content"]
                .replace("\x00", "")
                .replace("<think>", "")
                .replace("</think>", "")
            )
            text = re.sub(
                r"<\|(?:begin_of_text|end_of_text|system|developer|user|assistant|observation)\|>",
                lambda match: match.group().replace("<", "&lt;").replace(">", "&gt;"),
                text,
            )
            message = {**message, "content": text}
        calls = message.get("tool_calls")
        if calls:
            normalized_calls = []
            for call in calls:
                function = call.get("function", call)
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    function = {
                        **function,
                        "arguments": json.loads(arguments) if arguments.strip() else {},
                    }
                normalized_calls.append(
                    {**call, "function": function} if "function" in call else function
                )
            message = {**message, "tool_calls": normalized_calls}
        normalized.append(message)
    return normalized


_CALL = re.compile(
    r"\s*([^<\s]+)\s*((?:<arg_key>.*?</arg_key>\s*<arg_value>.*?</arg_value>\s*)*)", re.S
)
_ARGUMENT = re.compile(r"<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>", re.S)


def _accepts(schema, kind):
    types = schema.get("type")
    if types == kind or (isinstance(types, list) and kind in types):
        return True
    if kind == "string" and any(isinstance(value, str) for value in schema.get("enum", ())):
        return True
    return any(
        _accepts(option, kind) for key in ("anyOf", "oneOf") for option in schema.get(key, ())
    )


def parse_tool_call(text, tools):
    """Invert the template's tool-call rendering: string arguments are raw, others are JSON."""
    call = _CALL.fullmatch(text)
    if call is None:
        raise ValueError(f"Malformed Flint tool call: {text!r}")
    name = call[1]
    properties = {}
    for tool in tools or ():
        if tool["function"]["name"] == name:
            properties = tool["function"].get("parameters", {}).get("properties", {})
    arguments = {}
    for key, value in _ARGUMENT.findall(call[2]):
        schema = properties.get(key, {})
        if value == "null" and _accepts(schema, "null"):
            value = None
        elif not _accepts(schema, "string"):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        arguments[key] = value
    return {"name": name, "arguments": arguments}


def seeded_tool_parser(sampling, prompt_tokens):
    """Parse tool calls with IDs derived from the sample, prompt, and call order.

    The sampler already replays a request exactly for a given seed; deriving the IDs the same
    way makes the whole replayed message identical instead of differing in random UUIDs.
    The sample config is part of the hash, so a different temperature or other sampling
    parameter does not reuse IDs from another sample of the same prompt.
    """
    config = json.dumps(
        {
            "temperature": float(sampling.temperature),
            "top_p": float(sampling.top_p),
            "top_k": sampling.top_k,
            "presence_penalty": float(sampling.presence_penalty),
            "max_tokens": sampling.max_tokens,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    request = hashlib.sha256(sampling.seed.to_bytes(8, "little"))
    request.update(config)
    request.update(array("I", prompt_tokens).tobytes())
    indices = count()

    def parse(text, tools):
        call = parse_tool_call(text, tools)
        digest = request.copy()
        digest.update(next(indices).to_bytes(4, "little"))
        call["id"] = str(uuid.UUID(bytes=digest.digest()[:16], version=4))
        return call

    return parse
