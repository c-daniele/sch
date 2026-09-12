"""Unit tests for the image digest pin of the runtime stack (TASK-30).

Problem under test: AgentCore resolves the image reference once, when it
creates a runtime version, so a ContainerUri that names only a tag
(repo:v1) never picks up a rebuild of that same tag. The fix pins the
digest resolved by deploy.sh after the push (repo@sha256:...) — spec
runtime-provisioning R15c/R15d.

- infra/agent_runtime.yaml: the ImageDigest parameter, the ContainerUri
  rendered with and without a digest, the SCH_IMAGE_DIGEST marker in the
  runtime environment, and the execution role staying untouched;
- infra/deploy.sh: the digest reaches both runtime stack deploys, is
  resolved after every build path, and the sourced helpers behave against a
  stub `aws` (tag found / missing / unresolvable; deployed tag and digest
  reused by a stack-only deploy, including stacks predating the parameter).

Reuses the CloudFormation loader/renderer of test_runtime_tuning (PyYAML is
a test-only dependency; the product stays stdlib).
"""

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_runtime_tuning import (
    DEPLOY_PATH,
    FAKE_ACCOUNT,
    FAKE_REGION,
    load_template,
    render_resource,
    render_role,
    resolve,
    sub_value,
)

DIGEST = "sha256:" + "0123456789abcdef" * 4
OTHER_DIGEST = "sha256:" + "fedcba9876543210" * 4
# The documented pattern of AWS::BedrockAgentCore::Runtime ContainerUri, with
# the character class the docs render without brackets restored: registry /
# repository, optional :tag (no ':' or '@' inside), optional @digest.
CONTAINER_URI_PATTERN = re.compile(
    r"^(([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(\.cn)?|public\.ecr\.aws)/"
    r"((?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*)"
    r"(?::([^:@]{1,300}))?(?:@(.+))?$"
)


def container_uri(overrides=None):
    template = load_template()
    runtime, parameters = render_resource(template, "AgentRuntime", overrides)
    return sub_value(
        runtime["Properties"]["AgentRuntimeArtifact"]["ContainerConfiguration"]["ContainerUri"],
        parameters,
    )


def runtime_environment(overrides=None):
    template = load_template()
    runtime, _ = render_resource(template, "AgentRuntime", overrides)
    return runtime["Properties"]["EnvironmentVariables"]


class ImageDigestParameterTest(unittest.TestCase):
    """The parameter accepts exactly an empty value or a sha256 digest."""

    def setUp(self):
        self.spec = load_template()["Parameters"]["ImageDigest"]
        self.pattern = re.compile(self.spec["AllowedPattern"])

    def test_defaults_to_empty_so_older_stacks_and_stack_only_deploys_keep_the_tag(self):
        self.assertEqual(self.spec["Default"], "")
        self.assertEqual(self.spec["Type"], "String")

    def test_pattern_accepts_empty_and_lowercase_sha256(self):
        for value in ("", DIGEST, OTHER_DIGEST):
            with self.subTest(value=value):
                self.assertIsNotNone(self.pattern.fullmatch(value))

    def test_pattern_rejects_anything_else(self):
        for value in (
            "abc",
            "sha256:" + "0123456789ABCDEF" * 4,   # uppercase hex
            "sha256:" + "0" * 63,                 # too short
            "sha256:" + "0" * 65,                 # too long
            "sha512:" + "0" * 128,                # other algorithm
            "v2@" + DIGEST,                       # tag smuggled in
        ):
            with self.subTest(value=value):
                self.assertIsNone(self.pattern.fullmatch(value))


class ContainerUriTest(unittest.TestCase):
    """R15c: tag@digest when the deploy resolved a digest, tag only otherwise."""

    def test_tag_only_when_no_digest_is_pinned(self):
        self.assertEqual(
            container_uri(),
            f"{FAKE_ACCOUNT}.dkr.ecr.{FAKE_REGION}.amazonaws.com/sch-dev:v1",
        )

    def test_digest_only_when_pinned(self):
        uri = container_uri({"ApplicationVersion": "v2", "ImageDigest": DIGEST})
        self.assertEqual(
            uri, f"{FAKE_ACCOUNT}.dkr.ecr.{FAKE_REGION}.amazonaws.com/sch-dev@{DIGEST}"
        )

    def test_pinned_form_never_combines_tag_and_digest(self):
        """Regression guard: <repo>:<tag>@<digest> passes the CloudFormation
        pattern and creates a runtime version, but AgentCore then fails to
        start the microVM (502 on every invoke, no container log) — verified
        live on 2026-09-06. Digest only, always."""
        uri = container_uri({"ApplicationVersion": "v2", "ImageDigest": DIGEST})
        self.assertNotIn(":v2", uri)
        self.assertNotIn(f":v2@{DIGEST}", uri)

    def test_same_tag_different_digest_changes_the_uri(self):
        """The property AgentCore never saw change before: a rebuild of v2."""
        before = container_uri({"ApplicationVersion": "v2", "ImageDigest": DIGEST})
        after = container_uri({"ApplicationVersion": "v2", "ImageDigest": OTHER_DIGEST})
        self.assertNotEqual(before, after)

    def test_both_forms_match_the_documented_agentcore_pattern(self):
        for overrides in ({}, {"ApplicationVersion": "v2", "ImageDigest": DIGEST}):
            with self.subTest(overrides=overrides):
                uri = container_uri(overrides)
                match = CONTAINER_URI_PATTERN.match(uri)
                self.assertIsNotNone(match, uri)
                if overrides:
                    self.assertIsNone(match.group(6))   # no tag in the pinned form
                    self.assertEqual(match.group(7), DIGEST)
                else:
                    self.assertEqual(match.group(6), "v1")
                    self.assertIsNone(match.group(7))


class RuntimeEnvironmentTest(unittest.TestCase):
    """SCH_IMAGE_DIGEST identifies the build from inside a session; the rest
    of the runtime (environment, execution role) is untouched by the pin."""

    def test_digest_marker_present_only_when_pinned(self):
        self.assertNotIn("SCH_IMAGE_DIGEST", runtime_environment())
        env = runtime_environment({"ImageDigest": DIGEST})
        self.assertEqual(env["SCH_IMAGE_DIGEST"], {"Ref": "ImageDigest"})

    def test_pin_changes_nothing_else_in_the_runtime_environment(self):
        plain = runtime_environment()
        pinned = dict(runtime_environment({"ImageDigest": DIGEST}))
        pinned.pop("SCH_IMAGE_DIGEST")
        self.assertEqual(plain, pinned)

    def test_execution_role_does_not_depend_on_the_digest(self):
        template = load_template()
        plain, _, _ = render_role(template)
        pinned, _, _ = render_role(template, {"ImageDigest": DIGEST, "ApplicationVersion": "v9"})
        self.assertEqual(plain, pinned)

    def test_condition_is_a_plain_not_equals_on_the_parameter(self):
        template = load_template()
        self.assertEqual(
            template["Conditions"]["ImageDigestPinned"],
            {"Fn::Not": [{"Fn::Equals": [{"Ref": "ImageDigest"}, ""]}]},
        )
        # Sanity: resolve() honors it (the two branches above rely on this).
        self.assertIs(
            resolve({"Fn::If": ["ImageDigestPinned", "yes", "no"]},
                    template["Conditions"], {"ImageDigest": ""}),
            "no",
        )


class DeployScriptTextTest(unittest.TestCase):
    """The digest is wired through every path of deploy.sh."""

    @classmethod
    def setUpClass(cls):
        cls.script = DEPLOY_PATH.read_text()

    def test_digest_reaches_both_runtime_stack_deploys(self):
        # deploy_base_stack (first-time creation) and the main deploy: the
        # runtime must be created pinned, or the first install would produce
        # two runtime versions back to back.
        self.assertEqual(self.script.count('"ImageDigest=${IMAGE_DIGEST}"'), 2)
        for match in re.finditer(r'"ApplicationVersion=\$\{VERSION\}" \\\n\s+"([A-Za-z]+)=', self.script):
            self.assertEqual(match.group(1), "ImageDigest")

    def test_digest_resolved_after_every_image_path(self):
        # After the CodeBuild build, after the local docker push, and for
        # `-s -v <tag>` (an explicit tag that must already exist).
        calls = re.findall(r"^\s+resolve_image_digest$", self.script, re.M)
        self.assertEqual(len(calls), 3, calls)
        self.assertNotIn('--image-ids "imageTag=${VERSION}" >/dev/null', self.script)

    def test_stack_only_deploy_reuses_both_deployed_values(self):
        self.assertIn(
            'if [ "${SKIP_IMAGE}" -eq 1 ] && [ "${VERSION_SET}" -eq 0 ]; then\n    reuse_deployed_image',
            self.script,
        )
        self.assertIn("ParameterKey=='ApplicationVersion' || ParameterKey=='ImageDigest'", self.script)

    def test_help_text_names_the_rule_the_readme_points_at(self):
        self.assertIn("What creates a new runtime version", self.script)
        self.assertIn("printenv SCH_IMAGE_DIGEST", self.script)


class SourcedHelpersTest(unittest.TestCase):
    """resolve_image_digest / reuse_deployed_image against a stub `aws`."""

    @classmethod
    def setUpClass(cls):
        script = DEPLOY_PATH.read_text()
        match = re.search(
            r"^# Image digest helpers.*?^# \(end image digest helpers\)",
            script, re.S | re.M,
        )
        assert match, "image digest helper block not found in deploy.sh"
        cls.tmp = tempfile.TemporaryDirectory()
        cls.helpers = Path(cls.tmp.name) / "helpers.sh"
        cls.helpers.write_text(match.group(0))
        cls.bin = Path(cls.tmp.name) / "bin"
        cls.bin.mkdir()
        stub = cls.bin / "aws"
        stub.write_text(
            "#!/bin/bash\n"
            'printf \'%s\\n\' "$*" >> "${STUB_LOG}"\n'
            'if [ -n "${STUB_EXIT:-}" ]; then\n'
            '  echo "An error occurred (stub)" >&2\n'
            '  exit "${STUB_EXIT}"\n'
            "fi\n"
            'printf \'%b\' "${STUB_OUT}"\n'
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        cls.log = Path(cls.tmp.name) / "aws.log"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_helper(self, function, stub_out="", stub_exit=""):
        if self.log.exists():
            self.log.unlink()
        env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin",
            "STUB_LOG": str(self.log),
            "STUB_OUT": stub_out,
            "STUB_EXIT": stub_exit,
        }
        # Same shell options as deploy.sh, and the globals it would have set
        # by the time each helper runs.
        proc = subprocess.run(
            ["bash", "-c",
             'set -euo pipefail; source "$1"; '
             'PROJECT_NAME=sch; ENVIRONMENT=dev; REGION=eu-test-1; '
             'ECR_REPO=sch-dev; VERSION=v2; IMAGE_DIGEST=""; '
             'IMAGE_URI=111122223333.dkr.ecr.eu-test-1.amazonaws.com/sch-dev:v2; '
             f'{function}; printf \'%s\\n%s\\n\' "${{VERSION}}" "${{IMAGE_DIGEST}}"',
             "bash", str(self.helpers)],
            env=env, capture_output=True, text=True,
        )
        return proc

    def aws_calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    # --- resolve_image_digest -------------------------------------------------

    def test_resolve_sets_the_digest_of_the_tag(self):
        proc = self.run_helper("resolve_image_digest", stub_out=f"{DIGEST}\\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["v2", DIGEST])
        calls = self.aws_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("ecr describe-images --repository-name sch-dev --region eu-test-1", calls[0])
        self.assertIn("--image-ids imageTag=v2", calls[0])
        self.assertIn("--query imageDetails[0].imageDigest --output text", calls[0])

    def test_resolve_fails_when_the_tag_does_not_exist(self):
        proc = self.run_helper("resolve_image_digest", stub_exit="254")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not found in ECR", proc.stderr)
        self.assertIn("sch-dev:v2", proc.stderr)
        self.assertEqual(proc.stdout, "")  # set -e stopped the caller

    def test_resolve_fails_on_an_unusable_answer(self):
        for answer in ("None\\n", "\\n", "garbage\\n"):
            with self.subTest(answer=answer):
                proc = self.run_helper("resolve_image_digest", stub_out=answer)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("could not resolve the digest", proc.stderr)

    # --- reuse_deployed_image -------------------------------------------------

    def test_reuse_takes_deployed_tag_and_digest(self):
        proc = self.run_helper(
            "reuse_deployed_image",
            stub_out=f"ApplicationVersion\\tv3\\nImageDigest\\t{OTHER_DIGEST}\\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["v3", OTHER_DIGEST])
        calls = self.aws_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("cloudformation describe-stacks --stack-name sch-dev-runtime", calls[0])

    def test_reuse_on_a_stack_predating_the_parameter_keeps_the_digest_empty(self):
        # An older stack has no ImageDigest parameter (or an empty one): the
        # tag is reused and the URI stays tag-only, i.e. unchanged.
        for stub_out in ("ApplicationVersion\\tv3\\n", "ApplicationVersion\\tv3\\nImageDigest\\t\\n",
                         "ApplicationVersion\\tv3\\nImageDigest\\tNone\\n"):
            with self.subTest(stub_out=stub_out):
                proc = self.run_helper("reuse_deployed_image", stub_out=stub_out)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.splitlines(), ["v3", ""])

    def test_reuse_without_a_stack_leaves_the_defaults_and_succeeds(self):
        proc = self.run_helper("reuse_deployed_image", stub_exit="254")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.splitlines(), ["v2", ""])


if __name__ == "__main__":
    unittest.main()
