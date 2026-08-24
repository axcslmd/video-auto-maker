#!/usr/bin/env python3
"""Persistent onboarding for the three BasicRouter proxy base URLs."""
import json
import os
import stat
import sys
import tempfile
import urllib.parse


ENDPOINT_KINDS = ("text", "image", "video")
ENV_BY_KIND = {
    "text": "BASICROUTER_TEXT_BASE_URL",
    "image": "BASICROUTER_IMAGE_BASE_URL",
    "video": "BASICROUTER_VIDEO_BASE_URL",
}
LEGACY_ENV = "BASICROUTER_BASE_URL"
DEFAULT_BASE_URL = "https://api.basicrouter.ai/api"
CONFIG_PATH = os.environ.get(
    "BASICROUTER_ENDPOINTS_FILE",
    os.path.join(os.path.expanduser("~/.config"), "basicrouter", "endpoints.json"),
)


class EndpointSetupError(ValueError):
    pass


def _config_path():
    return os.environ.get("BASICROUTER_ENDPOINTS_FILE", CONFIG_PATH)


def normalize_base_url(value, label="API"):
    value = str(value or "").strip().rstrip("/")
    if not value:
        raise EndpointSetupError("%s 中转地址不能为空" % label)
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EndpointSetupError("%s 中转地址必须是完整的 http(s) URL" % label)
    if parsed.username or parsed.password:
        raise EndpointSetupError("%s 中转地址不能包含账号或密码" % label)
    if parsed.query or parsed.fragment:
        raise EndpointSetupError("%s 中转地址不能包含查询参数或片段" % label)
    return value


def validate_endpoints(values):
    if not isinstance(values, dict):
        raise EndpointSetupError("API_ENDPOINTS_INVALID")
    labels = {"text": "文本", "image": "图片", "video": "视频"}
    normalized = {
        kind: normalize_base_url(values.get(kind), labels[kind] + " API")
        for kind in ENDPOINT_KINDS
    }
    if len(set(normalized.values())) != len(ENDPOINT_KINDS):
        raise EndpointSetupError("文本、图片、视频 API 必须分别填写三个不同的中转地址")
    return normalized


def _read_file():
    path = _config_path()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise EndpointSetupError("API_ENDPOINTS_FILE_INVALID")
        raw = os.read(fd, 65537)
    finally:
        os.close(fd)
    try:
        payload = json.loads(raw.decode("utf-8"))
        return validate_endpoints(payload.get("endpoints", payload))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise EndpointSetupError("API_ENDPOINTS_FILE_INVALID") from exc


def explicit_endpoints():
    """Return user/admin supplied endpoints, or None when onboarding is due."""
    env_values = {kind: (os.environ.get(name) or "").strip()
                  for kind, name in ENV_BY_KIND.items()}
    if any(env_values.values()):
        if not all(env_values.values()):
            raise EndpointSetupError(
                "必须同时设置 BASICROUTER_TEXT_BASE_URL、"
                "BASICROUTER_IMAGE_BASE_URL 和 BASICROUTER_VIDEO_BASE_URL")
        return validate_endpoints(env_values)
    return _read_file()


def load_endpoints(*, allow_default=True):
    values = explicit_endpoints()
    if values:
        return values
    legacy = (os.environ.get(LEGACY_ENV) or "").strip()
    if legacy:
        # Runtime compatibility does not satisfy new-install onboarding: the
        # common entry still asks for the three explicit addresses.
        value = normalize_base_url(legacy, "旧版 API")
        return {kind: value for kind in ENDPOINT_KINDS}
    if allow_default:
        return {kind: DEFAULT_BASE_URL for kind in ENDPOINT_KINDS}
    return None


def base_url(kind):
    if kind not in ENDPOINT_KINDS:
        raise EndpointSetupError("未知 API 类型：%s" % kind)
    return load_endpoints()[kind]


def save_endpoints(values):
    values = validate_endpoints(values)
    path = _config_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory) or os.path.islink(path):
        raise EndpointSetupError("API_ENDPOINTS_SYMLINK_BLOCKED")
    payload = {"schema_version": 1, "endpoints": values}
    fd, temporary = tempfile.mkstemp(prefix=".endpoints-", dir=directory)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return path


def prompt_and_save():
    import endpoint_input
    try:
        save_endpoints(endpoint_input.read_endpoints())
    except endpoint_input.EndpointInputCancelled as exc:
        print("API_ENDPOINT_INPUT_CANCELLED: %s" % exc)
        return 1
    except endpoint_input.EndpointInputUnavailable as exc:
        print("API_ENDPOINT_INPUT_UNAVAILABLE: %s" % exc)
        return 1
    except EndpointSetupError as exc:
        print("API_ENDPOINTS_INVALID: %s" % exc)
        return 1
    print("API_ENDPOINTS_SAVED")
    return 0


def ensure_initialized(*, prompt_if_missing=False):
    try:
        if explicit_endpoints():
            return 0
    except EndpointSetupError as exc:
        print("API_ENDPOINTS_INVALID: %s" % exc)
        return 1
    if prompt_if_missing:
        return prompt_and_save()
    print("API_ENDPOINTS_REQUIRED: 请在初始化弹窗中填写文本、图片、视频三个 API 中转地址。")
    return 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else "check"
    if command == "check":
        if ensure_initialized() == 0:
            print("API_ENDPOINTS_READY")
            return 0
        return 1
    if command in {"init", "prompt"}:
        return ensure_initialized(prompt_if_missing=True)
    if command == "show":
        values = load_endpoints(allow_default=False)
        if not values:
            print("API_ENDPOINTS_MISSING")
            return 1
        print(json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print("usage: api_endpoint_setup.py {check|init|prompt|show}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
