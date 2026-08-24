#!/usr/bin/env python3
"""Host-neutral preparation helper for the /video-auto-maker conversation entry."""
import argparse
import contextlib
import io
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import key_setup
import api_endpoint_setup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _run(command, env=None, quiet=False):
    kwargs = {"cwd": ROOT, "env": env, "check": False}
    if quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    return subprocess.run(command, **kwargs).returncode


def _gate_with_optional_secure_prompt(public_session_id, secure_prompt=False):
    """Run the exact-session gate and optionally recover through a password UI."""
    if not secure_prompt:
        return key_setup.main(["gate", "--session-id", public_session_id])
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        gate = key_setup.main(["gate", "--session-id", public_session_id])
    gate_copy = output.getvalue()
    if gate == 0:
        print(gate_copy, end="")
        return 0
    status = gate_copy.splitlines()[0] if gate_copy else ""
    if status not in {"BLOCKED", "BLOCKED_INVALID_KEY"}:
        print(gate_copy, end="")
        return gate
    saved = key_setup.main([
        "save", "--secure-prompt", "--session-id", public_session_id])
    if saved != 0:
        print(gate_copy, end="")
        return saved
    return key_setup.main(["gate", "--session-id", public_session_id])

def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare the common Agent workflow")
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--session-id", help="public BasicRouter conversation session ID")
    parser.add_argument("--host-session-id", help="opaque stable host task/session ID")
    parser.add_argument(
        "--secure-key-prompt", action="store_true",
        help="when the key is missing, open a native password prompt instead of asking in chat")
    args = parser.parse_args(argv)
    if args.session_id:
        os.environ[key_setup.SESSION_ENV] = args.session_id.strip()
    public_session_id = key_setup.ensure_session_id(args.host_session_id)
    print("PUBLIC_SESSION_ID=%s" % public_session_id, flush=True)
    # The key validation ping must already know which text gateway to use.
    # New installations therefore collect all three route roots first.
    if api_endpoint_setup.ensure_initialized(prompt_if_missing=True) != 0:
        return 1
    gate = _gate_with_optional_secure_prompt(
        public_session_id, secure_prompt=args.secure_key_prompt)
    sys.stdout.flush()
    if gate != 0:
        # No environment bootstrap, customer guidance, or workflow routing is
        # allowed until this exact conversation session is authorized.
        return gate
    _run([sys.executable, "scripts/offline_setup.py", "hydrate"], quiet=True)
    check = _run([sys.executable, "scripts/setup_env.py", "full-check"])
    if check == 0:
        print("READY")
        return 0
    if os.name == "nt":
        print("BOOTSTRAP_REQUIRED: use the inline PowerShell command from AGENT_ENTRY_PROTOCOL.md")
        return 2
    env = os.environ.copy()
    env["AGENT_INLINE_BOOTSTRAP"] = "1"
    bootstrap = _run(["bash", "deploy.sh"], env=env)
    if bootstrap != 0:
        return bootstrap
    return _run([sys.executable, "scripts/setup_env.py", "full-check"])

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
