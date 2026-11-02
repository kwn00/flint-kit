"""Opt-in, reversible edits to Pi/OpenCode's user-level model registries."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid

from .client import private_write, provider_config

PROVIDER = "flint-local"
GROUPS = {"pi": "providers", "opencode": "provider"}
MAX_CONFIG_BYTES = 2 * 1024 * 1024
TOKEN = re.compile(r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"(?:[^"\\]|\\.)*"|'
                   r'[{}\[\]:,]|true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')


class ConfigError(ValueError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("중복 JSON 키가 있습니다. 기존 설정을 정리한 뒤 다시 연결하세요.")
        result[key] = value
    return result


class JsonConfig:
    """Keep source spans so editing one provider preserves unrelated JSONC text."""

    def __init__(self, text, comments=False):
        self.text, self.comments = text, comments
        self.tokens = []
        offset = 0
        while offset < len(text):
            match = TOKEN.match(text, offset)
            if match is None:
                raise ConfigError("올바른 JSON/JSONC 설정이 아닙니다. 원본은 변경하지 않았습니다.")
            token = match.group()
            if token.startswith(("//", "/*")):
                if not comments:
                    raise ConfigError("Pi models.json에는 주석을 사용할 수 없습니다.")
            elif not token.isspace():
                self.tokens.append(match)
            offset = match.end()
        clean = []
        for i, token in enumerate(self.tokens):
            trailing = (token.group() == "," and i + 1 < len(self.tokens)
                        and self.tokens[i + 1].group() in ("}", "]"))
            if not (comments and trailing):
                clean.append(token.group())
        try:
            self.value = json.loads(" ".join(clean), object_pairs_hook=unique_object)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ConfigError("설정 JSON 구문이 잘못되었습니다. 원본은 변경하지 않았습니다.") from error
        if not isinstance(self.value, dict):
            raise ConfigError("클라이언트 설정의 최상위 값은 JSON 객체여야 합니다.")
        stack = []
        self.ends = {}
        for i, token in enumerate(self.tokens):
            if token.group() in ("{", "["):
                stack.append(i)
            elif token.group() in ("}", "]"):
                self.ends[stack.pop()] = i

    def members(self, container):
        result = []
        index = container + 1
        while self.tokens[index].group() != "}":
            value = index + 2
            end = self.ends.get(value, value)
            comma = end + 1 if self.tokens[end + 1].group() == "," else None
            result.append((json.loads(self.tokens[index].group()), index, value, end, comma))
            index = end + 2 if comma is not None else end + 1
        return result

    def add(self, container, key, value):
        members = self.members(container)
        close = self.tokens[self.ends[container]].start()
        line_start = self.text.rfind("\n", 0, close) + 1
        indent = re.match(r"[ \t]*", self.text[line_start:close]).group() + "  "
        text = json.dumps(key) + ": " + json.dumps(value, ensure_ascii=False, indent=2)
        addition = "\n" + indent + text.replace("\n", "\n" + indent) + "\n"
        result = self.text[:close] + addition + self.text[close:]
        if members and members[-1][4] is None:
            end = self.tokens[members[-1][3]].end()
            result = result[:end] + "," + result[end:]
        return result

    def edit_provider(self, group, provider):
        root = next((m for m in self.members(0) if m[0] == group), None)
        if root is None:
            if provider is None:
                raise ConfigError("제거할 Flint provider가 없습니다.")
            result = self.add(0, group, {PROVIDER: provider})
        else:
            if not isinstance(self.value[group], dict):
                raise ConfigError(f"{group} 설정이 객체가 아닙니다.")
            members = self.members(root[2])
            member = next((m for m in members if m[0] == PROVIDER), None)
            if member is None:
                if provider is None:
                    raise ConfigError("제거할 Flint provider가 없습니다.")
                result = self.add(root[2], PROVIDER, provider)
            elif provider is not None:
                result = (self.text[:self.tokens[member[2]].start()]
                          + json.dumps(provider, ensure_ascii=False, indent=2)
                          + self.text[self.tokens[member[3]].end():])
            else:
                start, end = self.tokens[member[1]].start(), self.tokens[member[3]].end()
                comma = member[4]
                if comma is not None:
                    token = self.tokens[comma]
                    result = self.text[:token.start()] + self.text[token.end():]
                    result = result[:start] + result[end:]
                else:
                    result = self.text[:start] + self.text[end:]
                    index = members.index(member)
                    if index:
                        token = self.tokens[members[index - 1][4]]
                        result = result[:token.start()] + result[token.end():]
        JsonConfig(result, self.comments)
        return result


@dataclass(frozen=True)
class Snapshot:
    content: bytes | None
    mode: int = 0o600
    identity: tuple | None = None

    @property
    def text(self):
        return self.content.decode("utf-8") if self.content is not None else "{}"


def guard_path(path):
    if not path.is_absolute():
        raise ConfigError("설정 디렉터리는 절대 경로여야 합니다.")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ConfigError(f"심볼릭 링크 설정 경로는 자동 수정하지 않습니다: {part}")


def read_snapshot(path):
    guard_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return Snapshot(None)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
            raise ConfigError("일반 설정 파일(최대 2 MiB)만 지원합니다.")
        content = source.read(MAX_CONFIG_BYTES + 1)
        if len(content) > MAX_CONFIG_BYTES:
            raise ConfigError("설정 파일이 2 MiB를 초과합니다.")
        return Snapshot(content, stat.S_IMODE(info.st_mode),
                        (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size))


def write_checked(path, before, content):
    guard_path(path)
    if content is None:
        if read_snapshot(path) != before:
            raise ConfigError("설정 파일이 작업 중 변경되었습니다. 다시 확인하세요.")
        if before.content is not None:
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".flint-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), before.mode)
        if read_snapshot(path) != before:
            raise ConfigError("설정 파일이 작업 중 변경되었습니다. 다시 확인하세요.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint(content):
    return hashlib.sha256(content).hexdigest() if content is not None else None


def config_path(name):
    if name == "pi":
        directory = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home().resolve() / ".pi/agent").expanduser()
        return directory / "models.json"
    if name != "opencode":
        raise ConfigError("Pi와 opencode만 기존 CLI 연결을 지원합니다.")
    directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home().resolve() / ".config").expanduser() / "opencode"
    paths = [directory / file for file in ("opencode.json", "opencode.jsonc")]
    existing = [p for p in paths if os.path.lexists(p)]
    if len(existing) > 1 or (directory / "config.json").exists():
        raise ConfigError("opencode 전역 설정 파일이 여러 개이거나 구형 config.json이 있습니다. "
                          "opencode.json 또는 opencode.jsonc 하나로 정리한 뒤 연결하세요.")
    return existing[0] if existing else paths[0]


def check_visibility(name, config):
    if name != "opencode":
        return
    enabled, disabled = config.get("enabled_providers"), config.get("disabled_providers", [])
    if ((enabled is not None and (not isinstance(enabled, list) or PROVIDER not in enabled))
            or not isinstance(disabled, list) or PROVIDER in disabled):
        raise ConfigError("opencode의 enabled_providers/disabled_providers가 flint-local을 차단합니다. "
                          "기존 제한은 변경하지 않습니다. 해당 목록에서 Flint를 허용한 뒤 연결하세요.")


class Connections:
    def __init__(self, home, settings):
        self.home, self.settings = home.resolve(), settings
        self.manifest_path = self.home / "connections.json"

    def load(self):
        snapshot = read_snapshot(self.manifest_path)
        manifest = JsonConfig(snapshot.text).value
        for name, entry in manifest.items():
            if (name not in GROUPS or not isinstance(entry, dict)
                    or not isinstance(entry.get("provider"), dict)
                    or any(not isinstance(entry.get(key), str)
                           for key in ("path", "installed", "backup", "backup_hash"))):
                raise ConfigError("연결 기록 형식이 잘못되었습니다. connections.json과 백업을 확인하세요.")
        return snapshot, manifest

    def inspect(self, name, manifest):
        path = config_path(name)
        entry = manifest.get(name)
        if entry and entry["path"] != str(path):
            raise ConfigError("연결 후 설정 경로가 바뀌었습니다. 원래 디렉터리 설정으로 돌아가 연결을 해제하세요.")
        snapshot = read_snapshot(path)
        document = JsonConfig(snapshot.text, comments=name == "opencode")
        group = document.value.get(GROUPS[name], {})
        if not isinstance(group, dict):
            raise ConfigError(f"{GROUPS[name]} 설정이 객체가 아닙니다.")
        present = PROVIDER in group
        if entry:
            if not present or group[PROVIDER] != entry["provider"]:
                raise ConfigError("Flint 항목이 외부에서 수정되었거나 제거되었습니다. 덮어쓰지 않습니다. "
                                  "백업과 현재 설정을 확인하세요.")
        elif present:
            raise ConfigError("이미 flint-local provider가 있습니다. 이 앱이 만든 항목이 아니므로 덮어쓰지 않습니다.")
        return path, snapshot, document, entry

    def status(self, name):
        result = dict(enabled=False, path="", can_connect=False, can_remove=False,
                      needs_update=False, error="", backup="")
        try:
            _, manifest = self.load()
            entry = manifest.get(name)
            result.update(enabled=entry is not None, backup=entry["backup"] if entry else "")
            result["path"] = str(config_path(name))
            _, _, document, entry = self.inspect(name, manifest)
            result["can_remove"] = entry is not None
            check_visibility(name, document.value)
            result.update(can_connect=True, needs_update=bool(
                entry and entry["provider"] != provider_config(name, self.home, self.settings)))
        except (ConfigError, OSError, UnicodeError) as error:
            result["error"] = str(error)
        return result

    def change(self, name, enable):
        if name not in GROUPS or type(enable) is not bool:
            raise ConfigError("Pi/opencode 연결과 boolean enable만 지원합니다.")
        manifest_before, manifest = self.load()
        path, before, document, entry = self.inspect(name, manifest)
        if not enable and entry is None:
            raise ConfigError("해제할 기존 CLI 연결이 없습니다.")
        if entry and fingerprint(before.content) == entry["installed"]:
            backup = read_snapshot(Path(entry["backup"]))
            if fingerprint(backup.content) != entry["backup_hash"]:
                raise ConfigError("백업이 변경되었습니다. 복원을 중단합니다.")
            restore = json.loads(backup.text)["restore"]
        else:
            restore = (document.edit_provider(GROUPS[name], None) if entry
                       else before.content.decode("utf-8") if before.content is not None else None)
        if enable:
            check_visibility(name, document.value)
            provider = provider_config(name, self.home, self.settings)
            after = document.edit_provider(GROUPS[name], provider).encode("utf-8")
        else:
            after = restore.encode("utf-8") if restore is not None else None
        backup_path = self.home / "connection-backups" / (name + "-" + uuid.uuid4().hex + ".json")
        backup_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_write(backup_path, json.dumps(dict(path=str(path), before=before.text if before.content is not None else None,
                                                  restore=restore), ensure_ascii=False, indent=2))
        if enable:
            manifest[name] = dict(path=str(path), provider=provider, installed=fingerprint(after),
                                  backup=str(backup_path), backup_hash=fingerprint(backup_path.read_bytes()))
        else:
            del manifest[name]
        write_checked(path, before, after)
        try:
            write_checked(self.manifest_path, manifest_before, json.dumps(manifest, indent=2).encode("utf-8"))
        except (ConfigError, OSError) as error:
            try:
                current = read_snapshot(path)
                if current.content != after:
                    raise ConfigError("다른 프로그램이 설정을 변경하여 자동 복원을 거부했습니다.")
                write_checked(path, current, before.content)
            except (ConfigError, OSError) as restore_error:
                raise ConfigError(f"연결 기록 저장 실패; 설정 복원도 실패: {restore_error}. 백업: {backup_path}") from error
            raise ConfigError(f"연결 기록 저장 실패; 변경 전 설정으로 복원했습니다. 백업: {backup_path}") from error
        return path
