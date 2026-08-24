#!/usr/bin/env python3
"""Agent-agnostic, session-scoped BasicRouter key onboarding.

The host agent must provide a stable ``BASICROUTER_SESSION_ID`` for the whole
conversation.  The key is shared by subprocesses through a 600-permission
cache file tied to that ID, but is never reused by a new session by default.

CLI:
  python3 key_setup.py init                  -> print a new session ID
  python3 key_setup.py check                 -> prints VALIDATED / STORED_UNVERIFIED / MISSING / QUARANTINED
  python3 key_setup.py gate                  -> STORED + VALIDATED(exit0) or a client-facing
                                                 reminder + BLOCKED(exit1). Every
                                                 guided skill calls this FIRST; on
                                                 BLOCKED the skill must stop and not
                                                 start any 引导. Key is entered once
                                                 per session.
  python3 key_setup.py save --stdin          -> read key from secure host stdin, validate, and save
  python3 key_setup.py save --secure-prompt  -> open an OS password prompt, validate, and save
  python3 key_setup.py quarantine            -> globally deny the current credential fingerprint
  python3 key_setup.py quarantine-status     -> print quarantine state without key material
  python3 key_setup.py clear                 -> remove this session's key
  python3 key_setup.py get                   -> prints key or MISSING
"""
import os
import sys
import stat
import json
import hashlib
import secrets
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import br_client          # noqa: E402 — hoisted from inline import (v3 cleanup)

import agent_runtime
from project_utils import FileLock

SESSION_ENV = "BASICROUTER_SESSION_ID"
API_KEY_ENV = "BASICROUTER_API_KEY"
SESSION_DIR = os.path.join(os.path.expanduser("~/.cache"), "basicrouter", "sessions")
# Cross-process handoff is opt-in. A machine-global default would allow a new
# conversation to accidentally reuse the previous conversation's session ID.
SESSION_STATE_FILE = os.environ.get("BASICROUTER_SESSION_STATE_FILE")
VALIDATION_MAX_AGE_SECONDS = 15 * 60


class SessionKeyError(ValueError):
    """Base class for a safe credential-readiness block."""

    def __init__(self, error_code, client_message):
        super().__init__(error_code)
        self.error_code = error_code
        self.client_message = client_message


class SessionKeyRequired(SessionKeyError):
    """The current public session has no BasicRouter authorization."""

    def __init__(self, error_code="BASICROUTER_SESSION_KEY_REQUIRED"):
        super().__init__(error_code, blocked_message())


class SessionKeyUnverified(SessionKeyError):
    """A stored key could not be authenticated because the service is unavailable."""

    def __init__(self, error_code="BASICROUTER_SESSION_KEY_UNVERIFIED"):
        super().__init__(error_code, unverified_message())


class SessionKeyQuarantined(SessionKeyError):
    """The current credential fingerprint is denied across all sessions."""

    def __init__(self, error_code="BASICROUTER_SESSION_KEY_QUARANTINED"):
        super().__init__(error_code, quarantined_message())


def blocked_message():
    """Return the client-facing gate message without exposing session/key data."""
    return (
        "BLOCKED\n"
        "———\n"
        "当前任务会话还没有可用的 BasicRouter 密钥。\n"
        "请通过宿主提供的安全凭证输入录入密钥，由 Agent 使用不回显的标准输入执行 "
        "key_setup.py save --stdin；若宿主没有安全输入框，Agent 可执行 "
        "key_setup.py save --secure-prompt，在本机密码弹窗中完成录入。\n"
        "请勿把密钥粘贴到普通聊天消息中。\n"
        "为保护你的密钥，系统不会从日志、项目文件或其他任务会话复制授权。\n"
        "还没有密钥？可到 BasicRouter 账户获取。\n"
        "（密钥就绪前不会查询实时模型目录、改变生成状态或发起付费请求。）"
    )


def invalid_key_message():
    return (
        "BLOCKED_INVALID_KEY\n"
        "———\n"
        "当前任务会话保存的 BasicRouter 密钥已失效或无权限，旧授权已安全撤销。\n"
        "请通过宿主安全凭证输入提交新的 sk- 密钥，再以不回显的标准输入在线校验保存；"
        "请勿把密钥粘贴到普通聊天消息中。\n"
        "在重新验证前，不会改变生成状态或发起付费请求。"
    )


def unverified_message():
    return (
        "STORED_UNVERIFIED\n"
        "———\n"
        "密钥已保存在当前任务会话，但服务器暂时无法完成认证校验。\n"
        "我会保留本会话密钥，网络恢复后可直接重试；本次不会改变生成状态或发起付费请求。"
    )


def quarantined_message():
    return (
        "QUARANTINED\n"
        "———\n"
        "当前凭证已因暴露风险被安全隔离，不能用于任何外部调用。\n"
        "请先在 BasicRouter 侧撤销或轮换该凭证，再通过宿主安全凭证输入提交"
        "与已隔离指纹不同的新密钥，并由 Agent 使用不回显的标准输入执行 "
        "key_setup.py save --stdin，或执行 key_setup.py save --secure-prompt 使用本机密码弹窗。\n"
        "请勿把新密钥粘贴到普通聊天消息中；只有不同的新密钥在线验证通过后"
        "才会解除当前会话隔离。"
    )


def ensure_session_id(host_session_id=None):
    """Ensure this process has a public session ID for child processes.

    Host adapters should set BASICROUTER_SESSION_ID explicitly. A stable opaque
    host task/session signal may be hashed into the public ID. If neither is
    available, generate a fresh ID instead of deriving one from the project
    directory: different conversations must never share key authorization.
    """
    current = session_id()
    if current:
        return current
    # Agent command adapters may launch each command in a fresh shell. Keep
    # only the opaque public ID here, never the API key, so those subprocesses
    # can select the same session cache without a hard-coded command prefix.
    if SESSION_STATE_FILE:
        try:
            with open(SESSION_STATE_FILE, encoding="utf-8") as handle:
                current = session_id(handle.read())
                if current:
                    os.environ[SESSION_ENV] = current
                    return current
        except OSError:
            pass
    host = host_session_id or os.environ.get("BASICROUTER_HOST_SESSION_ID")
    if not host:
        runtime = agent_runtime.detect_agent_runtime()
        for variable in ("KILO_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
                         "HERMES_SESSION_ID", "WORKBUDDY_SESSION_ID",
                         "WORKBUDDY_TASK_ID"):
            if os.environ.get(variable):
                host = os.environ[variable]
                break
    if host:
        digest = hashlib.sha256(str(host).encode("utf-8")).hexdigest()[:32]
        value = "br-session-%s" % digest
    else:
        value = "br-session-" + secrets.token_urlsafe(24)
    os.environ[SESSION_ENV] = value
    if SESSION_STATE_FILE:
        directory = os.path.dirname(SESSION_STATE_FILE) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".session-", dir=directory)
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, SESSION_STATE_FILE)
            os.chmod(SESSION_STATE_FILE, stat.S_IRUSR | stat.S_IWUSR)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    return value


def session_id(explicit=None):
    value = explicit or os.environ.get(SESSION_ENV)
    return value.strip() if value and value.strip() else None


def session_key_path(explicit_session_id=None):
    sid = session_id(explicit_session_id)
    if not sid:
        return None
    # Hash the opaque host identifier so it cannot create path components.
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()
    return os.path.join(SESSION_DIR, digest, "key")


def session_validation_path(explicit_session_id=None):
    key_path = session_key_path(explicit_session_id)
    return os.path.join(os.path.dirname(key_path), "validation.json") if key_path else None


def _key_fingerprint(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_fingerprint(key):
    return _key_fingerprint(str(key)) if key else None


def session_quarantine_path(explicit_session_id=None):
    key_path = session_key_path(explicit_session_id)
    return (os.path.join(os.path.dirname(key_path), "quarantine.json")
            if key_path else None)


def global_quarantine_path():
    return os.path.join(SESSION_DIR, "_credential-quarantine.json")


def _read_private_json(path, *, max_bytes, invalid_code, symlink_code):
    if not path or not os.path.lexists(path):
        return None
    if os.path.islink(path):
        raise ValueError(symlink_code)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if os.path.islink(path):
            raise ValueError(symlink_code) from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            raise ValueError(invalid_code)
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(invalid_code)
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(invalid_code) from exc
    finally:
        os.close(fd)
    if not isinstance(record, dict):
        raise ValueError(invalid_code)
    return record


def quarantine_status(explicit_session_id=None):
    path = session_quarantine_path(explicit_session_id)
    record = _read_private_json(
        path, max_bytes=16384,
        invalid_code="SESSION_QUARANTINE_FILE_INVALID",
        symlink_code="SESSION_QUARANTINE_SYMLINK_BLOCKED")
    if record is None:
        return None
    fingerprint = str(record.get("key_sha256") or "")
    if (record.get("kind") != "basicrouter_session_credential_quarantine" or
            record.get("active") is not True or len(fingerprint) != 64 or
            any(char not in "0123456789abcdef" for char in fingerprint)):
        raise ValueError("SESSION_QUARANTINE_FILE_INVALID")
    record["path"] = path
    return record


def _global_quarantine_index():
    path = global_quarantine_path()
    record = _read_private_json(
        path, max_bytes=1048576,
        invalid_code="GLOBAL_CREDENTIAL_QUARANTINE_FILE_INVALID",
        symlink_code="GLOBAL_CREDENTIAL_QUARANTINE_SYMLINK_BLOCKED")
    if record is None:
        return None
    entries = record.get("fingerprints")
    if (record.get("schema_version") != 1 or
            record.get("kind") != "basicrouter_global_credential_quarantine" or
            not isinstance(entries, dict)):
        raise ValueError("GLOBAL_CREDENTIAL_QUARANTINE_FILE_INVALID")
    for fingerprint, entry in entries.items():
        value = str(fingerprint)
        if (len(value) != 64 or
                any(char not in "0123456789abcdef" for char in value) or
                not isinstance(entry, dict) or entry.get("active") is not True):
            raise ValueError("GLOBAL_CREDENTIAL_QUARANTINE_FILE_INVALID")
    record["path"] = path
    return record


def global_quarantine_status(key=None, *, key_sha256=None):
    fingerprint = key_sha256 or key_fingerprint(key)
    if not fingerprint:
        return None
    record = _global_quarantine_index()
    if not record:
        return None
    entry = record["fingerprints"].get(fingerprint)
    return dict(entry) if entry else None


def _atomic_private_json(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory) or os.path.islink(path):
        raise ValueError("CREDENTIAL_QUARANTINE_SYMLINK_BLOCKED")
    os.chmod(directory, stat.S_IRWXU)
    fd, temporary = tempfile.mkstemp(prefix=".quarantine-", dir=directory)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return path


def _record_global_quarantine(key_sha256, *, actor, reason, quarantined_at):
    path = global_quarantine_path()
    with FileLock(path + ".lock"):
        current = _global_quarantine_index() or {
            "schema_version": 1,
            "kind": "basicrouter_global_credential_quarantine",
            "fingerprints": {},
        }
        current.pop("path", None)
        entries = current["fingerprints"]
        if key_sha256 not in entries:
            entries[key_sha256] = {
                "active": True, "actor": str(actor), "reason": str(reason),
                "first_quarantined_at": quarantined_at,
            }
        _atomic_private_json(path, current)
    return dict(entries[key_sha256])


def _read_validation_marker(explicit_session_id=None):
    path = session_validation_path(explicit_session_id)
    if not path:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if os.path.islink(path):
            raise ValueError("SESSION_VALIDATION_SYMLINK_BLOCKED") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError("SESSION_VALIDATION_FILE_INVALID")
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise ValueError("SESSION_VALIDATION_FILE_INVALID")
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("SESSION_VALIDATION_FILE_INVALID") from exc
    finally:
        os.close(fd)


def validation_status(key, explicit_session_id=None, *, now=None,
                      max_age_seconds=VALIDATION_MAX_AGE_SECONDS):
    """Return VALIDATED or STORED_UNVERIFIED without making a network call."""
    marker = _read_validation_marker(explicit_session_id)
    if not marker or marker.get("key_sha256") != _key_fingerprint(key):
        return "STORED_UNVERIFIED"
    try:
        age = float(time.time() if now is None else now) - float(marker["validated_at"])
    except (KeyError, TypeError, ValueError):
        return "STORED_UNVERIFIED"
    if age < 0 or age > max(0, int(max_age_seconds)):
        return "STORED_UNVERIFIED"
    return "VALIDATED"


def mark_key_validated(key, explicit_session_id=None, *, validated_at=None):
    """Atomically bind a short-lived auth proof to the exact cached key hash."""
    path = session_validation_path(explicit_session_id)
    key_path = session_key_path(explicit_session_id)
    if not path or not key_path:
        raise ValueError("BASICROUTER_SESSION_ID is required")
    marker = {
        "schema_version": 1,
        "status": "validated",
        "key_sha256": _key_fingerprint(key),
        "validated_at": float(time.time() if validated_at is None else validated_at),
        "validation_method": "authenticated_low_cost_ping",
    }
    with FileLock(key_path + ".lock"):
        current = load_key(explicit_session_id)
        if current != key:
            raise ValueError("SESSION_KEY_CHANGED_DURING_VALIDATION")
        directory = os.path.dirname(path)
        fd, temporary = tempfile.mkstemp(prefix=".validation-", dir=directory)
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(marker, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    return marker


def _read_cached_key(explicit_session_id=None):
    """Read current session cache bytes without applying quarantine policy."""
    candidate = session_id(explicit_session_id)
    if not candidate:
        return None
    path = session_key_path(candidate)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if os.path.islink(path):
            raise ValueError("SESSION_KEY_SYMLINK_BLOCKED") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("SESSION_KEY_NOT_REGULAR_FILE")
        if info.st_size > 16384:
            raise ValueError("SESSION_KEY_FILE_TOO_LARGE")
        data = os.read(fd, 16385)
        if len(data) > 16384:
            raise ValueError("SESSION_KEY_FILE_TOO_LARGE")
        try:
            return data.decode("utf-8").strip() or None
        except UnicodeDecodeError as exc:
            raise ValueError("SESSION_KEY_INVALID_ENCODING") from exc
    finally:
        os.close(fd)


def load_key(explicit_session_id=None, *, include_quarantined=False):
    """Return only a session/global key not denied by credential fingerprint.

    ``include_quarantined`` may bypass only the current session marker for
    internal migration.  The machine-wide fingerprint deny-list is absolute,
    including opt-in environment credentials and other session caches.
    """
    if not include_quarantined and quarantine_status(explicit_session_id):
        return None
    cached = _read_cached_key(explicit_session_id)
    if cached:
        return None if global_quarantine_status(cached) else cached
    if os.environ.get("BR_ALLOW_GLOBAL_KEY") == "1":
        env = (os.environ.get(API_KEY_ENV) or "").strip() or None
        return None if env and global_quarantine_status(env) else env
    return None


def is_auth_rejection(value):
    """Classify only explicit authentication failures, never transient errors."""
    return br_client.is_authentication_error(value)


def reject_key(key, explicit_session_id=None):
    """Clear authorization only if the rejected key is still the cached key."""
    key_path = session_key_path(explicit_session_id)
    validation_path = session_validation_path(explicit_session_id)
    if not key_path:
        return False
    removed = False
    with FileLock(key_path + ".lock"):
        if _read_cached_key(explicit_session_id) != key:
            return False
        for path in (key_path, validation_path):
            if path and os.path.isfile(path) and not os.path.islink(path):
                os.remove(path)
                removed = True
        try:
            os.rmdir(os.path.dirname(key_path))
        except OSError:
            pass
    return removed


# All BasicRouter HTTP paths can now invalidate a stale readiness marker.  The
# callback compares exact key bytes before deleting, so a late 401 from an old
# request cannot erase a newly saved credential.
br_client.set_auth_rejection_handler(reject_key)


def require_key(error_code="BASICROUTER_SESSION_KEY_REQUIRED",
                explicit_session_id=None):
    """Resolve the public session and return only its exact scoped key.

    Paid/live callers use this before mutating workflow state.  Resolving the
    session here is important for fresh CLI subprocesses that receive the
    public session through ``BASICROUTER_SESSION_STATE_FILE``.
    """
    public_session_id = explicit_session_id or ensure_session_id()
    if credential_quarantine_status(public_session_id):
        raise SessionKeyQuarantined(
            error_code.replace("_REQUIRED", "_QUARANTINED"))
    key = load_key(public_session_id)
    if not key:
        raise SessionKeyRequired(error_code)
    if validation_status(key, public_session_id) == "VALIDATED":
        return key
    ok, message = _validate(key)
    if ok:
        mark_key_validated(key, public_session_id)
        return key
    if is_auth_rejection(message):
        reject_key(key, public_session_id)
        error = SessionKeyRequired(error_code)
        error.client_message = invalid_key_message()
        raise error
    raise SessionKeyUnverified(error_code.replace("_REQUIRED", "_UNVERIFIED"))


def save_key(key, explicit_session_id=None):
    """Persist a key for the current session with 600 perms."""
    key = key.strip()
    path = session_key_path(explicit_session_id)
    if not path:
        raise ValueError("BASICROUTER_SESSION_ID is required")
    directory = os.path.dirname(path)
    with FileLock(path + ".lock"):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.path.islink(directory) or os.path.islink(path):
            raise ValueError("SESSION_KEY_SYMLINK_BLOCKED")
        os.chmod(directory, stat.S_IRWXU)
        fd, temporary = tempfile.mkstemp(prefix=".key-", dir=directory)
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w") as f:
                f.write(key)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    return path


def clear_key(explicit_session_id=None):
    path = session_key_path(explicit_session_id)
    if path:
        with FileLock(path + ".lock"):
            validation_path = session_validation_path(explicit_session_id)
            for candidate in (path, validation_path):
                if candidate and os.path.isfile(candidate) and not os.path.islink(candidate):
                    os.remove(candidate)
            if os.path.isdir(os.path.dirname(path)):
                try:
                    os.rmdir(os.path.dirname(path))
                except OSError:
                    pass
    return path


def credential_quarantine_status(explicit_session_id=None):
    """Return session/global denial metadata without exposing key material."""
    session_record = quarantine_status(explicit_session_id)
    candidate = _read_cached_key(explicit_session_id)
    if not candidate and os.environ.get("BR_ALLOW_GLOBAL_KEY") == "1":
        candidate = (os.environ.get(API_KEY_ENV) or "").strip() or None
    return session_record or global_quarantine_status(candidate)


def quarantine_key(explicit_session_id=None, *, actor, reason):
    """Atomically deny the current credential fingerprint and erase its cache."""
    if not str(actor or "").strip() or not str(reason or "").strip():
        raise ValueError("CREDENTIAL_QUARANTINE_ACTOR_REASON_REQUIRED")
    existing = quarantine_status(explicit_session_id)
    if existing:
        _record_global_quarantine(
            existing["key_sha256"],
            actor=existing.get("actor") or actor,
            reason=existing.get("reason") or reason,
            quarantined_at=existing.get("quarantined_at") or
            datetime.now().astimezone().isoformat(timespec="seconds"))
        cached = _read_cached_key(explicit_session_id)
        if cached and key_fingerprint(cached) == existing["key_sha256"]:
            clear_key(explicit_session_id)
        return {key: value for key, value in existing.items()
                if key not in ("key_sha256", "path")}
    key = _read_cached_key(explicit_session_id)
    if not key and os.environ.get("BR_ALLOW_GLOBAL_KEY") == "1":
        key = (os.environ.get(API_KEY_ENV) or "").strip() or None
    if not key:
        raise ValueError("CREDENTIAL_QUARANTINE_KEY_REQUIRED")
    path = session_quarantine_path(explicit_session_id)
    if not path:
        raise ValueError("BASICROUTER_SESSION_ID is required")
    quarantined_at = datetime.now().astimezone().isoformat(timespec="seconds")
    record = {
        "schema_version": 1,
        "kind": "basicrouter_session_credential_quarantine",
        "active": True, "key_sha256": key_fingerprint(key),
        "actor": str(actor), "reason": str(reason),
        "quarantined_at": quarantined_at,
    }
    # Global denial is persisted before plaintext cache removal. A crash can
    # therefore leave a denied cache, never an allowed exposed cache.
    _record_global_quarantine(
        record["key_sha256"], actor=actor, reason=reason,
        quarantined_at=quarantined_at)
    with FileLock(path + ".lock"):
        _atomic_private_json(path, record)
    clear_key(explicit_session_id)
    return {key: value for key, value in record.items()
            if key != "key_sha256"}


def release_quarantine(validated_key, explicit_session_id=None):
    """Release a session marker only after a different key validated online."""
    record = quarantine_status(explicit_session_id)
    if not record:
        return False
    fingerprint = key_fingerprint(validated_key)
    if fingerprint == record.get("key_sha256"):
        raise ValueError("QUARANTINED_KEY_REUSE_BLOCKED")
    stored = _read_cached_key(explicit_session_id)
    if not stored or key_fingerprint(stored) != fingerprint:
        raise ValueError("QUARANTINE_RELEASE_KEY_MISMATCH")
    path = record["path"]
    with FileLock(path + ".lock"):
        if os.path.exists(path):
            os.remove(path)
    return True


def _validate(key):
    # import here so `check`/`get` work even without network
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    return br_client.validate_key(key)


def main(argv):
    if argv and argv[0] in ("--help", "-h"):
        print("用法：key_setup.py init|gate|save (--stdin|--secure-prompt)|get|clear|quarantine|"
              "quarantine-status --session-id <公共会话ID>")
        return 0
    if not argv:
        print("usage: key_setup.py {init|check|gate|save (--stdin|--secure-prompt)|get|clear|"
              "quarantine|quarantine-status} "
              "[--session-id ID] [--host-session-id ID]")
        return 2
    cmd = argv[0]
    explicit_sid = None
    host_sid = None
    quarantine_actor = None
    quarantine_reason = None
    if "--session-id" in argv:
        index = argv.index("--session-id")
        if index + 1 >= len(argv):
            print("ERROR: no session ID provided")
            return 2
        explicit_sid = argv[index + 1]
        argv = argv[:index] + argv[index + 2:]
    if "--host-session-id" in argv:
        index = argv.index("--host-session-id")
        if index + 1 >= len(argv):
            print("ERROR: no host session ID provided")
            return 2
        host_sid = argv[index + 1].strip()
        argv = argv[:index] + argv[index + 2:]
    if "--actor" in argv:
        index = argv.index("--actor")
        if index + 1 >= len(argv):
            print("ERROR: no quarantine actor provided")
            return 2
        quarantine_actor = argv[index + 1].strip()
        argv = argv[:index] + argv[index + 2:]
    if "--reason" in argv:
        index = argv.index("--reason")
        if index + 1 >= len(argv):
            print("ERROR: no quarantine reason provided")
            return 2
        quarantine_reason = argv[index + 1].strip()
        argv = argv[:index] + argv[index + 2:]
    if cmd == "init":
        if host_sid:
            digest = hashlib.sha256(host_sid.encode("utf-8")).hexdigest()[:32]
            print("br-session-%s" % digest)
        else:
            print("br-session-" + secrets.token_urlsafe(24))
        return 0
    resolved_sid = explicit_sid or ensure_session_id(host_sid)
    try:
        quarantine = quarantine_status(resolved_sid)
        current_quarantine = credential_quarantine_status(resolved_sid)
    except ValueError as exc:
        print("QUARANTINED_INVALID: %s" % exc)
        return 1
    if cmd == "check":
        if current_quarantine:
            print("QUARANTINED")
            return 1
        key = load_key(resolved_sid)
        print(validation_status(key, resolved_sid) if key else "MISSING")
        return 0
    if cmd == "gate":
        if not resolved_sid:
            print("SESSION_REQUIRED\n请由当前 Agent 为本次会话设置 BASICROUTER_SESSION_ID。")
            return 1
        try:
            require_key(explicit_session_id=resolved_sid)
        except SessionKeyError as exc:
            print(exc.client_message)
            return 1
        else:
            # Keep the public protocol's historical STORED token while making
            # authenticated readiness explicit for diagnostics/monitoring.
            print("STORED\nVALIDATED")
            return 0
    if cmd == "get":
        if current_quarantine:
            print("QUARANTINED")
            return 1
        k = load_key(resolved_sid)
        # Readiness diagnostics must never echo even a partial credential.
        print("STORED" if k else "MISSING")
        return 0 if k else 1
    if cmd == "clear":
        clear_key(resolved_sid)
        print("CLEARED; QUARANTINE_REMAINS" if current_quarantine else "CLEARED")
        return 0
    if cmd == "quarantine-status":
        if current_quarantine:
            when = ((quarantine or {}).get("quarantined_at") or
                    current_quarantine.get("first_quarantined_at") or
                    "unknown")
            print("QUARANTINED: active since %s" % when)
            return 0
        print("NOT_QUARANTINED")
        return 0
    if cmd == "quarantine":
        if not quarantine_actor or not quarantine_reason:
            print("ERROR: quarantine requires --actor and --reason")
            return 2
        try:
            record = quarantine_key(
                resolved_sid, actor=quarantine_actor,
                reason=quarantine_reason)
        except ValueError as exc:
            print("ERROR: %s" % exc)
            return 1
        print("QUARANTINED: active since %s" %
              record.get("quarantined_at", "unknown"))
        return 0
    if cmd == "save":
        if not resolved_sid:
            print("ERROR: BASICROUTER_SESSION_ID is required")
            return 1
        modes = argv[1:]
        if modes not in (["--stdin"], ["--secure-prompt"]):
            print("ERROR: 安全策略禁止把密钥放在命令行参数中；"
                  "请使用 save --stdin 或 save --secure-prompt。")
            return 2
        if modes == ["--secure-prompt"]:
            try:
                import secure_credential_input
                key = secure_credential_input.read_secret()
            except secure_credential_input.SecureInputCancelled as exc:
                print("SECURE_INPUT_CANCELLED: %s" % exc)
                return 1
            except secure_credential_input.SecureInputUnavailable as exc:
                print("SECURE_INPUT_UNAVAILABLE: %s" % exc)
                return 1
            except secure_credential_input.SecureInputError:
                print("SECURE_INPUT_FAILED: 本机安全输入未完成，请重试。")
                return 1
        else:
            key = sys.stdin.readline().strip()
        if not key:
            print("ERROR: no key provided")
            return 2
        if not key.startswith("sk-"):
            print("ERROR: 密钥格式不正确，请通过安全输入提交以 sk- 开头的 BasicRouter 密钥。")
            return 2
        try:
            submitted_quarantine = global_quarantine_status(key)
        except ValueError as exc:
            print("QUARANTINED_INVALID: %s" % exc)
            return 1
        if ((quarantine and
             key_fingerprint(key) == quarantine.get("key_sha256")) or
                submitted_quarantine):
            print(
                "QUARANTINED_KEY_REUSE_BLOCKED: 该凭证已因暴露风险被隔离，"
                "请在 BasicRouter 侧轮换后，通过安全输入提交不同的新密钥。")
            return 1
        ok, msg = _validate(key)
        if not ok:
            print("INVALID: %s" % msg)
            return 1
        try:
            save_key(key, resolved_sid)
            if quarantine:
                release_quarantine(key, resolved_sid)
            mark_key_validated(key, resolved_sid)
        except ValueError as exc:
            print("ERROR: %s" % exc)
            return 1
        print("SAVED (VALIDATED)%s" % (
            "; QUARANTINE_RELEASED" if quarantine else ""))
        return 0
    print("unknown command: %s" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
