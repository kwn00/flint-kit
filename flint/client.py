"""Isolated CLI launchers and shared Flint provider definitions."""
import fcntl
import json
import hashlib
import os
from pathlib import Path
import shutil
import shlex
import sys
import tempfile


def private_write(path, text):
    fd, temporary = tempfile.mkstemp(prefix=".flint-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def claude_onboarding(config_dir):
    destination = config_dir / ".claude.json"
    marker = config_dir.parent / "onboarding-import.json"
    lock = config_dir.parent / "onboarding-import.lock"
    for path in (config_dir.parent.parent, config_dir.parent, config_dir, destination, marker, lock):
        if path.is_symlink():
            raise SystemExit("Claude 전용 설정이 심볼릭 링크입니다. 기존 설정을 변경하지 않았습니다.")
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def read(path):
        if path.is_symlink():
            raise ValueError("설정 파일이 심볼릭 링크입니다.")
        if not path.exists():
            return None, {}
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Claude 설정 파일이 16 MiB를 초과합니다.")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Claude 설정에 중복 키가 있습니다.")
                result[key] = value
            return result
        raw = path.read_bytes()
        value = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(value, dict):
            raise ValueError("Claude 설정은 JSON 객체여야 합니다.")
        return raw, value

    try:
        with os.fdopen(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            if marker.exists():
                _, imported = read(marker)
                if imported.get("version") != 1:
                    raise ValueError("Claude 온보딩 가져오기 기록이 올바르지 않습니다.")
                return
            source_dir = os.environ.get("CLAUDE_CONFIG_DIR")
            source = (Path(source_dir).expanduser() / ".claude.json" if source_dir else
                      Path.home() / ".claude.json")
            if source.resolve() == destination.resolve():
                raise ValueError("기존 Claude 설정과 전용 설정 경로가 같습니다.")
            source_raw, original = read(source)
            if source_raw is None:
                print("Flint Kit: 기존 Claude 설정이 없어 CLI의 초기 설정을 그대로 진행합니다.", file=sys.stderr)
                return
            before, current = read(destination)
            fields = {"hasCompletedOnboarding": bool, "lastOnboardingVersion": str,
                      "shiftEnterKeyBindingInstalled": bool, "optionAsMetaKeyInstalled": bool}
            for key, kind in fields.items():
                if key in original:
                    if type(original[key]) is not kind:
                        raise ValueError("Claude 온보딩 필드 형식이 올바르지 않습니다.")
                    current.setdefault(key, original[key])
            projects = original.get("projects", {})
            if not isinstance(projects, dict) or not isinstance(current.get("projects", {}), dict):
                raise ValueError("Claude 프로젝트 설정 형식이 올바르지 않습니다.")
            trusted = 0
            for path, value in projects.items():
                if (not os.path.isabs(path) or not isinstance(value, dict)
                        or value.get("hasTrustDialogAccepted") is not True):
                    continue
                entry = current.setdefault("projects", {}).setdefault(path, {})
                if not isinstance(entry, dict):
                    raise ValueError("Claude 전용 프로젝트 설정 형식이 올바르지 않습니다.")
                if "hasTrustDialogAccepted" not in entry:
                    entry["hasTrustDialogAccepted"] = True
                    trusted += 1
            if read(destination)[0] != before:
                raise ValueError("가져오기 중 Claude 전용 설정이 변경되었습니다. 다시 실행하세요.")
            private_write(destination, json.dumps(current, indent=2) + "\n")
            private_write(marker, json.dumps({"version": 1}) + "\n")
            print(f"Flint Kit: 기존 온보딩 상태와 신뢰 폴더 {trusted}개를 가져왔습니다. "
                  "로그인·키·MCP·도구 권한은 복사하지 않았습니다.", file=sys.stderr)
    except (OSError, ValueError) as error:
        raise SystemExit("Claude 온보딩 가져오기 실패: " + str(error)) from None


def provider_config(name, home, settings):
    base = f"http://127.0.0.1:{settings['port']}/v1"
    context, output = settings.get("context", 65536), settings.get("output_limit", 8192)
    key_file = str(home / "rec/config/api-key")
    if name == "pi":
        return {"baseUrl": base, "api": "openai-completions",
                "apiKey": "!/bin/cat " + shlex.quote(key_file),
                "models": [{"id": "flint", "name": "Flint Kit",
                            "reasoning": True, "input": ["text"],
                            "contextWindow": context, "maxTokens": output,
                            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}]}
    if name == "opencode":
        return {"npm": "@ai-sdk/openai-compatible", "name": "Flint Kit",
                "options": {"baseURL": base, "apiKey": "{file:" + key_file + "}"},
                "models": {"flint": {"name": "Flint Kit",
                                     "limit": {"context": context, "output": output}}}}
    raise ValueError("Unknown provider")


def main():
    name, directory, *extra = sys.argv[1:]
    if name not in ("claude", "codex", "pi", "opencode"):
        raise SystemExit("Unknown client")
    home = Path(directory)
    executable = shutil.which(name)
    if not executable:
        raise SystemExit(f"{name} CLI가 PATH에 없습니다. 해당 프로젝트의 공식 설치 안내로 직접 설치하세요.")
    settings_path = home / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {"port": 11540}
    base = f"http://127.0.0.1:{settings['port']}"
    key = (home / "rec/config/api-key").read_text().strip()
    # Check authentication and readiness without auto-starting another model runtime.
    import urllib.request
    try:
        request = urllib.request.Request(base + "/health", headers={"Authorization": "Bearer " + key})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            if response.status != 200:
                raise OSError("Unhealthy engine")
    except OSError:
        raise SystemExit("먼저 Flint Kit 대시보드에서 서버를 시작하세요.") from None
    env = os.environ.copy()
    # Explicit local routing must win over inherited cloud-provider selection.
    for variable in tuple(env):
        if variable.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_", "AZURE_", "OPENAI_")):
            env.pop(variable)
    args = []
    context = settings.get("context", 65536)
    output = settings.get("output_limit", 8192)
    if name == "claude":
        config_dir = home / "clients/claude/config"
        claude_onboarding(config_dir)
        env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_AUTH_TOKEN=key,
                   ANTHROPIC_API_KEY="", ANTHROPIC_MODEL="flint",
                   ANTHROPIC_DEFAULT_OPUS_MODEL="flint", ANTHROPIC_DEFAULT_SONNET_MODEL="flint",
                   ANTHROPIC_DEFAULT_HAIKU_MODEL="flint", ANTHROPIC_DEFAULT_FABLE_MODEL="flint",
                   ANTHROPIC_CUSTOM_MODEL_OPTION="flint",
                   ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="Flint Kit",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CODE_ATTRIBUTION_HEADER="0",
                   CLAUDE_CODE_MAX_CONTEXT_TOKENS=str(context), CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(output),
                   CLAUDE_CONFIG_DIR=str(config_dir))
    elif name == "codex":
        codex_home = home / "clients/codex/config"
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        env["CODEX_HOME"] = str(codex_home)
        env["FLINT_LOCAL_KEY"] = key
        compact = max(context // 2, min(context * 9 // 10, context - output))
        args = ["-c", 'model="flint"', "-c", 'model_provider="flint_local"',
                "-c", f"model_context_window={context}", "-c", f"model_auto_compact_token_limit={compact}",
                "-c", 'web_search="disabled"',
                "-c", 'model_providers.flint_local.name="Flint Kit"',
                "-c", f'model_providers.flint_local.base_url="{base}/v1"',
                "-c", 'model_providers.flint_local.wire_api="responses"',
                "-c", 'model_providers.flint_local.env_key="FLINT_LOCAL_KEY"',
                "-c", "model_providers.flint_local.stream_idle_timeout_ms=1800000"]
    elif name == "opencode":
        isolated = home / "clients/opencode"
        env.pop("OPENCODE_CONFIG", None)
        env.update(OPENCODE_CONFIG_DIR=str(isolated / "config"),
                   XDG_CONFIG_HOME=str(isolated / "xdg-config"),
                   XDG_DATA_HOME=str(isolated / "data"),
                   XDG_STATE_HOME=str(isolated / "state"))
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
            "$schema": "https://opencode.ai/config.json", "model": "flint-local/flint",
            "provider": {"flint-local": provider_config(name, home, settings)}})
    else:
        agent = home / "clients/pi/agent"
        agent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = agent / "models.json"
        ownership = agent / "models.generated.sha256"
        # Dedicated Pi home: never overwrite the real ~/.pi configuration.
        config = {"providers": {"flint-local": provider_config(name, home, settings)}}
        if path.is_symlink() or ownership.is_symlink():
            raise SystemExit("Pi 모델 설정이 심볼릭 링크입니다. 덮어쓰지 않습니다.")
        expected = json.dumps(config, indent=2)
        if path.exists() and path.read_text() != expected:
            current_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if not ownership.is_file() or ownership.read_text().strip() != current_hash:
                raise SystemExit("Pi 전용 모델 설정이 이후 수정되었습니다. 기존 파일을 보존합니다.")
        if not path.exists() or path.read_text() != expected:
            private_write(path, expected)
        private_write(ownership, hashlib.sha256(expected.encode()).hexdigest() + "\n")
        env["PI_CODING_AGENT_DIR"] = str(agent)
        args = ["--provider", "flint-local", "--model", "flint"]
    os.execvpe(executable, [executable, *args, *extra], env)


if __name__ == "__main__":
    main()
