"""Stack-output cache keyed by account, region and stack (cli-cross-platform
R9a, TASK-25): a runtime ARN or bucket resolved with one account's
credentials is never served to another account, region or stack."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_CLI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)

from sch import config as config_mod

ACCOUNT_A = "111122223333"
ACCOUNT_B = "444455556666"
_ENV_NAMES = ("SCH_REGION", "SCH_PROJECT", "SCH_ENV", "SCH_RUNTIME_ARN",
              "SCH_CHECKPOINT_BUCKET", "SCH_WORKSPACE_REGISTRY_URL")


def make_cfg(tmp, **env):
    """A real Config built from a controlled environment."""
    with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
        for name in _ENV_NAMES:
            os.environ.pop(name, None)
        os.environ.update(env)
        return config_mod.Config()


class FakeAws:
    """Answers like two accounts that both deploy the same stacks."""

    def __init__(self, account=ACCOUNT_A):
        self.account = account
        self.calls = []
        self.sts_result = None
        self.describe_result = None

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[1:3] == ["sts", "get-caller-identity"]:
            if self.sts_result is not None:
                return self.sts_result
            return SimpleNamespace(returncode=0, stdout=self.account + "\n", stderr="")
        if argv[1:3] == ["cloudformation", "describe-stacks"]:
            if self.describe_result is not None:
                return self.describe_result
            stack = argv[argv.index("--stack-name") + 1]
            region = argv[argv.index("--region") + 1]
            if "RuntimeArn" in argv[argv.index("--query") + 1]:
                value = "arn:aws:bedrock-agentcore:{}:{}:runtime/{}".format(
                    region, self.account, stack)
            else:
                value = "{}-checkpoints-{}".format(stack[:-len("-runtime")], self.account)
            return SimpleNamespace(returncode=0, stdout=value + "\n", stderr="")
        raise AssertionError("unexpected AWS call: {}".format(argv))

    def count(self, *command):
        return sum(1 for argv in self.calls if argv[1:1 + len(command)] == list(command))


class StackOutputCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.config_dir = Path(self.tmp) / "sch"
        self.fake = FakeAws()

    def resolve(self, cfg):
        with patch("subprocess.run", self.fake):
            return config_mod.runtime_arn(cfg), config_mod.checkpoint_bucket(cfg)

    def entry(self, account, region="eu-west-1", stack="sch-dev-runtime"):
        return self.config_dir / "stack-outputs" / account / region / stack

    def test_values_are_cached_per_account(self):
        arn_a, bucket_a = self.resolve(make_cfg(self.tmp))
        self.assertEqual(bucket_a, "sch-dev-checkpoints-" + ACCOUNT_A)
        self.assertEqual((self.entry(ACCOUNT_A) / "checkpoint-bucket").read_text(), bucket_a + "\n")
        self.assertEqual((self.entry(ACCOUNT_A) / "runtime-arn").read_text(), arn_a + "\n")

        # Same laptop, the other account's credentials: the TASK-25 incident.
        self.fake.account = ACCOUNT_B
        arn_b, bucket_b = self.resolve(make_cfg(self.tmp))
        self.assertEqual(bucket_b, "sch-dev-checkpoints-" + ACCOUNT_B)
        self.assertIn(":" + ACCOUNT_B + ":", arn_b)

        # Back to the first account: both entries coexist, no stack lookup.
        self.fake.account = ACCOUNT_A
        self.fake.calls.clear()
        self.assertEqual(self.resolve(make_cfg(self.tmp)), (arn_a, bucket_a))
        self.assertEqual(self.fake.count("cloudformation", "describe-stacks"), 0)
        self.assertEqual(self.fake.count("sts", "get-caller-identity"), 1)

    def test_region_and_stack_name_are_part_of_the_key(self):
        self.resolve(make_cfg(self.tmp))
        _, bucket = self.resolve(make_cfg(self.tmp, SCH_ENV="poc"))
        self.assertEqual(bucket, "sch-poc-checkpoints-" + ACCOUNT_A)
        arn, _ = self.resolve(make_cfg(self.tmp, SCH_REGION="us-east-1"))
        self.assertIn(":us-east-1:", arn)
        self.assertEqual(self.fake.count("cloudformation", "describe-stacks"), 6)
        self.assertTrue(self.entry(ACCOUNT_A, stack="sch-poc-runtime").is_dir())
        self.assertTrue(self.entry(ACCOUNT_A, region="us-east-1").is_dir())

    def test_one_account_lookup_per_command(self):
        cfg = make_cfg(self.tmp)
        with patch("subprocess.run", self.fake):
            for _ in range(3):
                config_mod.runtime_arn(cfg)
                config_mod.checkpoint_bucket(cfg)
        self.assertEqual(self.fake.count("sts", "get-caller-identity"), 1)
        self.assertEqual(self.fake.count("cloudformation", "describe-stacks"), 2)

    def test_overrides_need_no_aws_call(self):
        cfg = make_cfg(self.tmp, SCH_RUNTIME_ARN="arn:override",
                       SCH_CHECKPOINT_BUCKET="bucket-override")
        with patch("subprocess.run", side_effect=AssertionError("no AWS call expected")):
            self.assertEqual(config_mod.runtime_arn(cfg), "arn:override")
            self.assertEqual(config_mod.checkpoint_bucket(cfg), "bucket-override")
        self.assertFalse((self.config_dir / "stack-outputs").exists())

    def test_legacy_flat_files_are_never_read_and_go_away(self):
        self.config_dir.mkdir()
        legacy_arn = self.config_dir / "runtime-arn"
        legacy_bucket = self.config_dir / "checkpoint-bucket"
        legacy_arn.write_text("arn:aws:bedrock-agentcore:eu-west-1:" + ACCOUNT_B + ":runtime/old\n")
        legacy_bucket.write_text("sch-dev-checkpoints-" + ACCOUNT_B + "\n")
        arn, bucket = self.resolve(make_cfg(self.tmp))
        self.assertIn(":" + ACCOUNT_A + ":", arn)
        self.assertEqual(bucket, "sch-dev-checkpoints-" + ACCOUNT_A)
        self.assertFalse(legacy_arn.exists())
        self.assertFalse(legacy_bucket.exists())

    def test_an_empty_cache_file_is_a_miss(self):
        self.entry(ACCOUNT_A).mkdir(parents=True)
        (self.entry(ACCOUNT_A) / "checkpoint-bucket").write_text("\n")
        _, bucket = self.resolve(make_cfg(self.tmp))
        self.assertEqual(bucket, "sch-dev-checkpoints-" + ACCOUNT_A)
        self.assertEqual((self.entry(ACCOUNT_A) / "checkpoint-bucket").read_text(), bucket + "\n")

    def test_a_failed_account_lookup_dies_once_and_names_the_way_out(self):
        self.fake.sts_result = SimpleNamespace(
            returncode=254, stdout="",
            stderr="\naws: [ERROR]: An error occurred (ExpiredToken) when calling the "
                   "GetCallerIdentity operation: The security token included in the "
                   "request is expired\n",
        )
        cfg = make_cfg(self.tmp)
        err = io.StringIO()
        with patch("subprocess.run", self.fake), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                config_mod.checkpoint_bucket(cfg)
            with self.assertRaises(SystemExit):
                config_mod.runtime_arn(cfg)
            # Callers that degrade on SystemExit do not repeat the lookup.
            self.assertEqual(config_mod.runtime_id(cfg), "")
        self.assertEqual(self.fake.count("sts", "get-caller-identity"), 1)
        self.assertEqual(self.fake.count("cloudformation", "describe-stacks"), 0)
        first = err.getvalue().splitlines()[0]
        self.assertEqual(
            first,
            "sch: cannot determine the AWS account of the current credentials (An error "
            "occurred (ExpiredToken) when calling the GetCallerIdentity operation: The "
            "security token included in the request is expired); refresh them, or set "
            "SCH_RUNTIME_ARN and SCH_CHECKPOINT_BUCKET",
        )

    def test_an_unexpected_sts_answer_is_a_failure(self):
        self.fake.sts_result = SimpleNamespace(returncode=0, stdout="None\n", stderr="")
        err = io.StringIO()
        with patch("subprocess.run", self.fake), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            config_mod.checkpoint_bucket(make_cfg(self.tmp))
        self.assertIn("unexpected answer 'None'", err.getvalue())

    def test_a_stack_lookup_failure_names_the_aws_error(self):
        self.fake.describe_result = SimpleNamespace(
            returncode=254, stdout="",
            stderr="\naws: [ERROR]: An error occurred (ValidationError) when calling the "
                   "DescribeStacks operation: Stack with id sch-dev-runtime does not exist\n",
        )
        err = io.StringIO()
        with patch("subprocess.run", self.fake), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            config_mod.checkpoint_bucket(make_cfg(self.tmp))
        self.assertEqual(
            err.getvalue(),
            "sch: cannot resolve checkpoint bucket (stack sch-dev-runtime in eu-west-1: An "
            "error occurred (ValidationError) when calling the DescribeStacks operation: "
            "Stack with id sch-dev-runtime does not exist); set SCH_CHECKPOINT_BUCKET\n",
        )
        self.assertFalse((self.config_dir / "stack-outputs").exists())

    def test_unsafe_path_components_bypass_the_cache(self):
        for env in ({"SCH_ENV": "../../escape"}, {"SCH_REGION": "eu-west-1/../x"}):
            with self.subTest(env=env):
                self.fake.calls.clear()
                cfg = make_cfg(self.tmp, **env)
                self.resolve(cfg)
                self.resolve(cfg)
                self.assertEqual(self.fake.count("cloudformation", "describe-stacks"), 4)
        self.assertFalse((self.config_dir / "stack-outputs").exists())
        self.assertEqual(sorted(p.name for p in Path(self.tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
