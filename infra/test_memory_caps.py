"""Unit tests for the TASK-1 memory-cap deploy wiring.

Asserts the requirements of docs/specs/platform/runtime-provisioning.md R17:

- the runtime stack exposes the memory-cap defaults as parameters
  (NodeHeapMb, BuildJobs) and wires them into the runtime environment as
  SCH_NODE_HEAP_MB / SCH_BUILD_JOBS;
- infra/deploy.sh accepts the same-named env overrides, defaults them, and
  passes them through to CloudFormation;
- the defaults agree across every layer (template, deploy.sh, image ENV,
  interactive wrapper, shim) so no layer silently runs uncapped.

PyYAML is a test-only dependency (the product stays stdlib), same as
test_runtime_tuning.py.
"""

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "infra" / "agent_runtime.yaml"
DEPLOY_PATH = REPO_ROOT / "infra" / "deploy.sh"
DOCKERFILE_PATH = REPO_ROOT / "image" / "Dockerfile"
WRAPPER_PATH = REPO_ROOT / "image" / "scripts" / "harness-wrapper.sh"
SHIM_PATH = REPO_ROOT / "image" / "app" / "main.py"

EXPECTED_HEAP_MB = "1792"
EXPECTED_BUILD_JOBS = "2"


class CfnLoader(yaml.SafeLoader):
    """Loads CFN short tags (!Sub, !If, ...) as their Fn:: canonical form."""


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    prefix = "" if suffix in ("Ref", "Condition") else "Fn::"
    return {f"{prefix}{suffix}": value}


CfnLoader.add_multi_constructor("!", _cfn_tag)


def load_template():
    with open(TEMPLATE_PATH) as fh:
        return yaml.load(fh, Loader=CfnLoader)


def template_text():
    return TEMPLATE_PATH.read_text()


def deploy_text():
    return DEPLOY_PATH.read_text()


def runtime_environment_variables(template):
    """The Environment Variables mapping of the AgentCore runtime resource."""
    resources = template["Resources"]
    for resource in resources.values():
        props = resource.get("Properties", {})
        env = props.get("EnvironmentVariables") or props.get("Environment")
        if isinstance(env, dict) and "SCH_CHECKPOINT_BUCKET" in env:
            return env
    raise AssertionError("runtime resource with EnvironmentVariables not found")


class TemplateParameterTest(unittest.TestCase):
    def test_heap_and_jobs_parameters_exist_with_defaults(self):
        template = load_template()
        params = template["Parameters"]
        self.assertEqual(params["NodeHeapMb"]["Default"], 1792)
        self.assertEqual(params["NodeHeapMb"]["MinValue"], 0)
        self.assertEqual(params["BuildJobs"]["Default"], 2)
        self.assertEqual(params["BuildJobs"]["MinValue"], 1)

    def test_runtime_env_wires_parameters(self):
        template = load_template()
        env = runtime_environment_variables(template)
        self.assertEqual(env["SCH_NODE_HEAP_MB"], {"Ref": "NodeHeapMb"})
        self.assertEqual(env["SCH_BUILD_JOBS"], {"Ref": "BuildJobs"})


class DeployPassthroughTest(unittest.TestCase):
    def test_deploy_defaults_match_template(self):
        text = deploy_text()
        self.assertIn('SCH_NODE_HEAP_MB="${SCH_NODE_HEAP_MB:-1792}"', text)
        self.assertIn('SCH_BUILD_JOBS="${SCH_BUILD_JOBS:-2}"', text)

    def test_deploy_passes_parameters_to_cloudformation(self):
        text = deploy_text()
        self.assertIn('"NodeHeapMb=${SCH_NODE_HEAP_MB}"', text)
        self.assertIn('"BuildJobs=${SCH_BUILD_JOBS}"', text)


class DefaultAgreementTest(unittest.TestCase):
    def test_image_env_defaults_match(self):
        text = DOCKERFILE_PATH.read_text()
        self.assertIn(f"SCH_NODE_HEAP_MB={EXPECTED_HEAP_MB}", text)
        self.assertIn(f"SCH_BUILD_JOBS={EXPECTED_BUILD_JOBS}", text)

    def test_wrapper_defaults_match(self):
        text = WRAPPER_PATH.read_text()
        self.assertIn(
            'SCH_NODE_HEAP_MB="${SCH_NODE_HEAP_MB:-%s}"' % EXPECTED_HEAP_MB, text
        )
        self.assertIn(
            'SCH_BUILD_JOBS="${SCH_BUILD_JOBS:-%s}"' % EXPECTED_BUILD_JOBS, text
        )

    def test_shim_defaults_match(self):
        text = SHIM_PATH.read_text()
        self.assertIn(
            "NODE_HEAP_MB_DEFAULT = %s" % EXPECTED_HEAP_MB, text
        )
        self.assertIn(
            "BUILD_JOBS_DEFAULT = %s" % EXPECTED_BUILD_JOBS, text
        )


if __name__ == "__main__":
    unittest.main()
