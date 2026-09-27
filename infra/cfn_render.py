"""Concrete CloudFormation rendering for tests and evidence scripts.

Renders infra/agent_runtime.yaml and infra/user_plane.yaml to plain JSON
values: parameters (defaults + overrides), Conditions, Fn::If, Ref, Fn::GetAtt,
Fn::Sub (both forms, `${!Literal}` escapes), Fn::Join, Fn::Split, Fn::Select
and AWS::NoValue. Resource references resolve to deterministic fake values
(role and policy ARNs built from their names, a fake runtime ID suffix), and
the pseudo parameters to a placeholder account and region, so the rendered
policy documents can go to IAM Access Analyzer and the IAM policy simulator
as they are.

Development tool only: needs PyYAML (the `dev` extra). The product stays
stdlib-only.
"""

import json
import re
from pathlib import Path

import yaml

INFRA_DIR = Path(__file__).resolve().parent
RUNTIME_TEMPLATE = INFRA_DIR / "agent_runtime.yaml"
PLANE_TEMPLATE = INFRA_DIR / "user_plane.yaml"

FAKE_ACCOUNT = "111122223333"
FAKE_REGION = "eu-west-1"
RUNTIME_ID_SUFFIX = "AbCdEf1234"


class CfnLoader(yaml.SafeLoader):
    """Loads CFN short tags (!Sub, !If, ...) as their Fn:: canonical form."""


def _cfn_tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    prefix = "" if suffix in ("Ref", "Condition") else "Fn::"
    return {f"{prefix}{suffix}": value}


CfnLoader.add_multi_constructor("!", _cfn_tag)


def load(path):
    with open(path) as fh:
        return yaml.load(fh, Loader=CfnLoader)


_NO_VALUE = object()


class Renderer:
    def __init__(self, template, overrides=None, account=FAKE_ACCOUNT, region=FAKE_REGION):
        self.template = template
        self.account = account
        self.region = region
        self.parameters = {}
        for name, spec in template.get("Parameters", {}).items():
            value = (overrides or {}).get(name, spec.get("Default"))
            if value is None:
                raise ValueError(f"parameter {name} has no default and no value")
            if spec.get("Type") == "CommaDelimitedList" and isinstance(value, str):
                value = [item.strip() for item in value.split(",")] if value else [""]
            self.parameters[name] = value
        unknown = set(overrides or {}) - set(self.parameters)
        if unknown:
            raise ValueError(f"unknown parameters: {sorted(unknown)}")
        self._conditions = {}
        self._resources = {}

    # --- conditions -----------------------------------------------------
    def condition(self, name):
        if name not in self._conditions:
            self._conditions[name] = bool(self._eval_condition(self.template["Conditions"][name]))
        return self._conditions[name]

    def _eval_condition(self, expr):
        if isinstance(expr, dict):
            if "Condition" in expr:
                return self.condition(expr["Condition"])
            if "Fn::Equals" in expr:
                left, right = (self.value(item) for item in expr["Fn::Equals"])
                return str(left) == str(right)
            if "Fn::Not" in expr:
                return not self._eval_condition(expr["Fn::Not"][0])
            if "Fn::And" in expr:
                return all(self._eval_condition(item) for item in expr["Fn::And"])
            if "Fn::Or" in expr:
                return any(self._eval_condition(item) for item in expr["Fn::Or"])
        raise ValueError(f"unsupported condition: {expr}")

    # --- resources ------------------------------------------------------
    def exists(self, logical_id):
        resource = self.template["Resources"][logical_id]
        cond = resource.get("Condition")
        return cond is None or self.condition(cond)

    def resource(self, logical_id):
        """The resource with every property resolved, or None when absent."""
        if not self.exists(logical_id):
            return None
        if logical_id not in self._resources:
            raw = self.template["Resources"][logical_id]
            self._resources[logical_id] = {
                "Type": raw["Type"],
                "Properties": self.value(raw.get("Properties", {})),
                **{k: raw[k] for k in ("DependsOn", "DeletionPolicy", "Condition") if k in raw},
            }
        return self._resources[logical_id]

    def resources(self):
        return {
            logical_id: self.resource(logical_id)
            for logical_id in self.template["Resources"]
            if self.exists(logical_id)
        }

    def outputs(self):
        out = {}
        for name, spec in self.template.get("Outputs", {}).items():
            cond = spec.get("Condition")
            if cond and not self.condition(cond):
                continue
            out[name] = self.value(spec["Value"])
        return out

    def _ref_resource(self, logical_id):
        resource = self.resource(logical_id)
        if resource is None:
            raise ValueError(f"Ref to absent resource {logical_id}")
        props = resource["Properties"]
        kind = resource["Type"]
        if kind == "AWS::IAM::Role":
            return props.get("RoleName", logical_id)
        if kind == "AWS::IAM::ManagedPolicy":
            return f"arn:aws:iam::{self.account}:policy/{props.get('ManagedPolicyName', logical_id)}"
        if kind == "AWS::S3::Bucket":
            return props.get("BucketName", logical_id.lower())
        if kind == "AWS::DynamoDB::Table":
            return props.get("TableName", logical_id)
        if kind == "AWS::CodeBuild::Project":
            return props.get("Name", logical_id)
        if kind == "AWS::Lambda::Function":
            return props.get("FunctionName", logical_id)
        if kind == "AWS::Logs::LogGroup":
            return props.get("LogGroupName", logical_id)
        if kind == "AWS::BedrockAgentCore::Runtime":
            return f"{props['AgentRuntimeName']}-{RUNTIME_ID_SUFFIX}"
        return f"{logical_id}-ref"

    def _get_att(self, logical_id, attribute):
        resource = self.resource(logical_id)
        if resource is None:
            raise ValueError(f"GetAtt on absent resource {logical_id}")
        props = resource["Properties"]
        kind = resource["Type"]
        if kind == "AWS::IAM::Role" and attribute == "Arn":
            path = props.get("Path", "/")
            return f"arn:aws:iam::{self.account}:role{path}{props['RoleName']}"
        if kind == "AWS::S3::Bucket" and attribute == "Arn":
            return f"arn:aws:s3:::{props['BucketName']}"
        if kind == "AWS::DynamoDB::Table" and attribute == "Arn":
            return f"arn:aws:dynamodb:{self.region}:{self.account}:table/{props['TableName']}"
        if kind == "AWS::CodeBuild::Project" and attribute == "Arn":
            return f"arn:aws:codebuild:{self.region}:{self.account}:project/{props['Name']}"
        if kind == "AWS::Lambda::Function" and attribute == "Arn":
            return f"arn:aws:lambda:{self.region}:{self.account}:function:{props['FunctionName']}"
        if kind == "AWS::Events::Rule" and attribute == "Arn":
            return f"arn:aws:events:{self.region}:{self.account}:rule/{props['Name']}"
        if kind == "AWS::BedrockAgentCore::Runtime":
            runtime_id = f"{props['AgentRuntimeName']}-{RUNTIME_ID_SUFFIX}"
            if attribute == "AgentRuntimeArn":
                return f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/{runtime_id}"
            if attribute == "AgentRuntimeId":
                return runtime_id
            if attribute == "AgentRuntimeVersion":
                return "1"
        if kind == "AWS::ApiGateway::RestApi" and attribute == "RootResourceId":
            return "rootid"
        return f"{logical_id}.{attribute}"

    # --- values ---------------------------------------------------------
    def value(self, node):
        resolved = self._resolve(node)
        return None if resolved is _NO_VALUE else resolved

    def _resolve(self, node):
        if isinstance(node, list):
            out = []
            for item in node:
                resolved = self._resolve(item)
                if resolved is not _NO_VALUE:
                    out.append(resolved)
            return out
        if not isinstance(node, dict):
            return node
        if len(node) == 1:
            (key, arg), = node.items()
            if key == "Ref":
                return self._ref(arg)
            if key == "Fn::If":
                cond, if_true, if_false = arg
                return self._resolve(if_true if self.condition(cond) else if_false)
            if key == "Fn::GetAtt":
                return self._get_att(*arg)
            if key == "Fn::Sub":
                return self._sub(arg)
            if key == "Fn::Join":
                delimiter, items = arg
                items = self._resolve(items)
                return delimiter.join(str(item) for item in items)
            if key == "Fn::Split":
                delimiter, source = arg
                return str(self._resolve(source)).split(delimiter)
            if key == "Fn::Select":
                index, items = arg
                return self._resolve(items)[int(self._resolve(index))]
        out = {}
        for key, item in node.items():
            resolved = self._resolve(item)
            if resolved is not _NO_VALUE:
                out[key] = resolved
        return out

    def _ref(self, name):
        if name == "AWS::NoValue":
            return _NO_VALUE
        if name == "AWS::AccountId":
            return self.account
        if name == "AWS::Region":
            return self.region
        if name == "AWS::StackName":
            return "stack"
        if name == "AWS::Partition":
            return "aws"
        if name in self.parameters:
            return self.parameters[name]
        return self._ref_resource(name)

    def _sub(self, arg):
        if isinstance(arg, str):
            body, mapping = arg, {}
        else:
            body, mapping = arg
            mapping = {key: self.value(value) for key, value in mapping.items()}

        def replace(match):
            expr = match.group(1)
            if expr.startswith("!"):
                return "${" + expr[1:] + "}"
            if expr in mapping:
                return str(mapping[expr])
            if "." in expr and expr.split(".", 1)[0] in self.template["Resources"]:
                return str(self._get_att(*expr.split(".", 1)))
            value = self._ref(expr)
            if isinstance(value, list):
                raise ValueError(f"list parameter in Sub: {expr}")
            return str(value)

        return re.sub(r"\$\{([^}]+)\}", replace, body)


def render_runtime(overrides=None):
    return Renderer(load(RUNTIME_TEMPLATE), overrides)


def render_plane(overrides=None):
    return Renderer(load(PLANE_TEMPLATE), overrides)


def policy_json(document):
    """A policy document as a string (AgentCore locks already are strings)."""
    if isinstance(document, str):
        return json.dumps(json.loads(document))
    return json.dumps(document)


def policy_size(document):
    """IAM's size measure: characters of the JSON without whitespace."""
    return len(re.sub(r"\s+", "", policy_json(document)))
