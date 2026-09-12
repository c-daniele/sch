import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sch import awsteardown
from sch.commands import destroy


class Cfg:
    region = "eu-west-1"
    project = "sch"
    env = "dev"

    def __init__(self, tmp):
        self.runtime_arn_cache = Path(tmp) / "runtime-arn"
        self.checkpoint_bucket_cache = Path(tmp) / "checkpoint-bucket"

    def stack_name(self):
        return "{}-{}-runtime".format(self.project, self.env)


def run_destroy(cfg, args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = destroy.cmd_destroy(cfg, args)
    return rc, out.getvalue(), err.getvalue()


class DestroyPlanTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Cfg(self._tmp.name)

    def test_refuses_when_the_account_cannot_be_resolved(self):
        with patch.object(awsteardown, "account_id", return_value=""):
            rc, _, err = run_destroy(self.cfg, [])
        self.assertEqual(rc, 1)
        self.assertIn("cannot resolve the AWS account", err)

    def test_dry_run_prints_account_and_deletes_nothing(self):
        with patch.object(awsteardown, "account_id", return_value="111122223333"), \
             patch.object(awsteardown, "stack_exists", return_value=True), \
             patch.object(awsteardown, "repository_exists", return_value=True), \
             patch.object(awsteardown, "bucket_exists", return_value=True), \
             patch.object(awsteardown, "delete_stack") as del_stack, \
             patch.object(awsteardown, "delete_bucket") as del_bucket, \
             patch.object(awsteardown, "purge_repository_images") as purge:
            rc, out, _ = run_destroy(self.cfg, ["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("111122223333", out)
        self.assertIn("dry run", out)
        del_stack.assert_not_called()
        del_bucket.assert_not_called()
        purge.assert_not_called()

    def test_plan_covers_both_stacks_the_repo_and_three_buckets(self):
        with patch.object(awsteardown, "stack_exists", return_value=True), \
             patch.object(awsteardown, "repository_exists", return_value=True), \
             patch.object(awsteardown, "bucket_exists", return_value=True):
            targets = destroy._plan("sch", "dev", "eu-west-1", "111122223333", False)
        names = [t.name for t in targets]
        self.assertIn("sch-dev-runtime", names)
        self.assertIn("sch-dev-bootstrap", names)
        self.assertIn("sch-dev-ecr", names)          # legacy deployments
        self.assertIn("sch-dev", names)              # ECR repository
        self.assertIn("sch-dev-build-sources-111122223333", names)
        self.assertIn("sch-dev-cfn-bootstrap-111122223333", names)
        self.assertIn("sch-dev-checkpoints-111122223333", names)

    def test_stack_owned_blockers_come_before_the_stack(self):
        """Regression: deleting the bootstrap stack before emptying the bucket it
        owns leaves it in DELETE_FAILED ("the bucket you tried to delete is not
        empty"), which is exactly what happened in a real teardown."""
        with patch.object(awsteardown, "stack_exists", return_value=True), \
             patch.object(awsteardown, "repository_exists", return_value=True), \
             patch.object(awsteardown, "bucket_exists", return_value=True):
            names = [
                t.name
                for t in destroy._plan("sch", "dev", "eu-west-1", "111122223333", False)
            ]
        self.assertLess(names.index("sch-dev-build-sources-111122223333"),
                        names.index("sch-dev-bootstrap"))
        self.assertLess(names.index("sch-dev"),            # ECR images
                        names.index("sch-dev-bootstrap"))
        # The retained checkpoint bucket is not a blocker, so it stays last.
        self.assertGreater(names.index("sch-dev-checkpoints-111122223333"),
                           names.index("sch-dev-bootstrap"))

    def test_keep_checkpoints_excludes_the_l2_bucket(self):
        with patch.object(awsteardown, "stack_exists", return_value=True), \
             patch.object(awsteardown, "repository_exists", return_value=True), \
             patch.object(awsteardown, "bucket_exists", return_value=True):
            targets = destroy._plan("sch", "dev", "eu-west-1", "111122223333", True)
        self.assertNotIn(
            "sch-dev-checkpoints-111122223333", [t.name for t in targets]
        )

    def test_nothing_present_is_a_clean_exit(self):
        with patch.object(awsteardown, "account_id", return_value="111122223333"), \
             patch.object(awsteardown, "stack_exists", return_value=False), \
             patch.object(awsteardown, "repository_exists", return_value=False), \
             patch.object(awsteardown, "bucket_exists", return_value=False):
            rc, out, _ = run_destroy(self.cfg, [])
        self.assertEqual(rc, 0)
        self.assertIn("already gone", out)

    def test_wrong_confirmation_aborts_before_any_deletion(self):
        with patch.object(awsteardown, "account_id", return_value="111122223333"), \
             patch.object(awsteardown, "stack_exists", return_value=True), \
             patch.object(awsteardown, "repository_exists", return_value=False), \
             patch.object(awsteardown, "bucket_exists", return_value=False), \
             patch("builtins.input", return_value="yes"), \
             patch.object(awsteardown, "delete_stack") as del_stack:
            rc, _, err = run_destroy(self.cfg, [])
        self.assertEqual(rc, 1)
        self.assertIn("cancelled", err)
        del_stack.assert_not_called()


class DestroyExecutionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Cfg(self._tmp.name)
        self.cfg.runtime_arn_cache.write_text("arn:aws:...:runtime/stale")
        self.cfg.checkpoint_bucket_cache.write_text("stale-bucket")

    def _patches(self, **overrides):
        base = {
            "account_id": lambda region: "111122223333",
            "stack_exists": lambda name, region: name.endswith("-runtime"),
            "repository_exists": lambda name, region: False,
            "bucket_exists": lambda name, region: False,
            "delete_stack": lambda name, region, retain_resources=(): "deleted",
            "purge_repository_images": lambda name, region: 0,
            "delete_bucket": lambda name, region: "deleted",
            "deregister_telegram_webhook": lambda token: "skipped (no bot token available)",
            "read_setenv_token": lambda root: "",
        }
        base.update(overrides)
        return [patch.object(awsteardown, k, v) for k, v in base.items()]

    def test_successful_run_deletes_and_invalidates_the_caches(self):
        patches = self._patches()
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        rc, out, _ = run_destroy(self.cfg, ["--yes"])
        self.assertEqual(rc, 0)
        self.assertIn("teardown complete", out)
        self.assertFalse(self.cfg.runtime_arn_cache.exists())
        self.assertFalse(self.cfg.checkpoint_bucket_cache.exists())

    def test_a_failed_step_is_reported_and_exits_non_zero(self):
        def boom(name, region, retain_resources=()):
            raise awsteardown.TeardownError("AccessDenied")

        patches = self._patches(delete_stack=boom)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        rc, out, err = run_destroy(self.cfg, ["--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("AccessDenied", out)
        self.assertIn("re-runnable", err)

    def test_runtime_stack_failure_retries_retaining_the_build_project(self):
        calls = []

        def flaky(name, region, retain_resources=()):
            calls.append(retain_resources)
            if not retain_resources:
                raise awsteardown.TeardownError("cannot delete ImageRebuildProject")
            return "deleted"

        patches = self._patches(delete_stack=flaky)
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        rc, out, _ = run_destroy(self.cfg, ["--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [(), ("ImageRebuildProject",)])
        self.assertIn("retained ImageRebuildProject", out)


class StackFailureReasonTests(unittest.TestCase):
    def test_the_resource_message_replaces_the_waiter_noise(self):
        events = json.dumps([
            {"id": "sch-dev-bootstrap",
             "reason": "The following resource(s) failed to delete: [BuildSourcesBucket]."},
            {"id": "BuildSourcesBucket",
             "reason": "The bucket you tried to delete is not empty (Service: S3...)"},
        ])
        with patch.object(awsteardown, "_aws", return_value=(0, events, "")):
            detail = awsteardown.stack_delete_failure_reason("sch-dev-bootstrap", "eu-west-1")
        self.assertIn("BuildSourcesBucket", detail)
        self.assertIn("not empty", detail)

    def test_no_events_degrades_to_empty(self):
        with patch.object(awsteardown, "_aws", return_value=(1, "", "denied")):
            self.assertEqual(
                awsteardown.stack_delete_failure_reason("s", "eu-west-1"), ""
            )


class TelegramTokenTests(unittest.TestCase):
    def test_token_is_read_from_setenv(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "infra").mkdir()
            (root / "infra" / "setenv.sh").write_text(
                "#!/bin/bash\nexport TELEGRAM_BOT_TOKEN='123:ABC'\n"
                "export TELEGRAM_CHAT_ID=-100\n"
            )
            self.assertEqual(awsteardown.read_setenv_token(root), "123:ABC")

    def test_missing_file_and_missing_root_are_empty(self):
        import tempfile
        self.assertEqual(awsteardown.read_setenv_token(None), "")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(awsteardown.read_setenv_token(Path(tmp)), "")

    def test_no_token_means_no_call(self):
        self.assertIn("skipped", awsteardown.deregister_telegram_webhook(""))


class ConfirmPhraseTests(unittest.TestCase):
    """The shared confirmation used by delete/destroy/uninstall."""

    def _confirm(self, typed):
        from sch import cli as cli_mod
        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", return_value=typed):
            with redirect_stdout(out), redirect_stderr(err):
                ok = cli_mod.confirm_phrase("DELETE sch-dev")
        return ok

    def test_exact_phrase_is_accepted(self):
        self.assertTrue(self._confirm("DELETE sch-dev"))

    def test_quotes_shown_in_the_prompt_may_be_typed_back(self):
        self.assertTrue(self._confirm("'DELETE sch-dev'"))
        self.assertTrue(self._confirm('"DELETE sch-dev"'))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertTrue(self._confirm("  DELETE sch-dev  "))

    def test_the_first_word_alone_is_not_enough(self):
        self.assertFalse(self._confirm("DELETE"))

    def test_case_and_content_must_match(self):
        self.assertFalse(self._confirm("delete sch-dev"))
        self.assertFalse(self._confirm("DELETE sch-stg"))

    def test_the_prompt_quotes_the_phrase(self):
        from sch import cli as cli_mod
        seen = {}

        def fake_input(prompt=""):
            seen["prompt"] = prompt
            return "DELETE sch-dev"

        with patch("builtins.input", fake_input):
            cli_mod.confirm_phrase("DELETE sch-dev")
        self.assertIn("'DELETE sch-dev'", seen["prompt"])

    def test_yes_skips_the_prompt_entirely(self):
        from sch import cli as cli_mod
        with patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertTrue(cli_mod.confirm_phrase("DELETE sch-dev", yes=True))

    def test_no_terminal_explains_itself(self):
        from sch import cli as cli_mod
        out, err = io.StringIO(), io.StringIO()
        with patch("builtins.input", side_effect=EOFError):
            with redirect_stdout(out), redirect_stderr(err):
                ok = cli_mod.confirm_phrase("DELETE sch-dev")
        self.assertFalse(ok)
        self.assertIn("not a terminal", err.getvalue())
        self.assertIn("--yes", err.getvalue())


if __name__ == "__main__":
    unittest.main()
