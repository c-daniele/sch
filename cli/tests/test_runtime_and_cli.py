"""Unit tests for `sch.runtime` (payload builders, injection-safety,
invocation-result parsing) and `sch.cli` (`--timeout` validation), plus
the `status --live` merge/render logic in `sch.commands.status`.
"""

import contextlib
import datetime
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import cli as cli_mod
from sch import procs
from sch import runtime
from sch.commands import run as run_cmd
from sch.commands import status as status_cmd
from sch.commands import task as task_cmd


class PayloadBuilderTests(unittest.TestCase):
    def test_task_payload_roundtrips_hostile_prompt_byte_for_byte(self):
        hostile = '"quote", $var, `backtick`, ; rm -rf /\nnewline\ttab'
        payload = runtime.payload_task("ws", "opencode", hostile, True, 30)
        data = json.loads(payload)
        self.assertEqual(data["prompt"], hostile)
        self.assertEqual(data["workspace"], "ws")
        self.assertEqual(data["harness"], "opencode")
        self.assertIs(data["continue"], True)
        self.assertEqual(data["timeout_s"], 30)
        self.assertIsInstance(data["timeout_s"], int)

    def test_task_payload_omits_timeout_when_not_given(self):
        payload = runtime.payload_task("ws", "claude", "hi", False)
        data = json.loads(payload)
        self.assertNotIn("timeout_s", data)

    def test_task_payload_includes_model_when_given(self):
        data = json.loads(runtime.payload_task(
            "ws", "opencode", "hi", False, model="provider/model-id:1"
        ))
        self.assertEqual(data["model"], "provider/model-id:1")

    def test_task_payload_omits_empty_model(self):
        data = json.loads(runtime.payload_task("ws", "claude", "hi", False, model=""))
        self.assertNotIn("model", data)

    def test_task_payload_without_model_is_byte_identical_to_legacy_shape(self):
        with_default = runtime.payload_task("ws", "opencode", "hi", True, 30, "s3", 2)
        explicit_empty = runtime.payload_task(
            "ws", "opencode", "hi", True, 30, "s3", 2, model=""
        )
        self.assertEqual(with_default, explicit_empty)
        self.assertNotIn("model", json.loads(with_default))

    def test_task_payload_continue_false(self):
        payload = runtime.payload_task("ws", "claude", "hi", False)
        self.assertIs(json.loads(payload)["continue"], False)

    def test_storage_backend_is_propagated(self):
        data = json.loads(runtime.payload_task(
            "ws", "opencode", "hi", False, storage_backend="s3"
        ))
        self.assertEqual(data["storage_backend"], "s3")

        noop = json.loads(runtime.payload_noop("ws", "opencode", "fresh", "session"))
        self.assertEqual(noop["storage_backend"], "session")

        builders = (
            runtime.payload_mark_interactive("ws", "opencode", True, "s3"),
            runtime.payload_prepare_run("ws", "opencode", "s3"),
            runtime.payload_checkpoint("ws", "opencode", "s3"),
            runtime.payload_info("ws", "opencode", "s3"),
            runtime.payload_serve_ensure("ws", "opencode", "s3"),
        )
        for payload in builders:
            self.assertEqual(json.loads(payload)["storage_backend"], "s3")

    def test_noop_payload_shape(self):
        payload = runtime.payload_noop("ws", "opencode", "fresh")
        self.assertEqual(
            json.loads(payload),
            {
                "action": "noop",
                "storage": "fresh",
                "workspace": "ws",
                "harness": "opencode",
            },
        )

    def test_mark_interactive_payload_shape(self):
        payload = runtime.payload_mark_interactive("ws", "claude", False)
        data = json.loads(payload)
        self.assertEqual(data["action"], "mark-interactive")
        self.assertIs(data["active"], False)

    def test_info_payload_shape(self):
        self.assertEqual(json.loads(runtime.payload_info()), {"action": "info"})

    def test_prepare_run_and_checkpoint_and_serve_ensure_shapes(self):
        self.assertEqual(
            json.loads(runtime.payload_prepare_run("ws", "opencode")),
            {"action": "prepare-run", "workspace": "ws", "harness": "opencode"},
        )
        self.assertEqual(
            json.loads(runtime.payload_checkpoint("ws", "claude")),
            {"action": "checkpoint", "workspace": "ws", "harness": "claude"},
        )
        self.assertEqual(
            json.loads(runtime.payload_serve_ensure("ws", "opencode")),
            {"action": "serve-ensure", "workspace": "ws", "harness": "opencode"},
        )

    def test_prepare_run_payload_includes_model_when_given(self):
        self.assertEqual(
            json.loads(runtime.payload_prepare_run(
                "ws", "opencode", "s3", 4, model="provider/model-id:1"
            )),
            {
                "action": "prepare-run",
                "workspace": "ws",
                "harness": "opencode",
                "model": "provider/model-id:1",
                "storage_backend": "s3",
                "session_epoch": 4,
            },
        )

    def test_prepare_run_payload_omits_empty_model(self):
        data = json.loads(runtime.payload_prepare_run("ws", "claude", model=""))
        self.assertNotIn("model", data)

    def test_prepare_run_payload_omits_continue_by_default(self):
        data = json.loads(runtime.payload_prepare_run("ws", "opencode"))
        self.assertNotIn("continue", data)

    def test_prepare_run_payload_includes_continue_when_requested(self):
        data = json.loads(runtime.payload_prepare_run(
            "ws", "opencode", continue_flag=True
        ))
        self.assertIs(data["continue"], True)

    def test_prepare_run_payload_continue_composes_with_model(self):
        data = json.loads(runtime.payload_prepare_run(
            "ws", "claude", "s3", 2, model="m/m", continue_flag=True
        ))
        self.assertIs(data["continue"], True)
        self.assertEqual(data["model"], "m/m")

    def test_payload_never_breaks_out_of_json_string_context(self):
        hostile_ws = 'a"; rm -rf /; echo "'
        payload = runtime.payload_noop(hostile_ws, "opencode", "fresh")
        # A single json.loads() must fully consume the payload with no
        # leftover trailing content — proof there is no injected structure.
        data = json.loads(payload)
        self.assertEqual(data["workspace"], hostile_ws)


class InvocationResultTests(unittest.TestCase):
    def test_get_returns_default_on_invalid_json(self):
        result = runtime.InvocationResult(ok=True, raw_text="not json")
        self.assertEqual(result.get("status", "unknown"), "unknown")

    def test_get_returns_default_on_empty_text(self):
        result = runtime.InvocationResult(ok=False, raw_text="")
        self.assertEqual(result.get("status", "unknown"), "unknown")

    def test_get_returns_present_value(self):
        result = runtime.InvocationResult(
            ok=True, raw_text=json.dumps({"status": "ok", "task_id": "t-1"})
        )
        self.assertEqual(result.get("status"), "ok")
        self.assertEqual(result.get("task_id"), "t-1")

    def test_get_treats_none_value_as_default(self):
        result = runtime.InvocationResult(ok=True, raw_text=json.dumps({"warning": None}))
        self.assertEqual(result.get("warning", ""), "")

    def test_get_on_non_dict_json_returns_default(self):
        result = runtime.InvocationResult(ok=True, raw_text=json.dumps([1, 2, 3]))
        self.assertEqual(result.get("status", "unknown"), "unknown")

    def test_storage_verification_accepts_matching_acknowledgment(self):
        result = runtime.InvocationResult(
            ok=True, raw_text=json.dumps({"status": "ok", "storage": "s3"})
        )
        self.assertEqual(runtime.storage_verification_error(result, "s3"), "")

    def test_storage_verification_distinguishes_invocation_failure(self):
        result = runtime.InvocationResult(ok=False, raw_text="")
        self.assertIn(
            "warmup invocation failed",
            runtime.storage_verification_error(result, "s3"),
        )

    def test_storage_verification_reports_runtime_rejection(self):
        result = runtime.InvocationResult(
            ok=True,
            raw_text=json.dumps({
                "status": "rejected",
                "error": "storage backend mismatch",
            }),
        )
        error = runtime.storage_verification_error(result, "s3")
        self.assertIn("status='rejected'", error)
        self.assertIn("storage backend mismatch", error)

    def test_storage_verification_reports_missing_acknowledgment(self):
        result = runtime.InvocationResult(
            ok=True, raw_text=json.dumps({"status": "ok"})
        )
        self.assertIn(
            "deploy the matching runtime image",
            runtime.storage_verification_error(result, "s3"),
        )

    def test_storage_verification_reports_wrong_backend(self):
        result = runtime.InvocationResult(
            ok=True, raw_text=json.dumps({"status": "ok", "storage": "session"})
        )
        error = runtime.storage_verification_error(result, "s3")
        self.assertIn("storage='session'", error)
        self.assertIn("expected storage='s3'", error)


class InvokeVerifiedArgvTests(unittest.TestCase):
    """The aws CLI argv built by invoke_verified (TASK-29: optional
    --cli-read-timeout for actions the shim may hold open past the aws CLI's
    60 s default, e.g. prepare-run with --continue on a cold boot)."""

    def _capture_argv(self, **kwargs):
        cfg = type("Config", (), {"region": "eu-west-1", "provider_keys": None})()
        calls = []

        def fake_run(argv, **_run_kwargs):
            calls.append(list(argv))
            return type("Completed", (), {"returncode": 0})()

        with patch.object(runtime, "runtime_arn", return_value="arn:runtime"), \
             patch.object(runtime.subprocess, "run", side_effect=fake_run):
            result = runtime.invoke_verified(cfg, "sid", '{"action": "noop"}', "op", **kwargs)
        self.assertTrue(result.ok)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_default_argv_has_no_read_timeout_and_ends_with_output_path(self):
        argv = self._capture_argv()
        self.assertEqual(argv[:3], ["aws", "bedrock-agentcore", "invoke-agent-runtime"])
        self.assertNotIn("--cli-read-timeout", argv)
        self.assertEqual(argv[argv.index("--region") + 1], "eu-west-1")
        # The positional outfile stays last: aws requires it after the options.
        self.assertNotEqual(argv[-1], "eu-west-1")
        self.assertFalse(argv[-1].startswith("--"))

    def test_read_timeout_is_passed_as_a_global_option_before_the_output_path(self):
        argv = self._capture_argv(read_timeout_s=300)
        index = argv.index("--cli-read-timeout")
        self.assertEqual(argv[index + 1], "300")
        self.assertEqual(index + 2, len(argv) - 1, "outfile must remain the last argument")

    def test_read_timeout_is_normalised_to_an_integer_string(self):
        argv = self._capture_argv(read_timeout_s=299.9)
        self.assertEqual(argv[argv.index("--cli-read-timeout") + 1], "299")


class TimeoutValidationTests(unittest.TestCase):
    def test_valid_integer_string(self):
        self.assertEqual(cli_mod.parse_int_or_die("42", "--timeout"), 42)

    def test_valid_negative_integer_string(self):
        self.assertEqual(cli_mod.parse_int_or_die("-5", "--timeout"), -5)

    def test_non_numeric_string_dies(self):
        with self.assertRaises(SystemExit):
            cli_mod.parse_int_or_die("abc", "--timeout")

    def test_float_string_dies(self):
        with self.assertRaises(SystemExit):
            cli_mod.parse_int_or_die("3.5", "--timeout")

    def test_empty_string_dies(self):
        with self.assertRaises(SystemExit):
            cli_mod.parse_int_or_die("", "--timeout")


class HelpTextTests(unittest.TestCase):
    def test_lists_dashboard_and_interval(self):
        self.assertIn("sch dashboard [--interval <seconds>]", cli_mod.USAGE_TEXT)

    def test_global_help_lists_run_model_flag(self):
        self.assertIn(
            "sch run   <workspace> [--harness <opencode|claude|pi>] [--model <id>]",
            cli_mod.USAGE_TEXT,
        )

    def test_global_help_lists_task_model_flag(self):
        self.assertIn(
            "sch task <workspace> [--harness <opencode|claude|pi>] [--model <id>]",
            cli_mod.USAGE_TEXT,
        )

    def test_explains_workspace_session_mapping_storage(self):
        self.assertIn("SCH generates runtimeSessionId; AWS does not assign it", cli_mod.USAGE_TEXT)
        self.assertIn("~/.config/sch/workspaces", cli_mod.USAGE_TEXT)
        self.assertIn("not shared", cli_mod.USAGE_TEXT)
        self.assertIn("DynamoDB-backed API is authoritative", cli_mod.USAGE_TEXT)

    def test_default_harness_is_opencode(self):
        self.assertIn(
            "SCH_DEFAULT_HARNESS (default: opencode", cli_mod.USAGE_TEXT
        )


class RunArgumentTests(unittest.TestCase):
    def test_parses_valid_model(self):
        ws, harness, model, branch, cont, options = run_cmd._parse_args([
            "ws", "--harness", "claude", "--model",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        ])
        self.assertEqual(ws, "ws")
        self.assertEqual(harness, "claude")
        self.assertEqual(model, "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
        self.assertIs(cont, False)
        self.assertEqual(options["sync"], "")

    def test_continue_defaults_to_false_and_parses(self):
        ws, harness, model, branch, cont, options = run_cmd._parse_args(
            ["ws", "--continue"]
        )
        self.assertIs(cont, True)
        self.assertEqual(run_cmd._parse_args(["ws"])[4], False)

    def test_continue_parses_in_either_order_with_model(self):
        for args in (
            ["ws", "--continue", "--model", "provider/model-id"],
            ["ws", "--model", "provider/model-id", "--continue"],
        ):
            with self.subTest(args=args):
                ws, harness, model, branch, cont, options = run_cmd._parse_args(args)
                self.assertEqual((ws, model), ("ws", "provider/model-id"))
                self.assertIs(cont, True)

    def test_model_is_optional(self):
        self.assertEqual(run_cmd._parse_args(["ws"])[2], "")

    def test_model_and_sync_options_parse_in_either_order(self):
        for args in (
            ["ws", "--model", "provider/model-id", "--sync", "/project"],
            ["ws", "--sync", "/project", "--model", "provider/model-id"],
        ):
            with self.subTest(args=args):
                ws, harness, model, branch, cont, options = run_cmd._parse_args(args)
                self.assertEqual((ws, harness, model), ("ws", "", "provider/model-id"))
                self.assertEqual(options["sync"], "/project")

    def test_rejects_missing_model_value(self):
        with self.assertRaises(SystemExit):
            run_cmd._parse_args(["ws", "--model"])

    def test_rejects_empty_model(self):
        with self.assertRaises(SystemExit):
            run_cmd._parse_args(["ws", "--model", ""])

    def test_rejects_model_with_spaces(self):
        with self.assertRaises(SystemExit):
            run_cmd._parse_args(["ws", "--model", "provider/model id"])

    def test_rejects_characters_outside_allowlist(self):
        for model in ("model@version", "model?query", "model\\name", "model\nname"):
            with self.subTest(model=model):
                with self.assertRaises(SystemExit):
                    run_cmd._parse_args(["ws", "--model", model])

    def test_local_help_lists_model_and_exits_successfully(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as raised:
                run_cmd._parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(
            out.getvalue(),
            "usage: sch run <workspace> [--harness <opencode|claude|pi>] "
            "[--model <id>] [--branch <name>] [--continue]\n",
        )

class SharedModelValidationTests(unittest.TestCase):
    """The client-side rule is a single implementation shared by run and
    task (spec: run-model-selection, "Regola condivisa tra run e task")."""

    def test_valid_model_is_returned(self):
        self.assertEqual(
            cli_mod.validate_model_or_die("provider/model-id:1"),
            "provider/model-id:1",
        )

    def test_empty_model_dies(self):
        with self.assertRaises(SystemExit):
            cli_mod.validate_model_or_die("")

    def test_out_of_allowlist_model_dies(self):
        for model in ("model with spaces", "model@version", "model?query"):
            with self.subTest(model=model):
                with self.assertRaises(SystemExit):
                    cli_mod.validate_model_or_die(model)

    def test_run_and_task_share_the_helper(self):
        with patch.object(cli_mod, "validate_model_or_die", return_value="m") as helper:
            run_cmd._parse_args(["ws", "--model", "m"])
            task_cmd._parse_args(["ws", "--model", "m", "prompt"])
        self.assertEqual(helper.call_count, 2)


class TaskArgumentTests(unittest.TestCase):
    def test_parses_valid_model(self):
        ws, harness, model, branch, cont, timeout, prompt, options, handoff, handoff_session, sanitize = task_cmd._parse_args([
            "ws", "--harness", "claude", "--model",
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0", "build and test",
        ])
        self.assertEqual(ws, "ws")
        self.assertEqual(harness, "claude")
        self.assertEqual(model, "eu.anthropic.claude-haiku-4-5-20251001-v1:0")
        self.assertEqual(prompt, "build and test")
        self.assertFalse(handoff)

    def test_model_is_optional(self):
        result = task_cmd._parse_args(["ws", "build"])
        self.assertEqual(result[2], "")

    def test_rejects_missing_model_value(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--model"])

    def test_rejects_empty_model(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--model", "", "build"])

    def test_rejects_model_with_spaces(self):
        with self.assertRaises(SystemExit):
            task_cmd._parse_args(["ws", "--model", "provider/model id", "build"])

    def test_rejects_characters_outside_allowlist(self):
        for model in ("model@version", "model?query", "model\\name", "model\nname"):
            with self.subTest(model=model):
                with self.assertRaises(SystemExit):
                    task_cmd._parse_args(["ws", "--model", model, "build"])

    def test_model_after_separator_is_prompt_text(self):
        # Existing `--` contract: everything after it is prompt text, so
        # `--model x` becomes prompt words and no model is selected.
        ws, harness, model, branch, cont, timeout, prompt, options, handoff, handoff_session, sanitize = task_cmd._parse_args(
            ["ws", "--", "--model", "x"]
        )
        self.assertEqual(model, "")
        self.assertEqual(prompt, "--model x")

    def test_model_before_separator_with_prompt_after(self):
        result = task_cmd._parse_args(
            ["ws", "--model", "provider/model-id", "--", "build", "the", "app"]
        )
        self.assertEqual(result[2], "provider/model-id")
        self.assertEqual(result[6], "build the app")

    def test_model_combines_with_continue_and_timeout(self):
        ws, harness, model, branch, cont, timeout, prompt, options, handoff, handoff_session, sanitize = task_cmd._parse_args([
            "ws", "--continue", "--model", "provider/model-id",
            "--timeout", "120", "proceed",
        ])
        self.assertEqual(model, "provider/model-id")
        self.assertTrue(cont)
        self.assertEqual(timeout, "120")
        self.assertEqual(prompt, "proceed")


class TaskModelSubmissionTests(unittest.TestCase):
    """cmd_task propagates --model into the payload and warns when the ack
    does not echo it (design D4 of add-task-model-flag)."""

    def setUp(self):
        self.cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        self.resolved = type(
            "Resolved", (), {
                "sid": "sid", "harness": "opencode", "identity": "ws",
                "storage": "s3", "epoch": 1, "was_created": False,
            }
        )()

    def _submit(self, args, ack):
        responses = [
            runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"})),
            runtime.InvocationResult(True, json.dumps(ack)),
        ]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(task_cmd.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(task_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(task_cmd.runtime, "invoke_verified", side_effect=responses) as invoke, \
             patch.object(task_cmd.workspace, "mark_status"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = task_cmd.cmd_task(self.cfg, args)
        return code, out, err, invoke

    def test_model_reaches_task_payload(self):
        code, out, err, invoke = self._submit(
            ["ws", "--model", "provider/model-id", "build"],
            {"status": "accepted", "task_id": "t-1", "model": "provider/model-id"},
        )
        self.assertEqual(code, 0)
        task_payload = json.loads(invoke.call_args_list[1].args[2])
        self.assertEqual(task_payload["model"], "provider/model-id")
        self.assertEqual(out.getvalue().strip(), "t-1")
        self.assertNotIn("did not echo requested model", err.getvalue())

    def test_payload_has_no_model_field_without_flag(self):
        code, out, err, invoke = self._submit(
            ["ws", "build"], {"status": "accepted", "task_id": "t-2"},
        )
        self.assertEqual(code, 0)
        task_payload = json.loads(invoke.call_args_list[1].args[2])
        self.assertNotIn("model", task_payload)
        self.assertNotIn("did not echo requested model", err.getvalue())

    def test_missing_model_echo_warns_but_prints_task_id(self):
        code, out, err, _ = self._submit(
            ["ws", "--model", "provider/model-id", "build"],
            {"status": "accepted", "task_id": "t-3"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "t-3")
        self.assertIn(
            "runtime did not echo requested model 'provider/model-id'",
            err.getvalue(),
        )
        self.assertIn("default model", err.getvalue())

    def test_mismatched_model_echo_warns_but_prints_task_id(self):
        code, out, err, _ = self._submit(
            ["ws", "--model", "provider/model-id", "build"],
            {"status": "accepted", "task_id": "t-4", "model": "different/model"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "t-4")
        self.assertIn("did not echo requested model", err.getvalue())


class RequiredCommandTests(unittest.TestCase):
    def test_missing_command_shows_install_hint(self):
        err = io.StringIO()
        with patch.object(procs.shutil, "which", return_value=None):
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as raised:
                    procs.require_command("agentcore", "install it with: npm install -g @aws/agentcore")

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(
            err.getvalue(),
            "sch: 'agentcore' not found on PATH; install it with: npm install -g @aws/agentcore\n",
        )


class ForegroundHandoffTests(unittest.TestCase):
    class FakeStdin:
        def isatty(self):
            return True

        def fileno(self):
            return 7

    def _run(self, child_effect):
        saved = [1, 2, 3]
        child_kwargs = (
            {"side_effect": child_effect}
            if isinstance(child_effect, BaseException)
            else {"return_value": child_effect}
        )
        with patch.object(procs.os, "name", "posix"), \
             patch.object(procs.sys, "stdin", self.FakeStdin()), \
             patch("termios.tcgetattr", return_value=saved) as get_attr, \
             patch("termios.tcsetattr") as set_attr, \
             patch.object(procs.subprocess, "call", **child_kwargs) as call:
            if isinstance(child_effect, BaseException):
                with self.assertRaises(type(child_effect)):
                    procs.foreground_handoff(["child", "arg"])
                result = None
            else:
                result = procs.foreground_handoff(["child", "arg"])
        get_attr.assert_called_once_with(7)
        set_attr.assert_called_once_with(7, ANY, saved)
        call.assert_called_once_with(["child", "arg"])
        return result

    def test_restores_terminal_after_success(self):
        self.assertEqual(self._run(0), 0)

    def test_restores_terminal_after_nonzero_exit(self):
        self.assertEqual(self._run(17), 17)

    def test_restores_terminal_after_child_exception(self):
        self._run(OSError("boom"))


class SyncSupervisorTests(unittest.TestCase):
    def test_helper_ready_gates_child_and_returns_its_exit_code(self):
        helper = [
            sys.executable,
            "-c",
            "import json, signal, time; print(json.dumps({'type':'ready'}), flush=True); signal.signal(signal.SIGTERM, lambda *_: exit(0)); time.sleep(30)",
        ]
        child = [sys.executable, "-c", "raise SystemExit(7)"]
        self.assertEqual(procs.supervise_interactive(helper, child), 7)

    def test_helper_error_prevents_child(self):
        helper = [sys.executable, "-c", "import json; print(json.dumps({'type':'error','message':'lease held'}))"]
        with self.assertRaisesRegex(procs.SyncStartError, "lease held"):
            procs.supervise_interactive(helper, [sys.executable, "-c", "raise SystemExit(99)"])

    def test_helper_exit_after_ready_stops_local_client(self):
        helper = [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'type':'ready'}), flush=True); raise SystemExit(4)",
        ]
        child = [sys.executable, "-c", "import time; time.sleep(30)"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(procs.supervise_interactive(helper, child), 1)
        self.assertIn("sync helper exited unexpectedly (status 4)", err.getvalue())

    def test_keyboard_interrupt_releases_helper_without_error(self):
        helper = MagicMock()
        helper.stdout = io.StringIO('{"type":"ready"}\n')
        helper.poll.return_value = None
        child = MagicMock()
        child.poll.return_value = None
        with patch.object(procs.subprocess, "Popen", side_effect=[helper, child]):
            with patch.object(procs.time, "sleep", side_effect=KeyboardInterrupt):
                self.assertEqual(procs.supervise_interactive(["helper"], ["child"]), 130)
        helper.terminate.assert_called_once()
        child.terminate.assert_called_once()

    def test_helper_control_pipe_closes_after_graceful_shutdown(self):
        events = []
        helper = MagicMock()
        helper.stdout = MagicMock()
        helper.stdout.readline.return_value = '{"type":"ready"}\n'
        helper.stdout.close.side_effect = lambda: events.append("stdout-close")
        helper.poll.return_value = None
        helper.terminate.side_effect = lambda: events.append("helper-terminate")
        child = MagicMock()
        child.poll.return_value = 0
        with patch.object(procs.subprocess, "Popen", side_effect=[helper, child]):
            self.assertEqual(procs.supervise_interactive(["helper"], ["child"]), 0)
        self.assertEqual(events, ["helper-terminate", "stdout-close"])

    def test_interactive_child_exit_restores_and_resets_terminal(self):
        helper = MagicMock()
        helper.stdout = io.StringIO('{"type":"ready"}\n')
        helper.poll.return_value = None
        child = MagicMock()
        child.poll.return_value = 0
        with patch.object(procs.subprocess, "Popen", side_effect=[helper, child]), \
             patch.object(procs, "_save_terminal_state", return_value="state"), \
             patch.object(procs, "_restore_terminal_state") as restore, \
             patch.object(procs, "reset_terminal_modes") as reset:
            self.assertEqual(procs.supervise_interactive(["helper"], ["child"]), 0)
        restore.assert_called_once_with("state")
        reset.assert_called_once_with()

    def test_helper_only_path_never_touches_terminal_modes(self):
        helper = MagicMock()
        helper.stdout = io.StringIO('{"type":"ready"}\n')
        helper.poll.return_value = None
        with patch.object(procs.subprocess, "Popen", return_value=helper), \
             patch.object(procs, "reset_terminal_modes") as reset:
            self.assertEqual(procs.supervise_interactive(["helper"]), 0)
        reset.assert_not_called()


class DetachedTaskSyncTests(unittest.TestCase):
    def setUp(self):
        self.cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        self.resolved = type(
            "Resolved", (), {
                "sid": "sid", "harness": "opencode", "identity": "owner/ws",
                "storage": "s3", "epoch": 1, "was_created": False,
            }
        )()

    def test_preflight_barrier_completes_before_submit(self):
        calls = []
        accepted = runtime.InvocationResult(True, json.dumps({"status": "accepted", "task_id": "task-1"}))
        with patch.object(task_cmd.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(task_cmd.sync_mod, "resolve_binding", return_value="/project"), \
             patch.object(task_cmd.sync_mod, "baseline_path", return_value="/baseline"), \
              patch.object(task_cmd.sync_mod, "helper_argv", return_value=["sync-helper"]), \
              patch.object(task_cmd, "runtime_arn", return_value="arn"), \
               patch.object(task_cmd.runtime, "invoke_verified", side_effect=[
                   runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"})),
                   accepted,
               ]), \
               patch.object(task_cmd.runtime, "verify_storage", side_effect=lambda *_: calls.append("warm") or True), \
               patch.object(task_cmd.procs, "supervise_interactive", side_effect=lambda *_: calls.append("barrier")), \
             patch.object(task_cmd.workspace, "mark_status"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(task_cmd.cmd_task(self.cfg, ["ws", "--sync", "/project", "build"]), 0)
        self.assertEqual(calls, ["warm", "barrier"])
        self.assertEqual(out.getvalue().strip(), "task-1")

    def test_failed_preflight_never_submits_task(self):
        with patch.object(task_cmd.harness_mod, "resolve_harness", return_value=self.resolved), \
             patch.object(task_cmd.sync_mod, "resolve_binding", return_value="/project"), \
             patch.object(task_cmd.sync_mod, "baseline_path", return_value="/baseline"), \
              patch.object(task_cmd.sync_mod, "helper_argv", return_value=["sync-helper"]), \
              patch.object(task_cmd, "runtime_arn", return_value="arn"), \
               patch.object(task_cmd.runtime, "invoke_verified", return_value=runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "s3"}))) as submit, \
               patch.object(task_cmd.runtime, "verify_storage", return_value=True), \
               patch.object(task_cmd.procs, "supervise_interactive", side_effect=procs.SyncStartError("barrier failed")), \
              patch.object(task_cmd.workspace, "mark_status"):
            with self.assertRaises(SystemExit):
                task_cmd.cmd_task(self.cfg, ["ws", "--sync", "/project", "build"])
        self.assertEqual(submit.call_count, 1)

    def test_windows_exec_uses_foreground_child_and_propagates_status(self):
        with patch.object(procs.os, "name", "nt"):
            with patch.object(procs.shutil, "which", return_value="agentcore.cmd"):
                with patch.object(procs, "reset_terminal_modes"):
                    with patch.object(procs.subprocess, "call", return_value=23) as call:
                        with self.assertRaises(SystemExit) as raised:
                            procs.exec_or_wait(["agentcore", "exec", "--it"])
        self.assertEqual(raised.exception.code, 23)
        call.assert_called_once_with(["agentcore.cmd", "exec", "--it"])

    def test_no_sync_binding_path_skips_helper(self):
        helper = MagicMock()
        helper.stdout = io.StringIO('{"type":"ready"}\n')
        helper.poll.return_value = None
        with patch.object(procs.subprocess, "Popen", return_value=helper):
            self.assertEqual(procs.supervise_interactive(["helper"]), 0)
        helper.terminate.assert_called_once()


class RunSyncTests(unittest.TestCase):
    def _resolved(self):
        return type(
            "Resolved", (), {
                "sid": "sid", "harness": "opencode", "identity": "ws",
                "storage": "session", "epoch": 0, "was_created": False,
            }
        )()

    def test_no_sync_propagates_model_and_warns_on_missing_echo_but_opens(self):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        responses = [
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "storage": "session",
            })),
            runtime.InvocationResult(True, json.dumps({"status": "ok"})),
        ]
        err = io.StringIO()
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses) as invoke, \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)) as open_shell, \
             patch.object(run_cmd.workspace, "mark_status"), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                run_cmd.cmd_run(cfg, ["ws", "--no-sync", "--model", "provider/model-id"])

        self.assertEqual(raised.exception.code, 0)
        prepare_payload = json.loads(invoke.call_args_list[1].args[2])
        self.assertEqual(prepare_payload["model"], "provider/model-id")
        self.assertIn("runtime did not echo requested model 'provider/model-id'", err.getvalue())
        open_shell.assert_called_once()

    def test_sync_propagates_model_and_warns_on_mismatched_echo_but_opens(self):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        responses = [
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "storage": "session",
            })),
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "model": "different/model",
            })),
        ]

        def supervise(helper, child, after_ready, presence=None):
            after_ready()
            return 9

        err = io.StringIO()
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value="/project"), \
             patch.object(run_cmd.sync_mod, "baseline_path", return_value="/baseline"), \
             patch.object(run_cmd.sync_mod, "helper_argv", return_value=["sync-helper"]), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses) as invoke, \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "supervise_interactive", side_effect=supervise), \
             patch.object(run_cmd.workspace, "mark_status"), \
             contextlib.redirect_stderr(err):
            result = run_cmd.cmd_run(cfg, [
                "ws", "--sync", "/project", "--model", "provider/model-id",
            ])

        self.assertEqual(result, 9)
        prepare_payload = json.loads(invoke.call_args_list[1].args[2])
        self.assertEqual(prepare_payload["model"], "provider/model-id")
        self.assertIn("runtime did not echo requested model 'provider/model-id'", err.getvalue())

    def test_continue_reaches_prepare_payload_and_stays_silent_on_echo(self):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        responses = [
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "storage": "session",
            })),
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "continue": True, "session_id": "ses_1",
            })),
        ]
        err = io.StringIO()
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses) as invoke, \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)), \
             patch.object(run_cmd.workspace, "mark_status"), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                run_cmd.cmd_run(cfg, ["ws", "--no-sync", "--continue"])

        prepare_payload = json.loads(invoke.call_args_list[1].args[2])
        self.assertIs(prepare_payload["continue"], True)
        self.assertNotIn("did not echo requested --continue", err.getvalue())
        self.assertNotIn("no prior", err.getvalue())

    def test_continue_without_echo_warns_but_opens(self):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        responses = [
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "storage": "session",
            })),
            runtime.InvocationResult(True, json.dumps({"status": "ok"})),
        ]
        err = io.StringIO()
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses), \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)) as open_shell, \
             patch.object(run_cmd.workspace, "mark_status"), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                run_cmd.cmd_run(cfg, ["ws", "--no-sync", "--continue"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("did not echo requested --continue", err.getvalue())
        open_shell.assert_called_once()

    def test_continue_without_prior_session_notices_fresh_but_opens(self):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        responses = [
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "storage": "session",
            })),
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "continue": True,
            })),
        ]
        err = io.StringIO()
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses), \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)) as open_shell, \
             patch.object(run_cmd.workspace, "mark_status"), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                run_cmd.cmd_run(cfg, ["ws", "--no-sync", "--continue"])

        self.assertIn("no prior opencode session", err.getvalue())
        open_shell.assert_called_once()

    def test_no_continue_omits_flag_and_stays_silent(self):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        responses = [
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "storage": "session",
            })),
            runtime.InvocationResult(True, json.dumps({"status": "ok"})),
        ]
        err = io.StringIO()
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses) as invoke, \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)), \
             patch.object(run_cmd.workspace, "mark_status"), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                run_cmd.cmd_run(cfg, ["ws", "--no-sync"])

        prepare_payload = json.loads(invoke.call_args_list[1].args[2])
        self.assertNotIn("continue", prepare_payload)
        self.assertNotIn("--continue", err.getvalue())
        self.assertNotIn("no prior", err.getvalue())

    def test_matching_model_echo_does_not_warn(self):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        responses = [
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "storage": "session",
            })),
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "model": "provider/model-id",
            })),
        ]
        err = io.StringIO()
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses), \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)), \
             patch.object(run_cmd.workspace, "mark_status"), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                run_cmd.cmd_run(cfg, ["ws", "--no-sync", "--model", "provider/model-id"])

        self.assertNotIn("runtime did not echo requested model", err.getvalue())

    # --- TASK-29: prepare-run with --continue waits for a cold workspace ---

    def _run_continue_with(self, prepare_response, args):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        responses = [
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "storage": "session",
            })),
            prepare_response,
        ]
        err = io.StringIO()
        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=self._resolved()), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value=None), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", side_effect=responses) as invoke, \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "exec_or_wait", side_effect=SystemExit(0)) as open_shell, \
             patch.object(run_cmd.workspace, "mark_status"), \
             contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                run_cmd.cmd_run(cfg, args)
        return invoke, open_shell, raised.exception.code, err.getvalue()

    def test_continue_raises_the_read_timeout_for_prepare_run_only(self):
        invoke, _open_shell, code, err = self._run_continue_with(
            runtime.InvocationResult(True, json.dumps({
                "status": "ok", "continue": True, "session_id": "ses_1",
            })),
            ["ws", "--no-sync", "--continue"],
        )

        self.assertEqual(code, 0)
        warmup_call, prepare_call = invoke.call_args_list
        self.assertNotIn("read_timeout_s", warmup_call.kwargs)
        self.assertEqual(
            prepare_call.kwargs.get("read_timeout_s"), run_cmd.PREPARE_RUN_READ_TIMEOUT_S
        )
        # The bound must clear the shim's own wait (240 s) with margin.
        self.assertGreater(run_cmd.PREPARE_RUN_READ_TIMEOUT_S, 240)
        self.assertIn("resolving the opencode session to resume", err)

    def test_without_continue_prepare_run_keeps_the_default_read_timeout(self):
        invoke, _open_shell, code, err = self._run_continue_with(
            runtime.InvocationResult(True, json.dumps({"status": "ok"})),
            ["ws", "--no-sync"],
        )

        self.assertEqual(code, 0)
        _warmup_call, prepare_call = invoke.call_args_list
        self.assertNotIn("read_timeout_s", prepare_call.kwargs)
        self.assertNotIn("resolving the", err)

    def test_workspace_not_ready_error_is_rendered_and_nothing_opens(self):
        detail = (
            "cannot resolve the session to resume: workspace bootstrap did not "
            "complete (phase=restore); the workspace is still restoring after 240s "
            "- retry `sch run --continue` once it is ready"
        )
        _invoke, open_shell, code, err = self._run_continue_with(
            runtime.InvocationResult(True, json.dumps({
                "status": "error", "action": "prepare-run", "error": detail,
            })),
            ["ws", "--no-sync", "--continue"],
        )

        self.assertEqual(code, 1)
        open_shell.assert_not_called()
        self.assertIn("sch: prepare-run error: " + detail, err)
        self.assertIn("cannot arm harness autostart (status=error)", err)
        self.assertNotIn("prepare-run response:", err)

    def test_non_ok_prepare_run_without_error_text_falls_back_to_raw_response(self):
        _invoke, open_shell, code, err = self._run_continue_with(
            runtime.InvocationResult(True, json.dumps({"status": "rejected"})),
            ["ws", "--no-sync", "--continue"],
        )

        self.assertEqual(code, 1)
        open_shell.assert_not_called()
        self.assertIn('prepare-run response: {"status": "rejected"}', err)

    def test_watch_helper_barrier_arms_run_without_separate_preflight(self):
        cfg = type("Config", (), {"region": "test", "ws_dir": Path("/nonexistent/sch-test-ws")})()
        resolved = self._resolved()
        helper_modes = []
        supervised = []

        def helper_argv(*args):
            helper_modes.append(args[8])
            return ["sync-helper", args[8]]

        def supervise(helper, child, after_ready, presence=None):
            supervised.append((helper, child))
            after_ready()
            return 7

        with patch.object(run_cmd.procs, "require_command"), \
             patch.object(run_cmd.harness_mod, "resolve_harness", return_value=resolved), \
             patch.object(run_cmd.sync_mod, "resolve_binding", return_value="/project"), \
             patch.object(run_cmd.sync_mod, "baseline_path", return_value="/baseline"), \
             patch.object(run_cmd.sync_mod, "helper_argv", side_effect=helper_argv), \
             patch.object(run_cmd, "runtime_arn", return_value="arn"), \
             patch.object(run_cmd.runtime, "invoke_verified", return_value=runtime.InvocationResult(True, json.dumps({"status": "ok", "storage": "session"}))), \
             patch.object(run_cmd.runtime, "verify_storage", return_value=True), \
             patch.object(run_cmd.runtime, "invoke_best_effort"), \
             patch.object(run_cmd.procs, "supervise_interactive", side_effect=supervise), \
             patch.object(run_cmd.workspace, "mark_status"):
            self.assertEqual(run_cmd.cmd_run(cfg, ["ws", "--sync", "/project"]), 7)

        self.assertEqual(helper_modes, ["watch"])
        self.assertEqual(len(supervised), 1)


class MergeLiveStatusTests(unittest.TestCase):
    def test_live_fields_win_on_conflict(self):
        raw = json.dumps({"state": "running", "task_id": "old"})
        merged = status_cmd.merge_live_status(raw, {"task_id": "new", "extra": 1})
        data = json.loads(merged)
        self.assertEqual(data["task_id"], "new")
        self.assertEqual(data["state"], "running")
        self.assertEqual(data["extra"], 1)

    def test_empty_live_task_leaves_raw_untouched(self):
        raw = json.dumps({"state": "none"})
        self.assertEqual(status_cmd.merge_live_status(raw, {}), raw)

    def test_non_dict_live_task_leaves_raw_untouched(self):
        raw = json.dumps({"state": "none"})
        self.assertEqual(status_cmd.merge_live_status(raw, None), raw)

    def test_invalid_base_json_is_left_untouched(self):
        self.assertEqual(
            status_cmd.merge_live_status("not json", {"task_id": "x"}), "not json"
        )

    def test_storage_and_root_are_preserved(self):
        raw = json.dumps({"state": "succeeded"})
        merged = status_cmd.merge_live_status(raw, {
            "storage": "s3", "workspace_root": "/home/sch/workspace",
        })
        data = json.loads(merged)
        self.assertEqual(data["storage"], "s3")
        self.assertEqual(data["workspace_root"], "/home/sch/workspace")


class RenderStatusTests(unittest.TestCase):
    def _render(self, data):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            status_cmd.render_status(data)
        return buf.getvalue()

    def test_basic_fields_in_order(self):
        # A `running` state carries a fresh heartbeat here so this stays a
        # pure field-order test: a `running` state without one is now
        # rendered as suspect (add-task-liveness-safety, task 1.3), which
        # `test_status_staleness.py` covers on its own.
        out = self._render(
            {
                "state": "running",
                "harness": "opencode",
                "task_id": "t-1",
                "exit_code": 0,
                "heartbeat_utc": datetime.datetime.now(
                    datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
        lines = out.splitlines()
        self.assertEqual(lines[0], "state        : running")
        self.assertIn("exit_code    : 0", lines)
        self.assertIn("harness      : opencode", lines)
        self.assertIn("task_id      : t-1", lines)

    def test_defaults_to_state_none_when_absent(self):
        out = self._render({})
        self.assertEqual(out, "state        : none\n")

    def test_short_error_not_truncated(self):
        out = self._render({"state": "failed", "error": "boom"})
        self.assertIn("error        : boom\n", out)

    def test_error_truncated_to_500_chars_with_ellipsis(self):
        long_error = "x" * 600
        out = self._render({"state": "failed", "error": long_error})
        line = next(l for l in out.splitlines() if l.startswith("error"))
        shown = line.split(": ", 1)[1]
        self.assertTrue(shown.endswith("..."))
        self.assertEqual(len(shown), 503)  # 500 chars + "..."

    def test_error_exactly_500_chars_not_truncated(self):
        exact_error = "y" * 500
        out = self._render({"state": "failed", "error": exact_error})
        line = next(l for l in out.splitlines() if l.startswith("error"))
        shown = line.split(": ", 1)[1]
        self.assertFalse(shown.endswith("..."))
        self.assertEqual(len(shown), 500)

    def test_exit_code_zero_is_shown(self):
        # exit_code == 0 is falsy but must still be rendered (explicit
        # `is not None` check, not truthiness).
        out = self._render({"state": "done", "exit_code": 0})
        self.assertIn("exit_code    : 0", out)

    def test_task_output_is_rendered_after_terminal_fields(self):
        out = self._render({"state": "succeeded", "output": "final answer"})
        self.assertIn("output:\nfinal answer\n", out)

    def test_truncated_task_output_is_labeled(self):
        out = self._render({"state": "succeeded", "output": "tail", "output_truncated": True})
        self.assertIn("output truncated; showing the final 12000 characters", out)

    def test_duration_zero_is_shown(self):
        out = self._render({"state": "done", "duration_s": 0})
        self.assertIn("duration_s   : 0", out)

    def test_model_line_shown_when_field_present(self):
        out = self._render({
            "state": "succeeded", "harness": "claude",
            "model": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
        })
        self.assertIn(
            "model        : eu.anthropic.claude-haiku-4-5-20251001-v1:0", out
        )

    def test_no_model_line_when_field_absent(self):
        # Status object written by an image predating the model field must
        # render without a model line and without errors.
        out = self._render({"state": "succeeded", "harness": "opencode"})
        self.assertNotIn("model", out)

    def test_live_storage_diagnostics_are_shown(self):
        out = self._render({
            "state": "running", "storage": "s3",
            "workspace_root": "/home/sch/workspace",
        })
        self.assertIn("storage      : s3", out)
        self.assertIn("workspace_root: /home/sch/workspace", out)


if __name__ == "__main__":
    unittest.main()
