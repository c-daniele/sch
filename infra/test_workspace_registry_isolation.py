"""Registry behavior with isolation on (per-principal-isolation R36-R39).

Every AWS request the handler builds is checked against the botocore service
model: SSM, S3 and AgentCore through explicit ``ParamValidator`` calls in the
fakes, DynamoDB through a real boto3 ``Table`` whose client validates each
request (botocore's own ``ParamValidator``) before an in-memory table answers.
No request leaves the process.
"""

import hashlib
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("WORKSPACE_REGISTRY_TABLE", "tests")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3
import botocore.session
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError, ParamValidationError
from botocore.validate import ParamValidator

_MODULE = Path(__file__).with_name("workspace_registry_handler.py")
_SPEC = importlib.util.spec_from_file_location("workspace_registry_handler_isolation", _MODULE)
handler = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = handler
_SPEC.loader.exec_module(handler)

ACCOUNT = "111122223333"
REGION = "eu-west-1"
PREFIX = "/sch/dev/planes/"
BUCKET = "sch-dev-checkpoints-" + ACCOUNT
REGISTRY_ARN = "arn:aws:lambda:{}:{}:function:sch-dev-workspace-registry".format(REGION, ACCOUNT)
SESSION = botocore.session.get_session()
VALIDATOR = ParamValidator()


def owner_id(owner):
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def plane_value(owner):
    key = owner_id(owner)[:16]
    return json.dumps({
        "schemaVersion": 1, "ownerKey": key, "ownerPrefix": "o." + key,
        "runtimeArn": "arn:aws:bedrock-agentcore:{}:{}:runtime/sch_dev_o_{}-AbCdEf1234".format(
            REGION, ACCOUNT, key),
        "accessRoleArn": "arn:aws:iam::{}:role/sch-dev-o-{}-access".format(ACCOUNT, key),
    })


ALICE = {"userArn": "arn:aws:iam::{}:user/alice".format(ACCOUNT),
         "user": "AIDAEXAMPLEALICE0001", "accountId": ACCOUNT}
BOB = {"userArn": "arn:aws:sts::{}:assumed-role/AWSReservedSSO_Developers_0123456789abcdef/bob@example.com".format(ACCOUNT),
       "user": "AROAEXAMPLEDEVS00001:bob@example.com", "accountId": ACCOUNT}
DAVE = {"userArn": "arn:aws:sts::{}:assumed-role/AWSReservedSSO_Developers_0123456789abcdef/dave@example.com".format(ACCOUNT),
        "user": "AROAEXAMPLEDEVS00001:dave@example.com", "accountId": ACCOUNT}
BOT = {"userArn": "arn:aws:sts::{}:assumed-role/sch-bot/run-1".format(ACCOUNT),
       "user": "AROAEXAMPLEBOTS00001:run-1", "accountId": ACCOUNT}
CAROL = {"userArn": "arn:aws:iam::{}:user/team/carol".format(ACCOUNT),
         "user": "AIDAEXAMPLECAROL0001", "accountId": ACCOUNT}

OWNERS = {
    "alice": "user:AIDAEXAMPLEALICE0001",
    "bob": "sso:AROAEXAMPLEDEVS00001:bob@example.com",
    "dave": "sso:AROAEXAMPLEDEVS00001:dave@example.com",
    "bot": "role:AROAEXAMPLEBOTS00001",
}


def _validate(service, operation, params):
    shape = SESSION.get_service_model(service).operation_model(operation).input_shape
    report = VALIDATOR.validate(params, shape)
    if report.has_errors():
        raise AssertionError("{}.{}: {}".format(service, operation, report.generate_report()))


def _client_error(code, operation):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


class FakeSsm:
    def __init__(self, parameters):
        self.parameters = dict(parameters)
        self.calls = []

    def get_parameter(self, **kwargs):
        _validate("ssm", "GetParameter", kwargs)
        self.calls.append(kwargs["Name"])
        if kwargs["Name"] not in self.parameters:
            raise _client_error("ParameterNotFound", "GetParameter")
        return {"Parameter": {"Name": kwargs["Name"], "Value": self.parameters[kwargs["Name"]]}}


class FakeS3:
    def __init__(self, keys=()):
        self.keys = set(keys)
        self.lists = []
        self.deleted = []

    def list_object_versions(self, **kwargs):
        _validate("s3", "ListObjectVersions", kwargs)
        self.lists.append(kwargs["Prefix"])
        return {"Versions": [{"Key": key, "VersionId": "v1"} for key in sorted(self.keys)
                             if key.startswith(kwargs["Prefix"])], "IsTruncated": False}

    def delete_objects(self, **kwargs):
        _validate("s3", "DeleteObjects", kwargs)
        for obj in kwargs["Delete"]["Objects"]:
            self.keys.discard(obj["Key"])
            self.deleted.append(obj["Key"])
        return {}


class FakeRuntime:
    def __init__(self):
        self.stops = []

    def stop_runtime_session(self, **kwargs):
        _validate("bedrock-agentcore", "StopRuntimeSession", kwargs)
        self.stops.append(kwargs)
        return {}


class MemoryDynamo:
    """In-memory table behind a real, validating boto3 DynamoDB client."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self._params = None
        self.table = boto3.resource(
            "dynamodb", region_name=REGION, aws_access_key_id="testing",
            aws_secret_access_key="testing").Table("tests")
        events = self.table.meta.client.meta.events
        events.register("before-parameter-build.dynamodb.*", self._capture)
        events.register_first("before-call.dynamodb.*", self._answer)

    def _capture(self, params, model, **_kwargs):
        self._params = (model.name, params)

    @staticmethod
    def _plain(item):
        return {k: TypeDeserializer().deserialize(v) for k, v in item.items()}

    @staticmethod
    def _wire(item):
        return {k: TypeSerializer().serialize(v) for k, v in item.items()}

    def _answer(self, model, **_kwargs):
        name, params = self._params
        self.calls.append(name)
        values = self._plain(params.get("ExpressionAttributeValues", {}))
        if name == "GetItem":
            key = self._key(params["Key"])
            body = {"Item": self._wire(self.items[key])} if key in self.items else {}
        elif name == "PutItem":
            item = self._plain(params["Item"])
            key = (item["ownerId"], item["logicalWorkspace"])
            if key in self.items:
                return self._error("ConditionalCheckFailedException", model)
            self.items[key] = item
            body = {}
        elif name == "Query":
            owner = values[":v0"] if ":v0" in values else list(values.values())[0]
            body = {"Items": [self._wire(i) for k, i in sorted(self.items.items()) if k[0] == owner]}
        elif name == "UpdateItem":
            key = self._key(params["Key"])
            item = self.items.get(key)
            expression = params["UpdateExpression"]
            condition = params.get("ConditionExpression", "")
            if "deletionState" in condition and "attribute_not_exists(deletionState)" in condition:
                if item is None or "deletionState" in item:
                    return self._error("ConditionalCheckFailedException", model)
            if expression.startswith("SET deletionState"):
                item["deletionState"] = values[":state"]
            elif expression.startswith("SET runtimeSessionId"):
                item["runtimeSessionId"] = values[":sid"]
                item["sessionEpoch"] = item.get("sessionEpoch", 0) + 1
            item["updatedAt"] = values[":now"]
            body = {"Attributes": self._wire(item)}
        elif name == "DeleteItem":
            self.items.pop(self._key(params["Key"]), None)
            body = {}
        else:  # pragma: no cover - the handler uses no other operation
            raise AssertionError("unexpected DynamoDB operation " + name)
        return self._ok(body)

    def _key(self, key):
        plain = self._plain(key)
        return (plain["ownerId"], plain["logicalWorkspace"])

    @staticmethod
    def _ok(body):
        return AWSResponse("https://dynamodb", 200, {}, None), body

    @staticmethod
    def _error(code, model):
        return (AWSResponse("https://dynamodb", 400, {}, None),
                {"Error": {"Code": code, "Message": code},
                 "ResponseMetadata": {"HTTPStatusCode": 400}})


class Context:
    invoked_function_arn = REGISTRY_ARN


class IsolationRegistryTest(unittest.TestCase):
    def setUp(self):
        planes = {PREFIX + owner_id(o)[:16]: plane_value(o)
                  for n, o in OWNERS.items() if n != "dave"}
        planes[PREFIX + owner_id(OWNERS["dave"])[:16]] = plane_value(OWNERS["dave"])
        self.ssm = FakeSsm(planes)
        self.s3 = FakeS3()
        self.runtime = FakeRuntime()
        self.dynamo = MemoryDynamo()
        env = {"ISOLATION_ENABLED": "true", "PLANE_PARAMETER_PREFIX": PREFIX,
               "CHECKPOINT_BUCKET": BUCKET,
               "RUNTIME_ARN": "arn:aws:bedrock-agentcore:{}:{}:runtime/sch_dev_shared-AbCdEf1234".format(REGION, ACCOUNT)}
        patches = [mock.patch.dict(os.environ, env),
                   mock.patch.object(handler, "SSM", self.ssm),
                   mock.patch.object(handler, "S3", self.s3),
                   mock.patch.object(handler, "RUNTIME", self.runtime),
                   mock.patch.object(handler, "TABLE", self.dynamo.table),
                   mock.patch.object(handler, "_PLANE_CACHE", {})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def call(self, identity, method, resource, name=None, body=None):
        event = {"httpMethod": method, "resource": resource,
                 "requestContext": {"accountId": ACCOUNT, "identity": dict(identity)}}
        if name is not None:
            event["pathParameters"] = {"name": name}
        if body is not None:
            event["body"] = json.dumps(body)
        response = handler.handler(event, Context())
        return response["statusCode"], json.loads(response["body"])

    def resolve(self, identity, name="ws", body=None):
        return self.call(identity, "POST", "/workspaces/{name}/resolve", name, body or {})

    # --- R37 --------------------------------------------------------------

    def test_unlisted_iam_user_gets_403_naming_the_entry_and_no_table_access(self):
        status, body = self.resolve(CAROL)
        self.assertEqual(status, 403)
        self.assertIn("add user:carol to ISOLATED_PRINCIPALS", body["error"])
        self.assertIn(CAROL["userArn"], body["error"])
        self.assertEqual(self.dynamo.calls, [])
        self.assertEqual(self.dynamo.items, {})

    def test_unlisted_sso_user_and_role_session_get_their_entry_forms(self):
        eve = {"userArn": "arn:aws:sts::{}:assumed-role/AWSReservedSSO_Ops_fedcba9876543210/eve@example.com".format(ACCOUNT),
               "user": "AROAEXAMPLEOPSS00001:eve@example.com", "accountId": ACCOUNT}
        status, body = self.resolve(eve)
        self.assertEqual(status, 403)
        self.assertIn("add sso:Ops/eve@example.com to", body["error"])
        ci = {"userArn": "arn:aws:sts::{}:assumed-role/ci-runner/job-7".format(ACCOUNT),
              "user": "AROAEXAMPLECIRU00001:job-7", "accountId": ACCOUNT}
        status, body = self.call(ci, "GET", "/workspaces")
        self.assertEqual(status, 403)
        self.assertIn("add role:ci-runner to", body["error"])
        self.assertEqual(self.dynamo.calls, [])

    def test_every_operation_refuses_an_unlisted_caller(self):
        for method, resource, name in (("GET", "/workspaces", None), ("DELETE", "/workspaces", None),
                                       ("POST", "/workspaces/{name}/rotate-session", "ws"),
                                       ("DELETE", "/workspaces/{name}", "ws")):
            status, _ = self.call(CAROL, method, resource, name)
            self.assertEqual(status, 403, (method, resource))
        self.assertEqual(self.dynamo.calls, [])
        self.assertEqual(self.runtime.stops, [])

    def test_caller_of_another_account_is_refused(self):
        foreign = dict(ALICE, accountId="444455556666")
        status, body = self.resolve(foreign)
        self.assertEqual(status, 403)
        self.assertIn("not in the deployment account", body["error"])
        self.assertEqual(self.ssm.calls, [])
        self.assertEqual(self.dynamo.calls, [])

    def test_missing_principal_id_is_a_bad_request(self):
        status, _ = self.resolve({"userArn": ALICE["userArn"], "accountId": ACCOUNT})
        self.assertEqual(status, 400)
        self.assertEqual(self.dynamo.calls, [])

    # --- R36/R38 ----------------------------------------------------------

    def test_listed_user_gets_a_record_keyed_by_bound_identity_with_its_plane(self):
        status, body = self.resolve(ALICE, body={"harness": "opencode", "storage": "s3"})
        self.assertEqual(status, 201)
        expected = json.loads(plane_value(OWNERS["alice"]))
        plane = {"runtimeArn": expected["runtimeArn"], "accessRoleArn": expected["accessRoleArn"],
                 "ownerPrefix": expected["ownerPrefix"]}
        self.assertTrue(body["isolation"])
        self.assertEqual(body["plane"], plane)
        self.assertEqual(body["workspace"]["plane"], plane)
        (key, item), = self.dynamo.items.items()
        self.assertEqual(key[0], owner_id(OWNERS["alice"]))
        self.assertEqual(item["ownerPrefix"], expected["ownerPrefix"])
        self.assertEqual(item["workspaceIdentity"],
                         handler._workspace_identity(owner_id(OWNERS["alice"]), "ws"))

    def test_two_identity_center_users_of_one_permission_set_are_two_owners(self):
        _, bob = self.resolve(BOB)
        _, dave = self.resolve(DAVE)
        owners = {key[0] for key in self.dynamo.items}
        self.assertEqual(owners, {owner_id(OWNERS["bob"]), owner_id(OWNERS["dave"])})
        self.assertNotEqual(bob["plane"], dave["plane"])
        self.assertNotEqual(bob["workspace"]["workspaceIdentity"], dave["workspace"]["workspaceIdentity"])
        _, listed = self.call(BOB, "GET", "/workspaces")
        self.assertEqual([w["runtimeSessionId"] for w in listed["workspaces"]],
                         [bob["workspace"]["runtimeSessionId"]])

    def test_new_session_name_of_an_iam_user_keeps_the_owner(self):
        _, first = self.resolve(ALICE)
        # Same IAM user, new credentials: the ARN is the same, the principal
        # ID is the same unique ID. Also a user seen through another path.
        renamed = dict(ALICE, userArn="arn:aws:iam::{}:user/engineering/alice".format(ACCOUNT))
        status, second = self.resolve(renamed)
        self.assertEqual(status, 200)
        self.assertFalse(second["created"])
        self.assertEqual(first["workspace"], second["workspace"])

    def test_new_session_name_of_a_role_keeps_the_owner(self):
        _, first = self.resolve(BOT)
        again = {"userArn": "arn:aws:sts::{}:assumed-role/sch-bot/botocore-session-1790000000".format(ACCOUNT),
                 "user": "AROAEXAMPLEBOTS00001:botocore-session-1790000000", "accountId": ACCOUNT}
        status, second = self.resolve(again)
        self.assertEqual(status, 200)
        self.assertEqual(first["workspace"], second["workspace"])
        self.assertEqual({key[0] for key in self.dynamo.items}, {owner_id(OWNERS["bot"])})

    def test_a_role_session_name_never_becomes_a_separate_sso_owner(self):
        # The sso: candidate of a role session is tried first; with no such
        # plane the role owner wins, whatever the session name is.
        tricky = dict(BOT, user="AROAEXAMPLEBOTS00001:bob@example.com")
        _, body = self.resolve(tricky)
        self.assertEqual(body["plane"]["ownerPrefix"], "o." + owner_id(OWNERS["bot"])[:16])
        self.assertEqual(self.ssm.calls[:2], [
            PREFIX + owner_id("sso:AROAEXAMPLEBOTS00001:bob@example.com")[:16],
            PREFIX + owner_id(OWNERS["bot"])[:16]])

    def test_plane_lookups_are_cached_for_at_most_sixty_seconds(self):
        clock = [1000.0]
        key = owner_id(OWNERS["alice"])[:16]
        with mock.patch.object(handler.time, "monotonic", lambda: clock[0]):
            self.resolve(ALICE)
            self.resolve(ALICE)
            self.assertEqual(self.ssm.calls, [PREFIX + key])
            clock[0] += 59
            self.resolve(ALICE)
            self.assertEqual(len(self.ssm.calls), 1)
            clock[0] += 2
            self.resolve(ALICE)
            self.assertEqual(len(self.ssm.calls), 2)
            # Misses are cached as well: carol's candidates are looked up once.
            self.resolve(CAROL)
            self.resolve(CAROL)
            self.assertEqual(len(self.ssm.calls), 3)

    def test_malformed_plane_parameter_fails_closed(self):
        key = owner_id(OWNERS["alice"])[:16]
        bad = json.loads(plane_value(OWNERS["alice"]))
        bad["ownerPrefix"] = "o." + owner_id(OWNERS["bob"])[:16]
        self.ssm.parameters[PREFIX + key] = json.dumps(bad)
        status, _ = self.resolve(ALICE)
        self.assertEqual(status, 500)
        self.assertEqual(self.dynamo.calls, [])

    def test_ssm_errors_other_than_not_found_fail_closed(self):
        def denied(**_kwargs):
            raise _client_error("AccessDeniedException", "GetParameter")
        self.ssm.get_parameter = denied
        status, _ = self.resolve(ALICE)
        self.assertEqual(status, 500)
        self.assertEqual(self.dynamo.calls, [])

    def test_rotate_and_immutability_conflicts_carry_the_plane(self):
        _, created = self.resolve(ALICE, body={"harness": "opencode"})
        status, rotated = self.call(ALICE, "POST", "/workspaces/{name}/rotate-session", "ws")
        self.assertEqual(status, 200)
        self.assertTrue(rotated["isolation"])
        self.assertEqual(rotated["workspace"]["plane"], created["plane"])
        self.assertEqual(rotated["workspace"]["sessionEpoch"], 2)
        status, conflict = self.resolve(ALICE, body={"harness": "claude"})
        self.assertEqual(status, 409)
        self.assertEqual(conflict["workspace"]["plane"], created["plane"])

    # --- R38 deletion -------------------------------------------------------

    def test_delete_stops_on_the_plane_runtime_and_purges_owner_prefixed_keys(self):
        _, created = self.resolve(ALICE)
        prefix = created["plane"]["ownerPrefix"]
        identity = created["workspace"]["workspaceIdentity"]
        mine = {"checkpoints/{}/{}/manifest.json".format(prefix, identity),
                "checkpoints/{}/{}/task-status.json".format(prefix, identity),
                "checkpoint-generations/{}/{}/g1/manifest.json".format(prefix, identity),
                "workspace-writers/{}/{}.json".format(prefix, identity),
                "builds/{}/{}/source.zip".format(prefix, identity)}
        others = {"checkpoints/{}/manifest.json".format(identity),
                  "workspace-writers/{}.json".format(identity),
                  "checkpoints/{}/{}x/manifest.json".format(prefix, identity),
                  "checkpoints/o.{}/{}/manifest.json".format(owner_id(OWNERS["bob"])[:16], identity)}
        self.s3.keys = mine | others
        status, body = self.call(ALICE, "DELETE", "/workspaces/{name}", "ws")
        self.assertEqual(status, 200)
        self.assertTrue(body["deleted"])
        self.assertEqual(self.runtime.stops, [{
            "agentRuntimeArn": created["plane"]["runtimeArn"],
            "runtimeSessionId": created["workspace"]["runtimeSessionId"]}])
        self.assertEqual(set(self.s3.deleted), mine)
        self.assertEqual(self.s3.keys, others)
        self.assertEqual(self.dynamo.items, {})

    def test_bulk_delete_uses_the_plane_for_every_record(self):
        self.resolve(ALICE, "a")
        self.resolve(ALICE, "b")
        self.resolve(BOB, "a")
        status, body = self.call(ALICE, "DELETE", "/workspaces")
        self.assertEqual(status, 200)
        self.assertEqual(body["deleted"], 2)
        self.assertTrue(body["isolation"])
        arns = {stop["agentRuntimeArn"] for stop in self.runtime.stops}
        self.assertEqual(arns, {json.loads(plane_value(OWNERS["alice"]))["runtimeArn"]})
        self.assertEqual({key[0] for key in self.dynamo.items}, {owner_id(OWNERS["bob"])})

    def test_list_of_an_owner_without_records_still_names_the_plane(self):
        status, body = self.call(DAVE, "GET", "/workspaces")
        self.assertEqual(status, 200)
        self.assertEqual(body["workspaces"], [])
        self.assertTrue(body["isolation"])
        self.assertEqual(body["plane"]["ownerPrefix"], "o." + owner_id(OWNERS["dave"])[:16])

    def test_validation_is_active_on_the_dynamodb_client(self):
        with self.assertRaises(ParamValidationError):
            self.dynamo.table.get_item(Key="not-a-map")


class IsolationOffTest(unittest.TestCase):
    """R9: without ISOLATION_ENABLED the owner and responses are unchanged."""

    def setUp(self):
        self.dynamo = MemoryDynamo()
        self.ssm = FakeSsm({})
        self.runtime = FakeRuntime()
        self.s3 = FakeS3()
        env = {"CHECKPOINT_BUCKET": BUCKET,
               "RUNTIME_ARN": "arn:aws:bedrock-agentcore:{}:{}:runtime/sch_dev_shared-AbCdEf1234".format(REGION, ACCOUNT)}
        patches = [mock.patch.dict(os.environ, env),
                   mock.patch.object(handler, "SSM", self.ssm),
                   mock.patch.object(handler, "S3", self.s3),
                   mock.patch.object(handler, "RUNTIME", self.runtime),
                   mock.patch.object(handler, "TABLE", self.dynamo.table)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("ISOLATION_ENABLED", None)
        os.environ.pop("PLANE_PARAMETER_PREFIX", None)

    def call(self, identity, method, resource, name=None, body=None):
        event = {"httpMethod": method, "resource": resource,
                 "requestContext": {"accountId": ACCOUNT, "identity": dict(identity)}}
        if name is not None:
            event["pathParameters"] = {"name": name}
        if body is not None:
            event["body"] = json.dumps(body)
        response = handler.handler(event, Context())
        return response["statusCode"], json.loads(response["body"])

    def test_owner_is_sha256_of_the_caller_arn_and_responses_have_no_plane(self):
        status, body = self.call(CAROL, "POST", "/workspaces/{name}/resolve", "ws",
                                 {"harness": "pi", "storage": "session"})
        self.assertEqual(status, 201)
        self.assertEqual(set(body), {"workspace", "created"})
        self.assertEqual(set(body["workspace"]), {"logicalWorkspace", "runtimeSessionId", "harness",
                                                  "workspaceIdentity", "storage", "sessionEpoch"})
        (key, item), = self.dynamo.items.items()
        self.assertEqual(key[0], hashlib.sha256(CAROL["userArn"].encode()).hexdigest())
        self.assertNotIn("ownerPrefix", item)
        self.assertEqual(self.ssm.calls, [])
        status, listed = self.call(CAROL, "GET", "/workspaces")
        self.assertEqual(set(listed), {"workspaces"})
        status, rotated = self.call(CAROL, "POST", "/workspaces/{name}/rotate-session", "ws")
        self.assertEqual(set(rotated), {"workspace"})

    def test_delete_uses_the_shared_runtime_and_flat_keys(self):
        _, created = self.call(CAROL, "POST", "/workspaces/{name}/resolve", "ws", {})
        identity = created["workspace"]["workspaceIdentity"]
        self.s3.keys = {"checkpoints/{}/manifest.json".format(identity),
                        "workspace-writers/{}.json".format(identity)}
        status, body = self.call(CAROL, "DELETE", "/workspaces/{name}", "ws")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"workspace", "deleted"})
        self.assertEqual(self.runtime.stops[0]["agentRuntimeArn"], os.environ["RUNTIME_ARN"])
        self.assertEqual(self.s3.lists[:3], ["checkpoints/{}/".format(identity),
                                             "checkpoint-generations/{}/".format(identity),
                                             "workspace-writers/{}.json".format(identity)])
        self.assertEqual(self.s3.keys, set())


if __name__ == "__main__":
    unittest.main()
