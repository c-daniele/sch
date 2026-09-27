"""Tests of the isolation part of infra/deploy.sh (TASK-20.3).

Spec: docs/specs/security/per-principal-isolation.md R2, R6, R10-R12, R17, R48.

- The plane helper block is sourced against a stub `aws`: one stack per
  plane with the runtime stack's configuration, a failed plane does not stop
  the others, orphans are deleted, a ROLLBACK_COMPLETE plane is recreated.
- deploy.sh itself: the preflight runs before the bootstrap stack and stops
  the deploy on a refused switch combination; orphans go before the runtime
  stack update, planes after it; every plane parameter the template needs is
  passed.
"""

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import cfn_render

INFRA_DIR = Path(__file__).resolve().parent
DEPLOY = INFRA_DIR / "deploy.sh"

STUB = r'''#!/bin/bash
printf '%s\n' "$*" >> "${STUB_LOG}"
case "$1 $2" in
  "cloudformation describe-stacks")
    query=""
    while [ $# -gt 0 ]; do [ "$1" = "--query" ] && query="$2"; shift; done
    case "${query}" in
      *SharedRuntimePolicyArns*) echo "arn:aws:iam::111122223333:policy/sch-dev-agentcore-policy,arn:aws:iam::111122223333:policy/sch-dev-runtime-cap-polly" ;;
      *WorkspaceRegistryRoleArn*) echo "arn:aws:iam::111122223333:role/sch-dev-workspace-registry-role" ;;
      *CheckpointBucketName*) echo "sch-dev-checkpoints-111122223333" ;;
      *ImageRebuildProjectName*) echo "None" ;;
      *"'ApplicationVersion'"*) echo "v7" ;;
      *"'ImageDigest'"*) echo "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" ;;
      *IdleRuntimeSessionTimeoutSeconds*) echo "1200" ;;
      *MaxLifetimeSeconds*) echo "28800" ;;
      *PiDefaultModel*) echo "" ;;
      *NodeHeapMb*) echo "1792" ;;
      *BuildJobs*) echo "2" ;;
      *RuntimeAwsApiRead*) echo "false" ;;
      *RuntimeArn*) echo "arn:aws:bedrock-agentcore:eu-west-1:111122223333:runtime/sch_dev_o_x-AbCdEf1234" ;;
      *) echo "None" ;;
    esac ;;
  "cloudformation deploy")
    case "$*" in *"${STUB_FAIL_STACK:-no-such-stack}"*) exit 255 ;; esac ;;
  "sts get-caller-identity") echo "111122223333" ;;
esac
exit 0
'''

KEY_A = "0123456789abcdef"
KEY_B = "fedcba9876543210"
KEY_GONE = "1111111111111111"


class PlaneHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = DEPLOY.read_text()
        match = re.search(r"^# Isolation plane helpers.*?^# \(end isolation plane helpers\)",
                          script, re.S | re.M)
        assert match, "isolation plane helper block not found in deploy.sh"
        cls.helpers = match.group(0)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "bin").mkdir()
        stub = root / "bin" / "aws"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.root = root
        self.log = root / "log"
        self.log.write_text("")
        (root / "helpers.sh").write_text(self.helpers)
        self.plan = root / "plan.tsv"

    def write_plan(self, lines):
        self.plan.write_text("".join("\t".join(line) + "\n" for line in lines))

    def run_helpers(self, body, **env):
        base = {
            "PATH": f"{self.root / 'bin'}:/usr/bin:/bin",
            "STUB_LOG": str(self.log),
            "PROJECT_NAME": "sch", "ENVIRONMENT": "dev", "REGION": "eu-west-1",
            "SCRIPT_DIR": str(INFRA_DIR), "VERSION": "v1",
            "SCH_NODE_HEAP_MB": "1792", "SCH_BUILD_JOBS": "2", "RUNTIME_AWS_API_READ": "true",
        }
        base.update(env)
        script = (
            "set -euo pipefail\n"
            'log() { echo "==> $*"; }\n'
            f'source "{self.root / "helpers.sh"}"\n'
            "PLANE_FAILURES=0\nPLANE_SUMMARY=()\n"
            f"{body}\n"
            'echo "FAILURES=${PLANE_FAILURES}"\n'
            'for l in "${PLANE_SUMMARY[@]}"; do echo "SUMMARY=${l}"; done\n'
        )
        return subprocess.run(["bash", "-c", script], env=base, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines()

    def plane_line(self, key, entry="user:alice", status="NEW"):
        return ["PLANE", entry, "user", key, "AIDAEXAMPLEALICE0001", f"sch-dev-plane-{key}", status]

    def test_each_plane_is_one_stack_with_the_runtime_stack_configuration(self):
        self.write_plan([self.plane_line(KEY_A), self.plane_line(KEY_B, "role:bots")])
        proc = self.run_helpers(f'deploy_planes "{self.plan}"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("FAILURES=0", proc.stdout)
        deploys = [c for c in self.calls() if c.startswith("cloudformation deploy")]
        self.assertEqual(len(deploys), 2)
        first = deploys[0]
        for token in (
            f"--stack-name sch-dev-plane-{KEY_A}",
            f"--template-file {INFRA_DIR / 'user_plane.yaml'}",
            f"OwnerKey={KEY_A}", "OwnerKind=user", "OwnerUserIdPattern=AIDAEXAMPLEALICE0001",
            "PrincipalEntry=user:alice", "ApplicationVersion=v7",
            "ImageDigest=sha256:0123456789abcdef", "IdleRuntimeSessionTimeoutSeconds=1200",
            "AwsApiRead=false", "ImageRebuildProject= ",
            "SharedPolicyArns=arn:aws:iam::111122223333:policy/sch-dev-agentcore-policy,arn:aws:iam::111122223333:policy/sch-dev-runtime-cap-polly",
            "RegistryRoleArn=arn:aws:iam::111122223333:role/sch-dev-workspace-registry-role",
            "CheckpointBucket=sch-dev-checkpoints-111122223333",
            f"--tags sch:deployment=sch-dev sch:owner-key={KEY_A}",
            "--capabilities CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset",
        ):
            self.assertIn(token, first)
        self.assertEqual(sum(1 for l in proc.stdout.splitlines() if l.startswith("SUMMARY=") and l.endswith("OK")), 2)

    def test_a_failed_plane_does_not_stop_the_others(self):
        self.write_plan([self.plane_line(KEY_A), self.plane_line(KEY_B, "role:bots")])
        proc = self.run_helpers(f'deploy_planes "{self.plan}"', STUB_FAIL_STACK=f"sch-dev-plane-{KEY_A}")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("FAILURES=1", proc.stdout)
        self.assertEqual(len([c for c in self.calls() if c.startswith("cloudformation deploy")]), 2)
        self.assertIn(f"SUMMARY=user:alice\t{KEY_A}\t-\tFAILED", proc.stdout)
        self.assertIn(f"SUMMARY=role:bots\t{KEY_B}\t", proc.stdout)

    def test_orphans_are_deleted_and_waited_for(self):
        self.write_plan([self.plane_line(KEY_A), ["ORPHAN", f"sch-dev-plane-{KEY_GONE}", KEY_GONE, "UPDATE_COMPLETE"]])
        proc = self.run_helpers(f'delete_orphan_planes "{self.plan}"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertEqual(calls, [
            f"cloudformation delete-stack --stack-name sch-dev-plane-{KEY_GONE} --region eu-west-1",
            f"cloudformation wait stack-delete-complete --stack-name sch-dev-plane-{KEY_GONE} --region eu-west-1",
        ])
        self.assertIn(f"SUMMARY=(removed)\t{KEY_GONE}\t-\tDELETED", proc.stdout)

    def test_a_failed_creation_is_deleted_before_it_is_recreated(self):
        self.write_plan([self.plane_line(KEY_A, status="ROLLBACK_COMPLETE")])
        proc = self.run_helpers(f'deploy_planes "{self.plan}"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        delete = calls.index(f"cloudformation delete-stack --stack-name sch-dev-plane-{KEY_A} --region eu-west-1")
        deploy = next(i for i, c in enumerate(calls) if c.startswith("cloudformation deploy"))
        self.assertLess(delete, deploy)

    def test_missing_runtime_outputs_deploy_no_plane(self):
        stub = self.root / "bin" / "aws"
        stub.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "${STUB_LOG}"\necho None\n')
        self.write_plan([self.plane_line(KEY_A)])
        proc = self.run_helpers(f'deploy_planes "{self.plan}"')
        self.assertIn("FAILURES=1", proc.stdout)
        self.assertFalse([c for c in self.calls() if c.startswith("cloudformation deploy")])


class DeployScriptTest(unittest.TestCase):
    def setUp(self):
        self.script = DEPLOY.read_text()

    def test_every_plane_parameter_without_default_is_passed(self):
        template = cfn_render.load(cfn_render.PLANE_TEMPLATE)
        block = re.search(r"deploy_planes\(\) \{.*?^  \}", self.script, re.S | re.M).group(0)
        passed = set(re.findall(r'"(\w+)=\$\{', block))
        self.assertEqual(passed, set(template["Parameters"]))

    def test_order_preflight_bootstrap_orphans_runtime_planes(self):
        positions = [
            self.script.index("run_isolation_helper plan --out"),
            self.script.index("# --- 1. Bootstrap stack"),
            self.script.index('delete_orphan_planes "${PLANE_PLAN}"'),
            self.script.index('--stack-name "${PROJECT_NAME}-${ENVIRONMENT}-runtime" \\\n    --parameter-overrides \\\n        "ProjectName'),
            self.script.index('deploy_planes "${PLANE_PLAN}"'),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('"IsolationEnabled=${ISOLATION_ENABLED}"', self.script)
        # R13: locks are template resources, never an API call from the deploy.
        self.assertNotIn("put-resource-policy", self.script)

    def run_deploy(self, **env):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "bin").mkdir()
        stub = root / "bin" / "aws"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        log = root / "log"
        log.write_text("")
        base = {"PATH": f"{root / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "STUB_LOG": str(log), "HOME": str(root)}
        base.update(env)
        proc = subprocess.run(["bash", str(DEPLOY), "-s"], env=base, capture_output=True, text=True,
                              cwd=str(root))
        return proc, log.read_text().splitlines()

    def test_refused_combinations_stop_before_any_stack(self):
        for env, message in (
            ({"ISOLATED_PRINCIPALS": "user:alice"}, "ENABLE_WORKSPACE_REGISTRY=true"),
            ({"ISOLATED_PRINCIPALS": "user:alice", "ENABLE_WORKSPACE_REGISTRY": "true",
              "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, "Telegram is not available"),
            ({"ISOLATED_PRINCIPALS": "bogus", "ENABLE_WORKSPACE_REGISTRY": "true"}, "expected user:"),
        ):
            with self.subTest(env=env):
                proc, calls = self.run_deploy(**env)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn(message, proc.stderr)
                self.assertIn("no stack has been changed", proc.stderr)
                self.assertFalse([c for c in calls if c.startswith("cloudformation deploy")], calls)
                self.assertFalse([c for c in calls if c.startswith("iam ")], calls)

    def test_oversized_escape_hatch_stops_before_any_stack(self):
        big = json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": f"translate:A{i:04d}", "Resource": "*"} for i in range(250)]})
        proc, calls = self.run_deploy(RUNTIME_EXTRA_POLICY_JSON=big)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("at most 6144", proc.stderr)
        self.assertFalse([c for c in calls if c.startswith("cloudformation deploy")])


if __name__ == "__main__":
    unittest.main()
