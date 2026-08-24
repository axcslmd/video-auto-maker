import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import prompt_review
import run_manifest
import storyboard
import explainer_layout_preview
import agent_entry
import agent_runtime
import key_setup
import motion_design
import package_delivery
import remotion_engine
import skill_contract
import video_use_bridge
import workflow_router
import release_audit
import secure_credential_input
import setup_env


class WorkflowStateConsistencyTests(unittest.TestCase):
    def test_credential_quarantine_blocks_load_gate_and_same_key_reuse(self):
        old_key = "sk-" + "exposed-credential-for-test-only-123456"
        session = "br-session-quarantine-test"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: session,
                }, clear=True):
            key_setup.save_key(old_key)
            record = key_setup.quarantine_key(
                actor="unit-test", reason="credential appeared in chat")
            self.assertTrue(record["active"])
            self.assertNotIn("key_sha256", record)
            marker_path = key_setup.session_quarantine_path()
            self.assertEqual(os.stat(marker_path).st_mode & 0o777, 0o600)
            self.assertNotIn(
                old_key, Path(marker_path).read_text(encoding="utf-8"))
            global_marker = key_setup.global_quarantine_path()
            self.assertEqual(os.stat(global_marker).st_mode & 0o777, 0o600)
            self.assertNotIn(
                old_key, Path(global_marker).read_text(encoding="utf-8"))
            self.assertTrue(key_setup.global_quarantine_status(old_key)["active"])
            self.assertIsNone(key_setup.load_key())
            self.assertIsNone(key_setup.load_key(include_quarantined=True))

            gate_output = io.StringIO()
            with contextlib.redirect_stdout(gate_output):
                self.assertEqual(key_setup.main(["gate"]), 1)
            self.assertIn("QUARANTINED", gate_output.getvalue())
            self.assertNotIn("直接粘贴到对话框", gate_output.getvalue())

            reuse_output = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(old_key + "\n")), \
                    mock.patch.object(key_setup, "_validate") as validator, \
                    contextlib.redirect_stdout(reuse_output):
                self.assertEqual(key_setup.main(["save", "--stdin"]), 1)
            validator.assert_not_called()
            self.assertIn("QUARANTINED_KEY_REUSE_BLOCKED", reuse_output.getvalue())
            self.assertNotIn(old_key, reuse_output.getvalue())

    def test_different_validated_key_releases_credential_quarantine(self):
        old_key = "sk-" + "old-exposed-credential-for-test-only"
        new_key = "sk-" + "new-rotated-credential-for-test-only"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: "br-session-quarantine-release",
                }, clear=True):
            key_setup.save_key(old_key)
            key_setup.quarantine_key(
                actor="unit-test", reason="credential appeared in chat")
            output = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(new_key + "\n")), \
                    mock.patch.object(
                        key_setup, "_validate", return_value=(True, "ok")) as validator, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(key_setup.main(["save", "--stdin"]), 0)
            validator.assert_called_once_with(new_key)
            self.assertIn("QUARANTINE_RELEASED", output.getvalue())
            self.assertNotIn(new_key, output.getvalue())
            self.assertIsNone(key_setup.quarantine_status())
            self.assertEqual(key_setup.load_key(), new_key)
            self.assertEqual(
                key_setup.validation_status(new_key), "VALIDATED")
            self.assertTrue(key_setup.global_quarantine_status(old_key)["active"])

    def test_global_credential_quarantine_blocks_other_sessions_and_env(self):
        old_key = "sk-" + "globally-exposed-credential-test-only"
        session_a = "br-session-global-quarantine-a"
        session_b = "br-session-global-quarantine-b"
        session_c = "br-session-global-quarantine-c"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: session_a,
                }, clear=True):
            key_setup.save_key(old_key, session_a)
            key_setup.quarantine_key(
                session_a, actor="unit-test", reason="credential appeared in chat")
            key_setup.save_key(old_key, session_b)
            self.assertIsNone(key_setup.load_key(session_b))
            self.assertIsNone(
                key_setup.load_key(session_b, include_quarantined=True))
            gate_output = io.StringIO()
            with mock.patch.dict(os.environ, {
                        key_setup.SESSION_ENV: session_b,
                    }, clear=True), contextlib.redirect_stdout(gate_output):
                self.assertEqual(key_setup.main(["gate"]), 1)
            self.assertEqual(gate_output.getvalue().splitlines()[0], "QUARANTINED")

            output = io.StringIO()
            with mock.patch.dict(os.environ, {
                        key_setup.SESSION_ENV: session_b,
                    }, clear=True), \
                    mock.patch("sys.stdin", io.StringIO(old_key + "\n")), \
                    mock.patch.object(key_setup, "_validate") as validator, \
                    contextlib.redirect_stdout(output):
                self.assertEqual(key_setup.main(["save", "--stdin"]), 1)
            validator.assert_not_called()
            self.assertIn("QUARANTINED_KEY_REUSE_BLOCKED", output.getvalue())
            self.assertNotIn(old_key, output.getvalue())

            with mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: session_c,
                    key_setup.API_KEY_ENV: old_key,
                    "BR_ALLOW_GLOBAL_KEY": "1",
            }, clear=True):
                self.assertIsNone(key_setup.load_key(session_c))

    def test_invalid_global_quarantine_index_fails_closed(self):
        key = "sk-" + "credential-global-index-corruption-test"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: "br-session-invalid-global-index",
                }, clear=True):
            key_setup.save_key(key)
            Path(key_setup.global_quarantine_path()).write_text(
                "not-json", encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "GLOBAL_CREDENTIAL_QUARANTINE_FILE_INVALID"):
                key_setup.load_key()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(key_setup.main(["gate"]), 1)
            self.assertIn("QUARANTINED_INVALID", output.getvalue())

    def test_clear_does_not_erase_quarantined_key_fingerprint(self):
        old_key = "sk-" + "exposed-credential-clear-test-only"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: "br-session-quarantine-clear",
                }, clear=True):
            key_setup.save_key(old_key)
            key_setup.quarantine_key(
                actor="unit-test", reason="credential appeared in chat")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(key_setup.main(["clear"]), 0)
            self.assertIn("QUARANTINE_REMAINS", output.getvalue())
            self.assertTrue(key_setup.quarantine_status()["active"])
            self.assertIsNone(key_setup.load_key())
            self.assertIsNone(key_setup.load_key(include_quarantined=True))

    def test_missing_key_gate_and_get_never_expose_secret_in_plain_chat(self):
        key = "sk-" + "diagnostic-output-must-not-leak"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: "br-session-safe-gate-copy",
                }, clear=True):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(key_setup.main(["gate"]), 1)
            copy = output.getvalue()
            self.assertIn("save --stdin", copy)
            self.assertIn("save --secure-prompt", copy)
            self.assertIn("请勿把密钥粘贴到普通聊天消息中", copy)
            self.assertNotIn("直接粘贴到对话框", copy)
            key_setup.save_key(key)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(key_setup.main(["get"]), 0)
            self.assertEqual(output.getvalue().strip(), "STORED")
            self.assertNotIn(key[:4], output.getvalue())

    def test_macos_secure_prompt_captures_secret_without_command_argument(self):
        secret = "sk-" + "native-dialog-test-only"
        completed = mock.Mock(returncode=0, stdout=secret + "\n")
        with mock.patch.object(
                secure_credential_input.shutil, "which",
                return_value="/usr/bin/osascript"), \
                mock.patch.object(
                    secure_credential_input.subprocess, "run",
                    return_value=completed) as runner:
            self.assertEqual(secure_credential_input._macos_prompt(), secret)
        command = runner.call_args.args[0]
        self.assertNotIn(secret, "\0".join(command))
        self.assertEqual(runner.call_args.kwargs["stderr"],
                         secure_credential_input.subprocess.DEVNULL)

    def test_secure_prompt_save_validates_without_echoing_secret(self):
        secret = "sk-" + "secure-prompt-save-test-only"
        session = "br-session-secure-prompt-save"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: session,
                }, clear=True), \
                mock.patch.object(
                    secure_credential_input, "read_secret",
                    return_value=secret), \
                mock.patch.object(
                    key_setup, "_validate", return_value=(True, "ok")):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = key_setup.main(["save", "--secure-prompt"])
            self.assertEqual(result, 0)
            self.assertEqual(key_setup.load_key(), secret)
            self.assertEqual(output.getvalue().strip(), "SAVED (VALIDATED)")
            self.assertNotIn(secret, output.getvalue())

    def test_cancelled_secure_prompt_never_validates_or_saves(self):
        session = "br-session-secure-prompt-cancel"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: session,
                }, clear=True), \
                mock.patch.object(
                    secure_credential_input, "read_secret",
                    side_effect=secure_credential_input.SecureInputCancelled(
                        "已取消安全密钥录入。")), \
                mock.patch.object(key_setup, "_validate") as validator:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = key_setup.main(["save", "--secure-prompt"])
            self.assertEqual(result, 1)
            self.assertIn("SECURE_INPUT_CANCELLED", output.getvalue())
            validator.assert_not_called()
            self.assertIsNone(key_setup.load_key())

    def test_project_guidance_never_instructs_plain_chat_secret_entry(self):
        legacy_phrases = (
            "客户粘贴 key",
            "客户在对话框粘贴",
            "客户粘贴 `sk-`",
            "请把你的 BasicRouter API Key 贴给我",
            "请重新粘贴一个以 sk- 开头的密钥",
            "收到客户粘贴的 key",
            "在对话里点头/粘贴 key",
            "在对话里请客户粘贴",
            "customer pastes the key in chat",
            "collect the key in chat",
            "paste your sk- key",
            "直接粘贴到当前对话框",
        )
        roots = [
            Path(ROOT, "README.md"),
            Path(ROOT, "AGENTS.md"),
            Path(ROOT, "AGENT_ENTRY_PROTOCOL.md"),
            Path(ROOT, "START_HERE_AGENT.md"),
            Path(ROOT, "prompts"),
            Path(ROOT, "skills"),
            Path(ROOT, "adapters"),
            Path(ROOT, ".codex"),
            Path(ROOT, ".kilo"),
            Path(ROOT, ".workbuddy"),
            Path(ROOT, "scripts"),
        ]
        violations = []
        for root in roots:
            paths = [root] if root.is_file() else [
                path for path in root.rglob("*") if path.is_file()
                and path.suffix.lower() in (".md", ".py")
            ]
            for path in paths:
                text = path.read_text(encoding="utf-8")
                for phrase in legacy_phrases:
                    if phrase in text:
                        violations.append("%s: %s" % (path, phrase))
        self.assertEqual(violations, [])

    def test_workbuddy_runtime_detection_is_diagnostic_and_has_unknown_fallback(self):
        self.assertEqual(
            agent_runtime.detect_agent_runtime(
                environ={"WORKBUDDY_CONFIG_DIR": "/tmp/wb"}),
            {"name": "workbuddy", "source": "WORKBUDDY_CONFIG_DIR"})
        self.assertEqual(
            agent_runtime.detect_agent_runtime(environ={}),
            {"name": "unknown", "source": "unknown"})
        self.assertEqual(
            agent_runtime.detect_agent_runtime(
                explicit="private-agent", environ={"WORKBUDDY_CONFIG_DIR": "/tmp/wb"}),
            {"name": "private-agent", "source": "explicit"})

    def test_new_session_does_not_read_legacy_key_cache(self):
        fake_key = "sk-" + "legacy-not-authorized-" * 2
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(key_setup, "SESSION_DIR", tmp), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None), \
                mock.patch.dict(os.environ, {
                    key_setup.SESSION_ENV: "br-session-current",
                }, clear=True):
            key_setup.save_key(fake_key, "br-codex-legacy")
            self.assertIsNone(key_setup.load_key())
            self.assertEqual(key_setup.load_key("br-codex-legacy"), fake_key)

    def test_workbuddy_task_id_can_seed_public_session_id(self):
        expected = "br-session-%s" % __import__("hashlib").sha256(
            b"wb-task-123").hexdigest()[:32]
        with mock.patch.dict(os.environ, {
                key_setup.SESSION_ENV: "",
                "WORKBUDDY_TASK_ID": "wb-task-123",
        }, clear=True), mock.patch.object(key_setup, "SESSION_STATE_FILE", None):
            self.assertEqual(key_setup.ensure_session_id(), expected)

    def test_unknown_host_gets_fresh_non_project_session_id(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(key_setup, "SESSION_STATE_FILE", None):
            session = key_setup.ensure_session_id()
        self.assertTrue(session.startswith("br-session-"), session)
        self.assertNotIn("project", session)

    def test_agent_entry_blocks_before_bootstrap_when_key_is_missing(self):
        with mock.patch.object(
                agent_entry.key_setup, "ensure_session_id",
                return_value="br-session-workbuddy"), \
                mock.patch.object(
                    agent_entry.api_endpoint_setup, "ensure_initialized",
                    return_value=0), \
                mock.patch.object(agent_entry.key_setup, "main", return_value=1) as gate, \
                mock.patch.object(agent_entry, "_run") as runner:
            result = agent_entry.main(["prepare"])
        self.assertEqual(result, 1)
        gate.assert_called_once_with([
            "gate", "--session-id", "br-session-workbuddy"])
        runner.assert_not_called()

    def test_agent_entry_prepares_environment_after_key_is_stored(self):
        with mock.patch.object(
                agent_entry.key_setup, "ensure_session_id",
                return_value="br-session-workbuddy"), \
                mock.patch.object(
                    agent_entry.api_endpoint_setup, "ensure_initialized",
                    return_value=0), \
                mock.patch.object(agent_entry.key_setup, "main", return_value=0) as gate, \
                mock.patch.object(agent_entry, "_run", return_value=0) as runner:
            result = agent_entry.main(["prepare"])
        self.assertEqual(result, 0)
        gate.assert_called_once_with([
            "gate", "--session-id", "br-session-workbuddy"])
        self.assertEqual(runner.call_count, 2)

    def test_agent_entry_secure_prompt_recovers_missing_key_before_bootstrap(self):
        calls = []

        def key_command(argv):
            calls.append(list(argv))
            if argv[0] == "save":
                print("SAVED (VALIDATED)")
                return 0
            if sum(1 for item in calls if item[0] == "gate") == 1:
                print("BLOCKED\n———\nmissing")
                return 1
            print("STORED\nVALIDATED")
            return 0

        with mock.patch.object(
                agent_entry.key_setup, "ensure_session_id",
                return_value="br-session-native-prompt"), \
                mock.patch.object(
                    agent_entry.api_endpoint_setup, "ensure_initialized",
                    return_value=0), \
                mock.patch.object(
                    agent_entry.key_setup, "main",
                    side_effect=key_command), \
                mock.patch.object(agent_entry, "_run", return_value=0) as runner:
            result = agent_entry.main(["prepare", "--secure-key-prompt"])
        self.assertEqual(result, 0)
        self.assertEqual(calls, [
            ["gate", "--session-id", "br-session-native-prompt"],
            ["save", "--secure-prompt", "--session-id",
             "br-session-native-prompt"],
            ["gate", "--session-id", "br-session-native-prompt"],
        ])
        self.assertEqual(runner.call_count, 2)

    def test_agent_entry_secure_prompt_does_not_bypass_quarantine(self):
        calls = []

        def quarantined_gate(argv):
            calls.append(list(argv))
            print("QUARANTINED\n———\nrotate first")
            return 1

        with mock.patch.object(
                agent_entry.key_setup, "ensure_session_id",
                return_value="br-session-quarantined"), \
                mock.patch.object(
                    agent_entry.api_endpoint_setup, "ensure_initialized",
                    return_value=0), \
                mock.patch.object(
                    agent_entry.key_setup, "main",
                    side_effect=quarantined_gate), \
                mock.patch.object(agent_entry, "_run") as runner:
            result = agent_entry.main(["prepare", "--secure-key-prompt"])
        self.assertEqual(result, 1)
        self.assertEqual(calls, [[
            "gate", "--session-id", "br-session-quarantined"]])
        runner.assert_not_called()

    def test_agent_entry_collects_endpoints_before_key_gate(self):
        with mock.patch.object(
                agent_entry.key_setup, "ensure_session_id",
                return_value="br-session-endpoint-setup"), \
                mock.patch.object(
                    agent_entry.api_endpoint_setup, "ensure_initialized",
                    return_value=1) as endpoint_gate, \
                mock.patch.object(agent_entry.key_setup, "main") as key_gate, \
                mock.patch.object(agent_entry, "_run") as runner:
            result = agent_entry.main(["prepare"])
        self.assertEqual(result, 1)
        endpoint_gate.assert_called_once_with(prompt_if_missing=True)
        key_gate.assert_not_called()
        runner.assert_not_called()

    def test_setup_key_status_reads_only_exact_public_session(self):
        with mock.patch.object(
                key_setup, "require_key",
                side_effect=key_setup.SessionKeyRequired()) as loader:
            self.assertFalse(setup_env._basicrouter_key_present())
        loader.assert_called_once_with()

    def test_router_contract_selects_existing_guidance_and_capabilities(self):
        contract = workflow_router.stage_contract({}, "product_usage")
        self.assertEqual(contract["protocol_version"],
                         workflow_router.ROUTER_CONTRACT_VERSION)
        self.assertIn("skills/product-consistency/SKILL.md",
                      contract["guidance_resources"])
        self.assertIn("local_image_preview",
                      contract["required_host_capabilities"])
        for relative in contract["guidance_resources"]:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, relative)), relative)

    def test_skills_are_bound_to_current_code_contracts(self):
        result = skill_contract.validate_skills()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["skill_count"], result["bound_count"])
        payload = skill_contract.contract_payload("story-video-orchestrator")
        self.assertEqual(payload["code_source"], "scripts/workflow_router.py")
        self.assertIn("product_usage", payload["stages"])
        self.assertIn("confirm_function", payload["stages"]["storyboard"])

    def test_delivery_includes_only_the_workbuddy_project_adapter(self):
        adapter = Path(".workbuddy/skills/video-auto-maker/SKILL.md")
        state = Path(".workbuddy/traces/private.json")
        self.assertFalse(package_delivery._is_excluded(Path(ROOT) / adapter, adapter))
        self.assertTrue(package_delivery._is_excluded(Path(ROOT) / state, state))
        codex_preview = Path(".codex-tmp/client-run/montage.png")
        self.assertTrue(package_delivery._is_excluded(
            Path(ROOT) / codex_preview, codex_preview))
        release_report = Path("RELEASE_VALIDATION.json")
        self.assertTrue(package_delivery._is_excluded(
            Path(ROOT) / release_report, release_report))
        render_temp = Path("remotion_engine/_props_abc123.json")
        self.assertTrue(package_delivery._is_excluded(
            Path(ROOT) / render_temp, render_temp, include_local_runtime=True))
        test_file = Path("tests/test_contract.py")
        self.assertFalse(package_delivery._is_excluded(
            Path(ROOT) / test_file, test_file, include_local_runtime=True))
        for name in package_delivery.FORBIDDEN_LEGACY_ROOT_FILES:
            relative = Path(name)
            self.assertTrue(package_delivery._is_excluded(
                Path(ROOT) / relative, relative), name)

    def test_formal_agent_project_layout_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in package_delivery.REQUIRED_ROOT_DIRS | {"offline-assets"}:
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in package_delivery.REQUIRED_ROOT_FILES:
                (root / name).write_text("ok", encoding="utf-8")
            for relative in (
                    "docs/customer/CUSTOMER_GUIDE.md",
                    "docs/operations/DELIVERY_CHECKLIST.md",
                    "docs/architecture/PROJECT_OVERVIEW.md",
                    "adapters/hermes/START_HERE.md",
                    "adapters/workbuddy/START_HERE.md",
                    "tests/test_one.py", "tests/test_two.py"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ok", encoding="utf-8")
            (root / "remotion_engine/node_modules").mkdir()
            result = package_delivery.validate_delivery_shape(
                root, include_local_runtime=True)
            self.assertTrue(result["ok"], result["errors"])
            (root / "remotion_engine/_props_leak.json").write_text("{}")
            result = package_delivery.validate_delivery_shape(
                root, include_local_runtime=True)
            self.assertIn("LAYOUT_GENERATED_RENDER_INPUT:_props_leak.json",
                          result["errors"])

    def test_delivery_zip_is_deterministic_and_manifest_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bundle"
            source.mkdir()
            (source / "run.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
            (source / "run.sh").chmod(0o755)
            package_delivery._write_delivery_manifest(
                source, ["run.sh"], [], None, False)
            first = Path(tmp) / "first.zip"
            second = Path(tmp) / "second.zip"
            package_delivery._zip_directory(source, first)
            package_delivery._zip_directory(source, second)
            self.assertEqual(package_delivery._sha256(first),
                             package_delivery._sha256(second))
            result = package_delivery.verify_archive(first, source)
            self.assertTrue(result["ok"], result["errors"])

    def test_delivery_zip_rejects_traversal_member(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("bundle/../escape.txt", "bad")
            result = package_delivery.verify_archive(archive)
            self.assertFalse(result["ok"])
            self.assertTrue(any("ARCHIVE_MEMBER_PATH_INVALID" in item
                                for item in result["errors"]))

    def test_release_report_rejects_stale_source_fingerprint(self):
        report = {
            "schema_version": 1, "kind": "release_validation", "status": "passed",
            "generated_at": "2026-08-18T00:00:00+00:00",
            "project_fingerprint": "a" * 64, "python": "CPython 3.9",
            "commands": [{"name": "gate%d" % i, "argv": ["python3", "gate"],
                          "exit_code": 0, "stdout_sha256": "b" * 64,
                          "stderr_sha256": "c" * 64, "duration_ms": 1}
                         for i in range(6)],
            "tests_passed": 1,
            "invariants": {"business_stage_count": 23, "chain_profile_count": 7,
                           "skill_count": 11, "host_adapter_count": 5,
                           "eval_case_count": 36, "offline_runtime_verified": True},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with mock.patch.object(release_audit, "source_fingerprint",
                                   return_value="d" * 64):
                result = release_audit.validate_report(path, require_current=True)
        self.assertIn("RELEASE_REPORT_SOURCE_STALE", result["errors"])

    def test_render_quality_profiles_change_actual_output_scale(self):
        self.assertEqual(remotion_engine.quality_scale("draft"), 0.25)
        self.assertEqual(remotion_engine.quality_scale("standard"), 0.5)
        self.assertEqual(remotion_engine.quality_scale("high"), 1.0)
        with self.assertRaisesRegex(ValueError, "REMOTION_QUALITY_INVALID"):
            remotion_engine.quality_scale("preview-ish")

    def test_output_dimensions_honor_explicit_contract(self):
        self.assertEqual(
            video_use_bridge._output_dimensions({
                "output_ratio": "16:9", "width": 960, "height": 540,
            }),
            (960, 540))
        self.assertEqual(
            video_use_bridge._output_dimensions({
                "output_ratio": "9:16", "resolution": [540, 960],
            }),
            (540, 960))

    def test_source_content_spec_fps_change_preserves_wall_clock_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source_content_spec.json")
            Path(source).write_text(json.dumps({
                "fps": 30, "scenes": [
                    {"id": "s1", "kind": "section",
                     "durationInFrames": 90, "props": {"title": "One"}},
                    {"id": "s2", "kind": "section",
                     "durationInFrames": 45, "props": {"title": "Two"}},
                ],
            }), encoding="utf-8")
            fusion = {
                "content_spec_source_path": source,
                "fps": 15,
                "brandPrimary": "#000000",
                "fusion": {"kind": "split", "layout": "side_by_side",
                           "ratio": 0.58, "gap": 24, "mix_audio": True},
                "edl": {"shots": [], "total_duration": 4.5},
            }
            written = video_use_bridge.write_plan_files(
                {}, fusion, os.path.join(tmp, "contracts"))
            compiled = json.loads(Path(
                written["files"]["content_spec"]).read_text(encoding="utf-8"))
            self.assertEqual(compiled["fps"], 15)
            self.assertEqual(
                [item["durationInFrames"] for item in compiled["scenes"]],
                [45, 22])

    def test_execution_preview_render_cache_checks_spec_and_output_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = os.path.join(tmp, "preview.bin")
            calls = []

            def render():
                calls.append(True)
                Path(output).write_bytes(b"first")

            fingerprint = explainer_layout_preview._render_fingerprint(
                {"scenes": [{"id": "s1"}]}, quality="draft",
                renderer="unit", composition="test",
                renderer_source_sha="source-v1")
            self.assertFalse(explainer_layout_preview._run_cached_render(
                "unit", output, fingerprint, render))
            self.assertTrue(explainer_layout_preview._run_cached_render(
                "unit", output, fingerprint, render))
            self.assertEqual(len(calls), 1)
            Path(output).write_bytes(b"tampered")
            self.assertFalse(explainer_layout_preview._run_cached_render(
                "unit", output, fingerprint, render))
            self.assertEqual(len(calls), 2)

    def test_board_stage_tracks_only_stable_customer_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {}
            for name in (
                    "storyboard_result.json", "cast_board.jpg", "storyboard_index.md",
                    "storyboard_preview.html", "storyboard_plan_audit.json"):
                path = os.path.join(tmp, name)
                Path(path).write_bytes(name.encode("utf-8"))
                paths[name] = path
            results = {
                "cast_board": {"path": paths["cast_board.jpg"]},
                "index_md": paths["storyboard_index.md"],
                "preview_html": paths["storyboard_preview.html"],
                "plan_audit_json": paths["storyboard_plan_audit.json"],
            }
            tracked = storyboard._result_stage_artifact_paths(
                results, paths["storyboard_result.json"], "cast_board")
            self.assertEqual(tracked, [os.path.abspath(paths["cast_board.jpg"])])

    def test_legacy_board_approval_repair_drops_mutable_checkpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = os.path.join(tmp, "cast_board.jpg")
            result = os.path.join(tmp, "storyboard_result.json")
            audit = os.path.join(tmp, "storyboard_plan_audit.json")
            marker_path = os.path.join(tmp, ".cast_confirmed.json")
            Path(board).write_bytes(b"confirmed-board")
            Path(result).write_text("{}", encoding="utf-8")
            Path(audit).write_text("old-audit", encoding="utf-8")
            board_sha = run_manifest.file_record(board)["sha256"]
            marker = {
                "status": "confirmed", "kind": "cast",
                "path": board, "board_sha256": board_sha,
                "confirmed_by_customer": True,
                "customer_confirmation": "满意，确认",
                "confirmation_source": "customer_dialogue",
            }
            Path(marker_path).write_text(
                json.dumps(marker, ensure_ascii=False), encoding="utf-8")
            manifest = run_manifest.create_manifest("unit_client", "stable_board_r1")
            manifest["approvals"].update({"script": True, "cast_board": True})
            manifest.setdefault("approval_hashes", {})["script"] = "approved-script"
            manifest.setdefault("generation", {})["cast_board"] = {
                "status": "approved",
                "outputs": [result, board, audit, marker_path],
                "artifacts": [run_manifest.file_record(path) for path in
                              (result, board, audit, marker_path)],
            }
            manifest["approval_evidence"] = {
                "cast_board": {
                    "confirmed_by_customer": True,
                    "customer_confirmation": "满意，确认",
                    "confirmation_source": "customer_dialogue",
                    "approval_path": marker_path,
                    "artifact_sha256": board_sha,
                }
            }
            real_current = run_manifest.approval_is_current

            def current_with_script(current_manifest, stage):
                if stage == "script":
                    return True
                return real_current(current_manifest, stage)

            with mock.patch.object(
                    run_manifest, "approval_is_current",
                    side_effect=current_with_script):
                repaired = run_manifest.repair_visual_board_approval(
                    manifest, "cast_board")
            tracked = [item["path"] for item in
                       manifest["generation"]["cast_board"]["artifacts"]]
            self.assertEqual(tracked, [os.path.abspath(board),
                                       os.path.abspath(marker_path)])
            self.assertTrue(repaired["approval_current"])
            Path(audit).write_text("new-audit", encoding="utf-8")
            with mock.patch.object(
                    run_manifest, "approval_is_current",
                    side_effect=current_with_script):
                self.assertTrue(real_current(manifest, "cast_board"))

    def test_binding_plan_reopens_script_and_derives_document_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            plan_path = os.path.join(tmp, "storyboard_plan.json")
            Path(script_path).write_text("{}", encoding="utf-8")
            Path(plan_path).write_text(json.dumps({
                "client": "unit_client", "run_id": "bind_plan_r1",
                "package": "explainer", "chain_profile": "document_explainer",
                "storyboard_role": "design_board",
                "characters": [{"id": "host", "appearance": "presenter"}],
                "shots": [{"id": "s1", "duration": 5,
                           "panel_plan": ["execution frame"]}],
            }), encoding="utf-8")
            manifest = run_manifest.create_manifest("unit_client", "bind_plan_r1")
            manifest.setdefault("generation", {})["script"] = {
                "status": "approved", "outputs": [script_path],
                "artifacts": [run_manifest.file_record(script_path)],
            }
            manifest["approvals"]["script"] = True
            manifest.setdefault("approval_hashes", {})["script"] = "legacy-script"
            result = run_manifest.bind_storyboard_plan(manifest, plan_path)
            self.assertTrue(result["script_reapproval_required"])
            self.assertFalse(manifest["approvals"]["script"])
            self.assertEqual(manifest["generation"]["script"]["status"],
                             "pending_approval")
            self.assertTrue(manifest["requires_content_scaffold"])
            self.assertTrue(manifest["requires_motion_design"])
            self.assertTrue(manifest["requires_styleframe"])
            self.assertTrue(manifest["requires_audio"])
            self.assertEqual(manifest["chain_profile"]["profile"],
                             "document_explainer")

    def test_script_finish_requires_and_tracks_bound_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "director_plan.json")
            plan_path = os.path.join(tmp, "storyboard_plan.json")
            Path(script_path).write_text("{}", encoding="utf-8")
            Path(plan_path).write_text(json.dumps({
                "client": "unit_client", "run_id": "script_finish_r1",
                "package": "oral-broadcast", "chain_profile": "oral_broadcast",
                "shots": [{"id": "s1", "duration": 3,
                           "panel_plan": ["a", "b", "c", "d"]}],
            }), encoding="utf-8")
            manifest = run_manifest.create_manifest(
                "unit_client", "script_finish_r1")
            manifest.setdefault("generation", {})["script"] = {
                "status": "running"}
            with self.assertRaisesRegex(ValueError, "SCRIPT_PLAN_BINDING_REQUIRED"):
                run_manifest.mark_generation_finished(
                    manifest, "script", [script_path])
            run_manifest.bind_storyboard_plan(manifest, plan_path)
            run_manifest.mark_generation_finished(
                manifest, "script", [script_path])
            tracked = manifest["generation"]["script"]["outputs"]
            self.assertIn(os.path.abspath(plan_path), tracked)

    def test_stage_artifact_paths_cannot_be_reused_by_downstream_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            shared = os.path.join(tmp, "content_spec.json")
            Path(shared).write_text("{}", encoding="utf-8")
            manifest = run_manifest.create_manifest(
                "unit_client", "ownership_r1")
            manifest.setdefault("generation", {})["script"] = {
                "status": "pending_approval", "outputs": [shared],
                "artifacts": [run_manifest.file_record(shared)],
            }
            manifest["generation"]["content_scaffold"] = {"status": "running"}
            with self.assertRaisesRegex(
                    ValueError, "STAGE_ARTIFACT_OWNERSHIP_CONFLICT"):
                run_manifest.mark_generation_finished(
                    manifest, "content_scaffold", [shared])

    def test_plan_binding_rejects_invalid_authored_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = os.path.join(tmp, "storyboard_plan.json")
            Path(plan_path).write_text(json.dumps({
                "client": "unit_client", "run_id": "invalid_plan_r1",
                "shots": [{"id": "s1", "duration": 3}],
            }), encoding="utf-8")
            manifest = run_manifest.create_manifest(
                "unit_client", "invalid_plan_r1")
            with self.assertRaisesRegex(ValueError, "PLAN_VALIDATION_FAILED"):
                run_manifest.bind_storyboard_plan(manifest, plan_path)

    def test_execution_plan_cannot_replace_customer_authored_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = os.path.join(tmp, "storyboard_execution_plan.json")
            Path(plan_path).write_text(json.dumps({
                "client": "unit_client", "run_id": "execution_plan_r1",
                "_execution_plan": {"kind": "motion_enriched_storyboard_plan"},
                "shots": [{"id": "s1", "duration": 3,
                           "panel_plan": ["execution frame"]}],
            }), encoding="utf-8")
            manifest = run_manifest.create_manifest(
                "unit_client", "execution_plan_r1")
            with self.assertRaisesRegex(
                    ValueError, "EXECUTION_PLAN_CANNOT_REPLACE_AUTHORED_PLAN"):
                run_manifest.bind_storyboard_plan(manifest, plan_path)

    def test_formal_execution_preview_requires_motion_enriched_plan(self):
        with self.assertRaisesRegex(
                ValueError, "FORMAL_EXECUTION_PLAN_INCOMPLETE"):
            explainer_layout_preview._validate_formal_execution_plan({
                "shots": [{"id": "s1", "duration": 3}],
            })

    def test_design_board_motion_injection_cannot_modify_authored_plan_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = os.path.join(tmp, "storyboard_plan.json")
            design_path = os.path.join(tmp, "motion_design.json")
            shots = [{"id": "s1", "duration": 3,
                      "panel_plan": ["execution frame"],
                      "dialogue": "测试讲解"}]
            plan = {
                "client": "unit_client", "run_id": "motion_inject_r1",
                "storyboard_role": "design_board", "shots": shots,
            }
            Path(plan_path).write_text(json.dumps(plan), encoding="utf-8")
            design = motion_design._rule_based_design(
                {"storyboard_role": "design_board"}, shots)
            Path(design_path).write_text(json.dumps(design), encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError, "DESIGN_BOARD_EXECUTION_PLAN_OUT_REQUIRED"):
                motion_design.main([
                    "inject", "--plan", plan_path,
                    "--design", design_path,
                ])

    def test_router_fails_closed_when_approved_script_has_no_bound_plan(self):
        manifest = run_manifest.create_manifest("unit_client", "router_r1")
        with mock.patch.object(
                workflow_router.rm, "approval_is_current",
                side_effect=lambda current_manifest, stage: stage in {"brief", "script"}):
            route = workflow_router.route_workflow(manifest=manifest)
        self.assertEqual(route["current_stage"], "script")
        self.assertEqual(route["next_action"], "bind_storyboard_plan")

    def test_prompt_review_fingerprints_match_generation_single_pass(self):
        plan = {
            "client": "unit_client", "storyboard_aspect_ratio": "16:9",
            "output_ratio": "16:9",
            "shots": [{"id": "s1", "duration": 12,
                       "panel_plan": ["a", "b", "c", "d"],
                       "dialogue": "这是一段会被按模型时长拆分的测试台词。"}],
        }
        self.assertEqual(prompt_review.plan_fingerprint(plan),
                         storyboard.plan_fingerprint(plan))
        self.assertEqual(prompt_review.visual_plan_fingerprint(plan),
                         storyboard.visual_plan_fingerprint(plan))

    def test_design_board_default_is_one_execution_frame(self):
        plan = {"storyboard_role": "design_board",
                "shots": [{"id": "s1", "duration": 5}]}
        storyboard._enrich_storyboard_contract(plan)
        self.assertEqual(len(plan["shots"][0]["panel_plan"]), 1)

    def test_design_board_contract_kind_selects_deterministic_explainer(self):
        plan = {
            "project_title": "产品介绍",
            "storyboard_contract": {
                "kind": "design_board",
                "video_model_usage": "do_not_submit_as_storyboard_ref",
            },
            "shots": [{"id": "s1", "duration": 3}],
        }
        self.assertTrue(video_use_bridge._is_document_explainer_plan(plan))
        self.assertFalse(video_use_bridge._is_oral_broadcast_plan(
            dict(plan, storyboard_role="design_board")))

    def test_prompt_polish_runs_independent_shots_concurrently(self):
        plan = {
            "client": "unit_client", "storyboard_aspect_ratio": "16:9",
            "output_ratio": "16:9",
            "shots": [{"id": "s%d" % index, "duration": 3,
                       "panel_plan": ["a", "b", "c", "d"],
                       "visual": "clean scene"}
                      for index in range(1, 5)],
        }
        active = 0
        maximum = 0
        lock = threading.Lock()
        release = threading.Event()

        def fake_chat(*args, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if maximum >= 2:
                    release.set()
            release.wait(timeout=1)
            with lock:
                active -= 1
            return json.dumps({
                "prompt_zh": "clean scene",
                "continuity_notes": [],
                "negative_prompt_zh": "no text",
            })

        with mock.patch.object(prompt_review.key_setup, "ensure_session_id"), \
                mock.patch.object(prompt_review.br_client, "chat",
                                  side_effect=fake_chat):
            review = prompt_review.polish(
                plan, "storyboard", api_key="test-key", model="qwen3.6-plus")
        self.assertGreaterEqual(maximum, 2)
        self.assertEqual([item["shot_id"] for item in review["prompts"]],
                         ["s1", "s2", "s3", "s4"])

    def test_confirmed_prompt_review_can_be_invalidated_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "review.json")
            Path(path).write_text(json.dumps({
                "status": "confirmed", "stage": "storyboard",
                "customer_confirmation": "确认",
                "confirmation_source": "customer_dialogue",
                "prompts": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(
                    ValueError,
                    "PROMPT_REVIEW_CONFIRMED_INVALIDATION_REASON_REQUIRED"):
                prompt_review.invalidate(path)
            review = prompt_review.invalidate(
                path, reason="plan fingerprint changed")
            self.assertEqual(review["status"], "invalidated")
            self.assertEqual(review["previous_status"], "confirmed")
            self.assertEqual(review["customer_confirmation"], "确认")


if __name__ == "__main__":
    unittest.main()
