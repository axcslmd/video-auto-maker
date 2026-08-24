#!/usr/bin/env python3
"""Shared validation helpers for project-owned identifiers and paths."""
import os
import re
import json
import hashlib
import time
import uuid
import tempfile
import shutil


CLIENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,46}[a-z0-9])?$")
COMPONENT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?$")


class LockTimeoutError(TimeoutError):
    """Raised when a project lock cannot be acquired before its deadline."""


def sha256_file(path, *, block_size=1024 * 1024):
    """Return a streaming SHA-256 digest for one regular file.

    Keeping byte identity here prevents workflow adapters from growing subtly
    different hashing implementations. Callers remain responsible for path
    containment and for translating a missing file into their domain error.
    """
    if not isinstance(block_size, int) or block_size <= 0:
        raise ValueError("HASH_BLOCK_SIZE_INVALID")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _process_start_identity(pid):
    """Return a PID-reuse-resistant process identity where the OS exposes one."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "posix":
        stat_path = "/proc/%d/stat" % pid
        try:
            with open(stat_path, "rb") as handle:
                fields = handle.read().split()
            if len(fields) > 21:
                return "proc:%s" % fields[21].decode("ascii")
        except (OSError, UnicodeError):
            pass
        try:
            import subprocess
            value = subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                stderr=subprocess.DEVNULL, text=True).strip()
            return "ps:%s" % value if value else None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def _process_is_same_live_owner(pid, start_identity):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    current = _process_start_identity(pid)
    # A live PID is never considered stale when either side lacks a reliable
    # start identity. This fails closed instead of stealing another process's lock.
    return not start_identity or not current or current == start_identity


class FileLock:
    """Small cross-platform lockfile based on atomic ``O_EXCL`` creation."""

    def __init__(self, path, *, timeout=10.0, stale_after=300.0,
                 poll_interval=0.05):
        self.path = os.path.abspath(path)
        self.timeout = timeout
        self.stale_after = stale_after
        self.poll_interval = poll_interval
        self.token = uuid.uuid4().hex
        self._acquired = False

    def _remove_if_stale(self):
        if self.stale_after is None:
            return False
        try:
            before = os.stat(self.path)
        except FileNotFoundError:
            return True
        if time.time() - before.st_mtime <= self.stale_after:
            return False
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags)
            try:
                owner = json.loads(os.read(fd, 16384).decode("ascii"))
            finally:
                os.close(fd)
        except (OSError, ValueError, UnicodeError):
            owner = {}
        pid = owner.get("pid")
        if _process_is_same_live_owner(pid, owner.get("process_start")):
            return False
        try:
            current = os.stat(self.path)
            if (current.st_mtime_ns, current.st_size) != (before.st_mtime_ns, before.st_size):
                return False
            os.unlink(self.path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def acquire(self):
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        started = time.monotonic()
        payload = json.dumps({"token": self.token, "pid": os.getpid(),
                              "process_start": _process_start_identity(os.getpid()),
                              "created_at": time.time()}).encode("ascii")
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self._acquired = True
                return self
            except FileExistsError:
                if self._remove_if_stale():
                    continue
                if self.timeout is not None and time.monotonic() - started >= self.timeout:
                    raise LockTimeoutError("LOCK_TIMEOUT: %s" % self.path)
                time.sleep(self.poll_interval)

    def release(self):
        if not self._acquired:
            return
        try:
            with open(self.path, "rb") as handle:
                owner = json.loads(handle.read().decode("ascii"))
            if owner.get("token") == self.token:
                os.unlink(self.path)
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            # Lock-protected ledgers/manifests can contain sensitive task metadata.
            if self.path.endswith(".lock"):
                protected = self.path[:-5]
                if os.path.isfile(protected) and not os.path.islink(protected):
                    try:
                        os.chmod(protected, 0o600)
                    except OSError:
                        pass
            self._acquired = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


def validate_client(client):
    """Reject unsafe client values before using them as directory names."""
    if not isinstance(client, str) or not CLIENT_RE.fullmatch(client):
        raise ValueError(
            "CLIENT_INVALID: client 必须是英文小写 slug，只能包含 a-z、0-9、_、-，"
            "长度 1-48，且不能以 _ 或 - 开头/结尾。"
        )
    return client


def validate_component(value, label="identifier"):
    """Validate one project-owned directory component.

    Client slugs have a stricter lowercase rule. SKU, actor and run identifiers
    are allowed to retain case, but may never contain separators or traversal.
    """
    if not isinstance(value, str) or not COMPONENT_RE.fullmatch(value):
        raise ValueError(
            "%s_INVALID: %s 必须是单一目录标识，不能包含路径分隔符或 '..'。"
            % (label.upper(), label)
        )
    return value


def validate_run_id(run_id):
    return validate_component(run_id, "run_id")


def safe_project_path(root, *parts):
    """Join project path parts and assert the result stays below root."""
    root = os.path.abspath(root)
    for part in parts:
        if not isinstance(part, str) or os.path.isabs(part):
            raise ValueError("PATH_INVALID: 项目路径只能使用相对路径片段")
    candidate = os.path.abspath(os.path.join(root, *parts))
    if os.path.commonpath((root, candidate)) != root:
        raise ValueError("PATH_ESCAPE: 路径不能逃出项目目录")
    return candidate


def require_contained_path(root, path, *, label="path", must_exist=False,
                           reject_symlinks=True):
    """Resolve a path and require it to remain under a trusted directory."""
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(path)
    root_real = os.path.realpath(root_abs)
    candidate_real = os.path.realpath(candidate)
    if os.path.commonpath((root_real, candidate_real)) != root_real:
        raise ValueError("%s_ESCAPE: 路径不能逃出受控目录" % label.upper())
    if reject_symlinks:
        current = candidate
        while True:
            if os.path.lexists(current) and os.path.islink(current):
                raise ValueError("%s_SYMLINK_BLOCKED: 不允许符号链接" % label.upper())
            if current == root_abs:
                break
            parent = os.path.dirname(current)
            if parent == current or os.path.commonpath((root_abs, parent)) != root_abs:
                break
            current = parent
    if must_exist and not os.path.isfile(candidate):
        raise ValueError("%s_MISSING: %s" % (label.upper(), candidate))
    return candidate


def _reject_destination_symlinks(path, label="destination"):
    """Reject a symlink target or its immediate destination directory."""
    absolute = os.path.abspath(path)
    for current in (absolute, os.path.dirname(absolute)):
        if os.path.lexists(current) and os.path.islink(current):
            raise ValueError("%s_SYMLINK_BLOCKED: 不允许符号链接" % label.upper())
    return absolute


def fsync_directory(directory):
    """Durably commit a rename on filesystems that support directory fsync."""
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path, data, *, mode=0o600, label="destination"):
    """Write bytes through a same-directory file, fsync, and atomic replace."""
    path = _reject_destination_symlinks(path, label)
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        raise ValueError("%s_PARENT_MISSING" % label.upper())
    fd, temporary = tempfile.mkstemp(prefix=".%s." % os.path.basename(path),
                                     suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _reject_destination_symlinks(path, label)
        os.replace(temporary, path)
        os.chmod(path, mode)
        fsync_directory(directory)
        temporary = None
    finally:
        if fd is not None:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return path


def atomic_write_json(path, value, *, mode=0o600):
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return atomic_write_bytes(path, encoded, mode=mode, label="json_destination")


def atomic_copy_file(source, destination, *, mode=0o600):
    """Copy a regular non-symlink file without exposing a partial destination."""
    if os.path.islink(source) or not os.path.isfile(source):
        raise ValueError("SOURCE_INVALID: 源文件必须是普通文件且不能是符号链接")
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    try:
        destination = _reject_destination_symlinks(destination, "destination")
        directory = os.path.dirname(destination) or "."
        fd, temporary = tempfile.mkstemp(prefix=".%s." % os.path.basename(destination),
                                         suffix=".tmp", dir=directory)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as output, os.fdopen(source_fd, "rb") as input_file:
                fd = None
                source_fd = None
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            _reject_destination_symlinks(destination, "destination")
            os.replace(temporary, destination)
            os.chmod(destination, mode)
            fsync_directory(directory)
            temporary = None
        finally:
            if fd is not None:
                os.close(fd)
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
    finally:
        if source_fd is not None:
            os.close(source_fd)
    return destination
