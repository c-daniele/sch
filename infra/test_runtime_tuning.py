"""Unit tests for the runtime capability tuning (TASK-9).

Renders the AgentRuntimeRole of infra/agent_runtime.yaml under different
tuning parameter sets and asserts the requirements of
docs/specs/security/runtime-capability-tuning.md:

- with every parameter at its default, the rendered role is byte-identical
  to the pre-feature role (fixture agent_runtime_role_pre_tuning.json,
  extracted from the template before the feature existed) — spec R1/I1;
- RuntimeBedrockAccess='false' strips every Bedrock grant on both planes,
  whatever the allow-list says — spec R7/I5;
- allow-list entries are expanded verbatim into BOTH ARN forms, wildcards
  included — spec R6;
- the Mantle plane is granted only when the allow-list is empty or contains
  an OpenAI-family entry — spec R8 (the marker heuristic itself lives in
  infra/deploy.sh and is tested separately);
- each capability's action set matches the R4 table of the spec verbatim;
- the data-bucket S3 legs appear only when RuntimeDataBucketArn is set —
  spec R5;
- the escape-hatch policy exists only when RuntimeExtraPolicyJson is set,
  and its document is the raw parameter — spec R10;
- deploy.sh validates RUNTIME_CAPABILITIES and derives the boolean template
  parameters (capability flags, OpenAI-family marker, allow-list
  normalization) — spec R3/R8/R12;
- sch setup --deploy gained no deploy surface — spec R11.

Rendering evaluates the template's Conditions and Fn::If with plain
Python (conditions are only Equals/And/Or/Not over parameter values);
Fn::Sub/Fn::Split are evaluated only where a test needs the expanded
value. PyYAML is a test-only dependency (the product stays stdlib).
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "infra" / "agent_runtime.yaml"
SPEC_PATH = REPO_ROOT / "docs" / "specs" / "security" / "runtime-capability-tuning.md"
DEPLOY_PATH = REPO_ROOT / "infra" / "deploy.sh"
FIXTURE_PATH = REPO_ROOT / "infra" / "fixtures" / "agent_runtime_role_pre_tuning.json"
SETUP_PATH = REPO_ROOT / "cli" / "sch" / "commands" / "setup.py"

FAKE_ACCOUNT = "111122223333"
FAKE_REGION = "eu-test-1"


class CfnLoader(yaml.SafeLoader):
    """Loads CFN short tags (!Sub, !If, ...) as their Fn:: canonical form."""


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    # Ref and Condition have no Fn:: prefix in CloudFormation's canonical form.
    prefix = "" if suffix in ("Ref", "Condition") else "Fn::"
    return {f"{prefix}{suffix}": value}


CfnLoader.add_multi_constructor("!", _cfn_tag)


def load_template():
    with open(TEMPLATE_PATH) as fh:
        return yaml.load(fh, Loader=CfnLoader)


def default_parameters(template):
    return {
        name: spec.get("Default", "")
        for name, spec in template["Parameters"].items()
    }


def evaluate_condition(name, conditions, parameters):
    """Evaluates a named template condition to a bool."""
    if isinstance(name, dict) and "Condition" in name:
        name = name["Condition"]
    return evaluate_expression(conditions[name], conditions, parameters)


def evaluate_expression(expr, conditions, parameters, _seen=None):
    """Evaluates a condition expression (Equals/And/Or/Not, Condition refs)."""
    _seen = _seen or set()
    if isinstance(expr, dict):
        if "Condition" in expr:
            ref = expr["Condition"]
            if ref in _seen:
                raise ValueError(f"circular condition reference: {ref}")
            return evaluate_expression(
                conditions[ref], conditions, parameters, _seen | {ref}
            )
        if "Fn::Equals" in expr:
            def operand(value):
                if isinstance(value, dict):
                    if "Ref" in value:
                        ref = value["Ref"]
                        if ref == "AWS::NoValue":
                            return None
                        return parameters.get(ref)
                    raise AssertionError(f"unsupported operand: {value}")
                return value

            left, right = expr["Fn::Equals"]
            return operand(left) == operand(right)
        if "Fn::Not" in expr:
            return not evaluate_expression(expr["Fn::Not"][0], conditions, parameters, _seen)
        if "Fn::And" in expr:
            return all(
                evaluate_expression(c, conditions, parameters, _seen)
                for c in expr["Fn::And"]
            )
        if "Fn::Or" in expr:
            return any(
                evaluate_expression(c, conditions, parameters, _seen)
                for c in expr["Fn::Or"]
            )
    raise AssertionError(f"unsupported condition expression: {expr}")


NO_VALUE = object()


def resolve(value, conditions, parameters):
    """Resolves Fn::If / AWS::NoValue, leaving intrinsics (Sub, Split, Join,
    GetAtt, Refs — parameter, pseudo or resource) symbolic. Callers that need
    a value-level evaluation use evaluate_value on the specific property."""
    if isinstance(value, dict):
        if "Fn::If" in value:
            cond_name, if_true, if_false = value["Fn::If"]
            branch = if_true if evaluate_condition(cond_name, conditions, parameters) else if_false
            if branch == {"Ref": "AWS::NoValue"}:
                return NO_VALUE
            return resolve(branch, conditions, parameters)
        if value == {"Ref": "AWS::NoValue"}:
            return NO_VALUE
        resolved = {}
        for key, item in value.items():
            item_resolved = resolve(item, conditions, parameters)
            if item_resolved is NO_VALUE:
                continue
            resolved[key] = item_resolved
        return resolved
    if isinstance(value, list):
        out = []
        for item in value:
            resolved = resolve(item, conditions, parameters)
            if resolved is not NO_VALUE:
                out.append(resolved)
        return out
    return value


def render_role(template, overrides=None):
    parameters = default_parameters(template)
    parameters.update(overrides or {})
    conditions = template["Conditions"]
    role = resolve(template["Resources"]["AgentRuntimeRole"], conditions, parameters)
    return role, parameters, conditions


def render_resource(template, resource_name, overrides=None):
    parameters = default_parameters(template)
    parameters.update(overrides or {})
    conditions = template["Conditions"]
    resource = template["Resources"][resource_name]
    cond_name = resource.get("Condition")
    if cond_name and not evaluate_condition(cond_name, conditions, parameters):
        return None, parameters
    return resolve(resource, conditions, parameters), parameters


def sub_value(value, parameters):
    """Resolves Fn::Sub shorthand/mapped form with pseudo-parameters stubbed."""
    if isinstance(value, dict) and "Fn::Sub" in value:
        if isinstance(value["Fn::Sub"], str):
            body, mapping = value["Fn::Sub"], {}
        else:
            body, mapping = value["Fn::Sub"]
        variables = dict(parameters)
        variables.update(mapping)
        variables["AWS::AccountId"] = FAKE_ACCOUNT
        variables["AWS::Region"] = FAKE_REGION

        def replace(match):
            expr = match.group(1)
            if expr in variables:
                inner = variables[expr]
                if isinstance(inner, dict) and "Ref" in inner:
                    inner = variables[inner["Ref"]]
                return str(inner)
            raise AssertionError(f"unresolvable Sub variable: {expr}")

        return re.sub(r"\$\{([^}]+)\}", replace, body)
    raise AssertionError(f"expected an Fn::Sub, got: {value}")


def evaluate_value(value, parameters):
    """Resolves Fn::Join / Fn::Split / Fn::Sub to their string/list values."""
    if isinstance(value, dict):
        if "Fn::Sub" in value:
            return sub_value(value, parameters)
        if "Fn::Join" in value:
            delimiter, items = value["Fn::Join"]
            if isinstance(items, dict):
                # A single intrinsic returning the list of strings (e.g. Split).
                items = evaluate_value(items, parameters)
            return delimiter.join(
                evaluate_value(item, parameters) for item in items
            )
        if "Fn::Split" in value:
            delimiter, source = value["Fn::Split"]
            return evaluate_value(source, parameters).split(delimiter)
        if "Ref" in value and value["Ref"] in parameters:
            return parameters[value["Ref"]]
    if isinstance(value, list):
        return [evaluate_value(item, parameters) for item in value]
    return value


def statements_of(role):
    return role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]


def find_statement(role, sid):
    for statement in statements_of(role):
        if statement.get("Sid") == sid:
            return statement
    return None


class DefaultRenderingTest(unittest.TestCase):
    """R1/I1: defaults are inert — the role is byte-identical to pre-feature."""

    def test_default_role_is_byte_identical_to_prefeature(self):
        fixture = json.loads(FIXTURE_PATH.read_text())
        rendered, _, _ = render_role(load_template())
        self.assertEqual(rendered, fixture)


class BedrockAccessTest(unittest.TestCase):
    """R7/I5: access 'false' strips every Bedrock grant on both planes."""

    SCENARIOS = [
        {},
        {"RuntimeBedrockModelAllowlist": "eu.anthropic.claude-sonnet-4-6"},
        {
            "RuntimeBedrockModelAllowlist": "openai.gpt-5.5",
            "RuntimeBedrockAllowlistHasOpenAIFamily": "true",
        },
        {"RuntimeBedrockModelAllowlist": "*"},
    ]

    def test_access_false_strips_both_planes(self):
        template = load_template()
        for overrides in self.SCENARIOS:
            with self.subTest(overrides=overrides):
                params = dict(overrides)
                params["RuntimeBedrockAccess"] = "false"
                role, _, _ = render_role(template, params)
                blob = json.dumps(statements_of(role))
                self.assertNotIn("bedrock:Invoke", blob)
                self.assertNotIn("bedrock-mantle", blob)

    def test_access_true_keeps_bedrock(self):
        role, _, _ = render_role(load_template())
        self.assertIsNotNone(find_statement(role, "BedrockInferenceProfiles"))
        self.assertIsNotNone(find_statement(role, "BedrockMantleInference"))


class AllowlistTest(unittest.TestCase):
    """R6: entries inserted verbatim into BOTH ARN forms, region-wildcarded."""

    def test_expansion_is_verbatim_in_both_forms(self):
        entries = ["*anthropic.claude-sonnet*", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"]
        role, _, _ = render_role(
            load_template(),
            {
                "RuntimeBedrockModelAllowlist": ",".join(entries),
                "RuntimeBedrockAllowlistHasOpenAIFamily": "false",
            },
        )
        statement = find_statement(role, "BedrockInferenceProfilesAllowlisted")
        self.assertIsNotNone(statement)
        arns = evaluate_value(statement["Resource"], {"RuntimeBedrockModelAllowlist": ",".join(entries)})
        expected = []
        for form in ("inference-profile", "foundation-model"):
            prefix = f"arn:aws:bedrock:*:*:{form}/" if form == "inference-profile" \
                else f"arn:aws:bedrock:*::{form}/"
            expected.extend(f"{prefix}{entry}" for entry in entries)
        self.assertEqual(arns, expected)

    def test_empty_allowlist_is_the_broad_grant(self):
        role, _, _ = render_role(load_template())
        statement = find_statement(role, "BedrockInferenceProfiles")
        self.assertEqual(
            statement["Resource"],
            [
                {"Fn::Sub": "arn:aws:bedrock:*:${AWS::AccountId}:inference-profile/*"},
                {"Fn::Sub": "arn:aws:bedrock:*:${AWS::AccountId}:application-inference-profile/*"},
                "arn:aws:bedrock:*::foundation-model/*",
            ],
        )
        self.assertIsNone(find_statement(role, "BedrockInferenceProfilesAllowlisted"))


class MantleTest(unittest.TestCase):
    """R8: Mantle granted when access on AND (allowlist empty OR OpenAI marker)."""

    def test_gating_matrix(self):
        template = load_template()
        cases = [
            # (overrides, mantle expected)
            ({}, True),
            ({"RuntimeBedrockModelAllowlist": "eu.anthropic.claude-sonnet-4-6"}, False),
            ({"RuntimeBedrockModelAllowlist": "openai.gpt-5.5",
              "RuntimeBedrockAllowlistHasOpenAIFamily": "true"}, True),
            ({"RuntimeBedrockModelAllowlist": "eu.anthropic.claude-sonnet-4-6",
              "RuntimeBedrockAllowlistHasOpenAIFamily": "true"}, True),
            ({"RuntimeBedrockAccess": "false",
              "RuntimeBedrockModelAllowlist": "openai.gpt-5.5",
              "RuntimeBedrockAllowlistHasOpenAIFamily": "true"}, False),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                role, _, _ = render_role(template, overrides)
                self.assertEqual(find_statement(role, "BedrockMantleInference") is not None, expected)


class CapabilityPolicyTest(unittest.TestCase):
    """R3/R4: dedicated conditional policies matching the spec table."""

    RESOURCE_BY_CAPABILITY = {
        "transcribe": "RuntimeTranscribeCapabilityPolicy",
        "textract": "RuntimeTextractCapabilityPolicy",
        "rekognition": "RuntimeRekognitionCapabilityPolicy",
        "polly": "RuntimePollyCapabilityPolicy",
        "comprehend": "RuntimeComprehendCapabilityPolicy",
    }

    @staticmethod
    def spec_action_sets():
        """Parses the R4 table out of the normative spec (AC: no divergence)."""
        table = {}
        for line in SPEC_PATH.read_text().splitlines():
            match = re.match(r"^\|\s*`(\w+)`\s*\|(.*?)\|\s*$", line)
            if not match:
                continue
            capability, actions_cell = match.groups()
            if capability not in CapabilityPolicyTest.RESOURCE_BY_CAPABILITY:
                continue
            actions = []
            for token in re.findall(r"`([^`]+)`", actions_cell):
                actions.append(token if ":" in token else f"{capability}:{token}")
            table[capability] = actions
        return table

    def test_spec_table_covers_the_whole_catalog(self):
        self.assertEqual(set(self.spec_action_sets()), set(self.RESOURCE_BY_CAPABILITY))

    def test_disabled_capability_has_no_policy_resource(self):
        template = load_template()
        rendered, _ = render_resource(template, "RuntimeTranscribeCapabilityPolicy")
        self.assertIsNone(rendered)

    def test_enabled_capability_matches_spec_actions(self):
        template = load_template()
        expected_all = self.spec_action_sets()
        for capability, resource_name in self.RESOURCE_BY_CAPABILITY.items():
            overrides = {f"RuntimeCap{capability.capitalize()}Enabled": "true"}
            rendered, _ = render_resource(template, resource_name, overrides)
            with self.subTest(capability=capability):
                self.assertIsNotNone(rendered)
                statement = rendered["Properties"]["PolicyDocument"]["Statement"][0]
                self.assertEqual(statement["Action"], expected_all[capability])
                self.assertEqual(statement["Resource"], "*")


class DataBucketTest(unittest.TestCase):
    """R5: S3 legs only when RuntimeDataBucketArn is set."""

    def test_no_s3_legs_without_data_bucket(self):
        rendered, _ = render_resource(
            load_template(), "RuntimeTranscribeCapabilityPolicy",
            {"RuntimeCapTranscribeEnabled": "true"},
        )
        blob = json.dumps(rendered["Properties"]["PolicyDocument"])
        self.assertNotIn("s3:", blob)

    def test_s3_legs_with_data_bucket(self):
        bucket_arn = "arn:aws:s3:::my-media-bucket"
        rendered, _ = render_resource(
            load_template(), "RuntimeTranscribeCapabilityPolicy",
            {"RuntimeCapTranscribeEnabled": "true", "RuntimeDataBucketArn": bucket_arn},
        )
        statements = rendered["Properties"]["PolicyDocument"]["Statement"]
        read = [s for s in statements if s.get("Sid", "").endswith("DataBucketRead")]
        write = [s for s in statements if s.get("Sid", "").endswith("DataBucketWrite")]
        self.assertEqual(len(read), 1)
        self.assertEqual(read[0]["Action"], "s3:GetObject")
        self.assertEqual(sub_value(read[0]["Resource"], {"RuntimeDataBucketArn": bucket_arn}),
                         f"{bucket_arn}/*")
        self.assertEqual(len(write), 1)
        self.assertEqual(write[0]["Action"], "s3:PutObject")
        self.assertEqual(
            [sub_value(r, {"RuntimeDataBucketArn": bucket_arn}) for r in write[0]["Resource"]],
            [f"{bucket_arn}/transcribe/*", f"{bucket_arn}/textract/*"],
        )


class ExtraPolicyTest(unittest.TestCase):
    """R10: one extra operator-owned policy, only when the JSON is set."""

    def test_absent_by_default(self):
        rendered, _ = render_resource(load_template(), "RuntimeExtraPolicy")
        self.assertIsNone(rendered)

    def test_present_with_raw_document(self):
        document = ('{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
                    '"Action":"translate:TranslateText","Resource":"*"}]}')
        rendered, _ = render_resource(
            load_template(), "RuntimeExtraPolicy", {"RuntimeExtraPolicyJson": document},
        )
        self.assertIsNotNone(rendered)
        # The document passes through as a Ref to the parameter; CloudFormation
        # evaluates it to the raw JSON string at deploy time.
        self.assertEqual(
            rendered["Properties"]["PolicyDocument"],
            {"Ref": "RuntimeExtraPolicyJson"},
        )
        self.assertEqual(rendered["Properties"]["Roles"], [{"Ref": "AgentRuntimeRole"}])


class ReadOnlyAccessTest(unittest.TestCase):
    """R9: ReadOnlyAccess attached only when RuntimeAwsApiRead='true'."""

    def test_attached_by_default_and_detached_when_disabled(self):
        role, _, _ = render_role(load_template())
        self.assertEqual(
            role["Properties"]["ManagedPolicyArns"],
            ["arn:aws:iam::aws:policy/ReadOnlyAccess"],
        )
        role, _, _ = render_role(load_template(), {"RuntimeAwsApiRead": "false"})
        self.assertNotIn("ManagedPolicyArns", role["Properties"])


class DeployHelperTest(unittest.TestCase):
    """R3/R8/R12: deploy.sh validation + derived boolean parameters."""

    @classmethod
    def setUpClass(cls):
        # Extract the helper block into a temp file: deploy.sh is not
        # sourceable as a whole (it deploys), and macOS bash mishandles
        # `source <(sed ...)`.
        script = DEPLOY_PATH.read_text()
        match = re.search(
            r"^# Runtime capability tuning helpers.*?^# \(end runtime capability tuning helpers\)",
            script, re.S | re.M,
        )
        assert match, "runtime tuning helper block not found in deploy.sh"
        cls.helper_file = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
        cls.helper_file.write(match.group(0))
        cls.helper_file.close()

    def resolve(self, capabilities="", allowlist="", extra_env=None):
        env = {
            "PATH": "/usr/bin:/bin",
            "RUNTIME_CAPABILITIES": capabilities,
            "RUNTIME_BEDROCK_MODEL_ALLOWLIST": allowlist,
        }
        env.update(extra_env or {})
        proc = subprocess.run(
            ["bash", "-c",
             f'source "{self.helper_file_path}"; runtime_tuning_resolve'],
            env=env, capture_output=True, text=True,
        )
        return proc

    @property
    def helper_file_path(self):
        return self.helper_file.name

    def parse(self, stdout):
        out = {}
        for line in stdout.splitlines():
            key, _, value = line.partition("=")
            out[key] = value.strip("'")
        return out

    def test_valid_capabilities_produce_flags(self):
        proc = self.resolve(capabilities=" transcribe , textract ")
        self.assertEqual(proc.returncode, 0)
        values = self.parse(proc.stdout)
        self.assertEqual(values["TUNING_CAPS"], "transcribe,textract")
        self.assertEqual(values["TUNING_CAP_TRANSCRIBE"], "true")
        self.assertEqual(values["TUNING_CAP_TEXTRACT"], "true")
        self.assertEqual(values["TUNING_CAP_REKOGNITION"], "false")
        self.assertEqual(values["TUNING_CAP_POLLY"], "false")
        self.assertEqual(values["TUNING_CAP_COMPREHEND"], "false")
        self.assertEqual(values["TUNING_HAS_OPENAI"], "false")

    def test_invalid_capability_fails_fast_listing_valid_names(self):
        proc = self.resolve(capabilities="transcribe,bogus")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("bogus", proc.stderr)
        for name in ("transcribe", "textract", "rekognition", "polly", "comprehend"):
            self.assertIn(name, proc.stderr)

    def test_allowlist_normalization_and_marker(self):
        proc = self.resolve(
            allowlist=" *anthropic.claude-sonnet* , openai.gpt-5.5 , ")
        self.assertEqual(proc.returncode, 0)
        values = self.parse(proc.stdout)
        self.assertEqual(values["TUNING_ALLOWLIST"],
                         "*anthropic.claude-sonnet*,openai.gpt-5.5")
        self.assertEqual(values["TUNING_HAS_OPENAI"], "true")

    def test_marker_heuristic_is_case_insensitive_and_pattern_aware(self):
        for allowlist, expected in [
            ("eu.anthropic.claude-sonnet-4-6", "false"),
            ("*anthropic.claude-sonnet*", "false"),
            ("OpenAI.gpt-5.5", "true"),
            ("eu.openai.gpt-5.5", "true"),
            ("gpt-5.5", "true"),
        ]:
            with self.subTest(allowlist=allowlist):
                proc = self.resolve(allowlist=allowlist)
                self.assertEqual(self.parse(proc.stdout)["TUNING_HAS_OPENAI"], expected)

    def test_deploy_passes_tuning_parameters_to_cloudformation(self):
        """R11 surface: the six env vars reach the main deploy call verbatim."""
        script = DEPLOY_PATH.read_text()
        for env_var in (
            "RUNTIME_CAPABILITIES",
            "RUNTIME_BEDROCK_ACCESS",
            "RUNTIME_BEDROCK_MODEL_ALLOWLIST",
            "RUNTIME_AWS_API_READ",
            "RUNTIME_DATA_BUCKET_ARN",
            "RUNTIME_EXTRA_POLICY_JSON",
        ):
            self.assertIn(env_var, script, env_var)
        for param in (
            "RuntimeCapabilities=", "RuntimeBedrockAccess=",
            "RuntimeBedrockModelAllowlist=", "RuntimeAwsApiRead=",
            "RuntimeDataBucketArn=", "RuntimeExtraPolicyJson=",
            "RuntimeBedrockAllowlistHasOpenAIFamily=",
        ):
            self.assertIn(f'"{param}', script, param)


class SetupSurfaceTest(unittest.TestCase):
    """R11: sch setup --deploy gained no parameters and maps no env vars."""

    def test_setup_has_no_runtime_tuning_surface(self):
        source = SETUP_PATH.read_text()
        self.assertNotIn("RUNTIME_", source)
        self.assertNotIn("--capability", source)
        # The deploy passthrough is still the plain wrapper.
        self.assertIn('subprocess.run(["bash", str(script)])', source)


if __name__ == "__main__":
    unittest.main()
