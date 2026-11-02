"""Explicit opt-in real engine smoke. Does not install/download or alter client settings.

Run only after local engine/model preparation:
  python3 -m tests.real_smoke [--baseline]
"""
import argparse
import http.client
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="Run one complete real tuner trial")
    args = parser.parse_args()
    port = 11539
    cookie = ""
    csrf = ""
    admission_checked = False
    origin = f"http://127.0.0.1:{port}"

    def call(method, route, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json", "Origin": origin}
        if cookie:
            headers.update(Cookie=cookie, **{"X-Flint-CSRF": csrf})
        conn.request(method, route, json.dumps(body) if body is not None else None, headers)
        res = conn.getresponse()
        data = json.loads(res.read())
        response_headers = dict(res.getheaders())
        conn.close()
        if res.status not in (200, 202):
            raise RuntimeError(f"{route}: {res.status} {data}")
        return data, response_headers

    session, headers = call("GET", "/api/session")
    csrf = session["csrf"]
    cookie = headers["Set-Cookie"].split(";")[0]

    def state():
        return call("GET", "/api/state")[0]

    def wait_job(timeout=600):
        nonlocal admission_checked
        end = time.monotonic() + timeout
        previous = None
        while time.monotonic() < end:
            s = state()
            message = s["job"]["message"]
            if message != previous:
                print(s["job"]["state"], message, flush=True)
                previous = message
            if (s["job"].get("action") == "/api/tune" and s["job"]["state"] == "running"
                    and s["engine"]["ready"] and not admission_checked):
                try:
                    code, _ = engine("GET", "/health")
                    if code == 503:
                        admission_checked = True
                        print("Exclusive benchmark rejects external CLI requests: HTTP 503", flush=True)
                except OSError:
                    # State snapshots can cross a deliberate engine restart.
                    pass
            if s["job"]["state"] != "running":
                if s["job"]["state"] != "done":
                    raise RuntimeError(s["job"])
                return s
            time.sleep(2)
        raise RuntimeError("Job timeout")

    initial = state()
    resuming = initial["job"]["state"] == "running" and initial["job"]["action"] == "/api/tune"
    if resuming and not args.baseline:
        raise RuntimeError("A baseline is already running; use --baseline to await it")
    if not resuming and not initial["engine"]["ready"]:
        call("POST", "/api/engine/start", {})
        current = wait_job()
    else:
        current = initial
    if not resuming:
        assert current["engine"]["ready"]
    key = (Path(current["data_dir"]) / "rec/config/api-key").read_text().strip()
    engine_port = current["settings"]["port"]

    def engine(method, route, payload=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", engine_port, timeout=90)
        h = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
        h.update(headers or {})
        conn.request(method, route, json.dumps(payload) if payload is not None else None, h)
        res = conn.getresponse()
        data = res.read()
        code = res.status
        conn.close()
        return code, json.loads(data) if data else None

    if args.baseline:
        before = state()
        if not resuming:
            call("POST", "/api/tune", {"mode": "baseline"})
        after = wait_job(timeout=1800)
        assert after["settings"] == before["settings"], "Settings were not restored"
        assert after["engine"]["ready"], "Running engine was not restored"
        assert len(after["tuning"]["results"]) == 1
        assert after["tuning"]["results"][0]["score"] > 0
        assert not after["tuning"]["adopted"]
        assert admission_checked, "Exclusive benchmark admission was not verified"
        print("Real one-trial tuning and runtime/settings restoration OK", flush=True)
        print(json.dumps(after["tuning"], indent=2), flush=True)
    assert engine("GET", "/health")[0] == 200
    assert engine("GET", "/health", headers={"Authorization": ""})[0] == 401
    assert engine("GET", "/health", headers={"Origin": "https://evil.example"})[0] == 403
    assert engine("GET", "/health", headers={"Host": "evil.example"})[0] == 403
    code, response = engine("POST", "/v1/chat/completions",
                            dict(model="flint", messages=[{"role": "user", "content": "Reply with OK."}],
                                 max_tokens=96, temperature=0))
    assert code == 200, (code, response)
    assert response["choices"][0]["message"]["content"].strip()
    print("Real chat completion OK; usage:", response["usage"], flush=True)
    code, message = engine("POST", "/v1/messages", dict(model="flint", max_tokens=128,
        messages=[{"role": "user", "content": "Reply with OK."}], temperature=0))
    assert code == 200 and message["type"] == "message" and message["role"] == "assistant"
    assert isinstance(message["content"], list) and message["usage"]["input_tokens"] > 0
    print("Real Anthropic Messages shape OK; usage:", message["usage"], flush=True)
    code, response = engine("POST", "/v1/responses", dict(model="flint",
        input="Reply with OK.", max_output_tokens=128, temperature=0))
    assert code == 200 and response["object"] == "response" and isinstance(response["output"], list)
    assert response["usage"]["input_tokens"] > 0
    print("Real Responses API shape OK; usage:", response["usage"], flush=True)
    print("Loopback authentication/Host/Origin checks and real inference verified", flush=True)


if __name__ == "__main__":
    main()
