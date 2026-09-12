"""Isolated handler tests without a DynamoDB dependency."""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("WORKSPACE_REGISTRY_TABLE", "tests")
# The handler builds its boto3 clients at import time; a client needs a region
# even though these tests never make a call. Developer machines and the SCH
# microVM usually carry one, a CI runner does not (NoRegionError on import).
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
_MODULE = Path(__file__).with_name("workspace_registry_handler.py")
_SPEC = importlib.util.spec_from_file_location("workspace_registry_handler", _MODULE)
handler = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = handler
_SPEC.loader.exec_module(handler)


class IdentityTests(unittest.TestCase):
    def test_identity_is_deterministic_safe_and_owner_scoped(self):
        a = handler._workspace_identity("owner-a", "same-name")
        self.assertEqual(a, handler._workspace_identity("owner-a", "same-name"))
        self.assertNotEqual(a, handler._workspace_identity("owner-b", "same-name"))
        self.assertRegex(a, r"^ws-[a-z2-7]+$")

    def test_owner_comes_only_from_verified_api_gateway_context(self):
        event = {"requestContext": {"identity": {"userArn": "arn:aws:iam::1:user/a"}}}
        self.assertEqual(handler._owner_id(event), handler._owner_id(event))
        with self.assertRaises(ValueError):
            handler._owner_id({"requestContext": {"identity": {}}})

    def test_invalid_workspace_name_is_rejected(self):
        with self.assertRaises(ValueError):
            handler._name({"pathParameters": {"name": "../../escape"}})

    def test_legacy_record_reconciles_to_session(self):
        record = {
            "logicalWorkspace": "ws", "runtimeSessionId": "sid", "harness": "opencode",
            "workspaceIdentity": "ws-safe",
        }
        self.assertEqual(handler._record(record)["storage"], "session")

    def test_new_record_persists_storage(self):
        record = handler._new_record("owner", "ws", "claude", "s3")
        self.assertEqual(record["storage"], "s3")
        self.assertEqual(record["sessionEpoch"], 1)

    def test_body_requires_json_object(self):
        for body in ("[]", '"text"', "null"):
            with self.assertRaises(ValueError):
                handler._body({"body": body})

    def test_explicit_empty_storage_is_invalid(self):
        with self.assertRaises(ValueError):
            handler._resolve("owner", "ws", {"storage": ""})


class FakeTable:
    def __init__(self, item):
        self.item = dict(item)
        self.updated = False

    def get_item(self, **_kwargs):
        return {"Item": dict(self.item)}

    def update_item(self, **_kwargs):
        self.updated = True
        self.item["storage"] = "session"
        self.item["sessionEpoch"] = self.item.get("sessionEpoch", 1)
        return {"Attributes": dict(self.item)}


class RegistryMigrationTests(unittest.TestCase):
    def test_legacy_storage_is_persisted_on_resolve(self):
        item = {
            "ownerId": "owner", "logicalWorkspace": "ws",
            "runtimeSessionId": "sid", "harness": "opencode",
            "workspaceIdentity": "ws-safe",
        }
        table = FakeTable(item)
        original = handler.TABLE
        handler.TABLE = table
        try:
            response = handler._resolve("owner", "ws", {"defaultStorage": "s3"})
        finally:
            handler.TABLE = original
        self.assertTrue(table.updated)
        self.assertEqual(json.loads(response["body"])["workspace"]["storage"], "session")


class DeletionStateTests(unittest.TestCase):
    def test_record_exposes_deletion_state_but_list_filters_it(self):
        item = {
            "ownerId": "owner", "logicalWorkspace": "ws", "runtimeSessionId": "sid",
            "harness": "opencode", "workspaceIdentity": "ws-safe", "deletionState": "deleting",
        }
        self.assertEqual(handler._public_record(item)["deletionState"], "deleting")
        original = handler.TABLE
        class ListTable:
            def query(self, **_kwargs):
                return {"Items": [item]}
        handler.TABLE = ListTable()
        try:
            body = json.loads(handler._list("owner")["body"])
        finally:
            handler.TABLE = original
        self.assertEqual(body["workspaces"], [])

    def test_resolve_rejects_deleting_record_pi(self):
        """add-pi-harness: the deletion guard is harness-agnostic."""
        item = {
            "ownerId": "owner", "logicalWorkspace": "ws", "runtimeSessionId": "sid",
            "harness": "pi", "workspaceIdentity": "ws-safe", "deletionState": "deleting",
        }
        original = handler.TABLE
        class ResolveTable:
            def get_item(self, **_kwargs):
                return {"Item": item}
        handler.TABLE = ResolveTable()
        try:
            response = handler._resolve("owner", "ws", {})
        finally:
            handler.TABLE = original
        self.assertEqual(response["statusCode"], 409)

    def test_resolve_rejects_deleting_record(self):
        item = {
            "ownerId": "owner", "logicalWorkspace": "ws", "runtimeSessionId": "sid",
            "harness": "opencode", "workspaceIdentity": "ws-safe", "deletionState": "deleting",
        }
        original = handler.TABLE
        class ResolveTable:
            def get_item(self, **_kwargs):
                return {"Item": item}
        handler.TABLE = ResolveTable()
        try:
            response = handler._resolve("owner", "ws", {})
        finally:
            handler.TABLE = original
        self.assertEqual(response["statusCode"], 409)


class PiHarnessEnumTests(unittest.TestCase):
    """add-pi-harness task 6.2: three-value enum + the default aligned to the
    CLI's (design D12, spec: harness-selection "Default harness coerente tra
    client e registry")."""

    def test_enum_has_exactly_the_three_supported_harnesses(self):
        self.assertEqual(handler.HARNESS_VALUES, ("opencode", "claude", "pi"))

    def test_module_default_matches_the_cli_default(self):
        cli_config = Path(__file__).parents[1] / "cli" / "sch" / "config.py"
        cli_default = None
        for line in cli_config.read_text().splitlines():
            if line.startswith("DEFAULT_HARNESS"):
                cli_default = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        self.assertIsNotNone(cli_default, "cli DEFAULT_HARNESS not found")
        self.assertEqual(handler.DEFAULT_HARNESS, cli_default)

    def test_cfn_template_default_matches_the_module_default(self):
        """The Lambda's env var wins over the module constant at runtime, so the
        template must carry the same value — otherwise aligning only the code
        would be cosmetic."""
        template = (Path(__file__).with_name("agent_runtime.yaml")).read_text()
        matches = [
            line.split(":", 1)[1].strip()
            for line in template.splitlines()
            if line.strip().startswith("DEFAULT_HARNESS:")
        ]
        self.assertEqual(matches, [handler.DEFAULT_HARNESS])

    def test_cfn_exposes_pi_runtime_configuration(self):
        template = (Path(__file__).with_name("agent_runtime.yaml")).read_text()
        deploy = (Path(__file__).with_name("deploy.sh")).read_text()
        self.assertIn("PiDefaultModel:", template)
        self.assertIn("SCH_PI_DEFAULT_MODEL: !Ref PiDefaultModel", template)
        self.assertIn('"PiDefaultModel=${SCH_PI_DEFAULT_MODEL}"', deploy)

    def test_pi_is_accepted_and_persisted_for_a_new_workspace(self):
        created = {}
        original = handler.TABLE

        class CreateTable:
            def get_item(self, **_kwargs):
                return {}

            def put_item(self, Item=None, **_kwargs):
                created.update(Item)
                return {}

        handler.TABLE = CreateTable()
        try:
            response = handler._resolve("owner", "piws", {"harness": "pi", "storage": "s3"})
        finally:
            handler.TABLE = original
        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(created["harness"], "pi")
        self.assertEqual(json.loads(response["body"])["workspace"]["harness"], "pi")

    def test_new_record_accepts_pi(self):
        record = handler._new_record("owner", "ws", "pi", "s3")
        self.assertEqual(record["harness"], "pi")

    def test_unknown_harness_is_still_rejected(self):
        with self.assertRaises(ValueError):
            handler._resolve("owner", "ws", {"harness": "bogus"})

    def test_harness_immutability_holds_for_pi(self):
        item = {
            "ownerId": "owner", "logicalWorkspace": "ws", "runtimeSessionId": "sid",
            "harness": "pi", "workspaceIdentity": "ws-safe", "storage": "s3",
            "sessionEpoch": 1,
        }
        original = handler.TABLE

        class ResolveTable:
            def get_item(self, **_kwargs):
                return {"Item": dict(item)}

        handler.TABLE = ResolveTable()
        try:
            switch = handler._resolve("owner", "ws", {"harness": "opencode"})
            congruent = handler._resolve("owner", "ws", {"harness": "pi"})
        finally:
            handler.TABLE = original
        self.assertEqual(switch["statusCode"], 409)
        self.assertIn("immutable", json.loads(switch["body"])["error"])
        self.assertEqual(congruent["statusCode"], 200)

    def test_default_is_used_when_no_harness_is_requested(self):
        created = {}
        original_table = handler.TABLE
        original_env = os.environ.pop("DEFAULT_HARNESS", None)

        class CreateTable:
            def get_item(self, **_kwargs):
                return {}

            def put_item(self, Item=None, **_kwargs):
                created.update(Item)
                return {}

        handler.TABLE = CreateTable()
        try:
            handler._resolve("owner", "dflt", {})
        finally:
            handler.TABLE = original_table
            if original_env is not None:
                os.environ["DEFAULT_HARNESS"] = original_env
        self.assertEqual(created["harness"], handler.DEFAULT_HARNESS)


if __name__ == "__main__":
    unittest.main()
