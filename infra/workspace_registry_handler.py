"""IAM-authenticated workspace registry Lambda handler.

API Gateway supplies the authenticated caller ARN in requestContext; clients
never provide an owner identifier. The stored owner key is a hash so principal
details never become DynamoDB keys or runtime path components.
"""

import base64
import hashlib
import json
import os
import re
import time
import uuid

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE = boto3.resource("dynamodb").Table(os.environ["WORKSPACE_REGISTRY_TABLE"])
S3 = boto3.client("s3")
RUNTIME = boto3.client("bedrock-agentcore")
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# add-pi-harness (task 6.1): `pi` is the third valid harness. This enum must
# stay in sync with cli/sch/harness.py VALID_HARNESSES — the registry is
# authoritative for workspace creation when configured, so a value the CLI
# accepts and the registry rejects would make `--harness pi` fail only for
# deployments with the registry enabled.
HARNESS_VALUES = ("opencode", "claude", "pi")
# add-pi-harness (task 6.1, design D12): the default for a workspace created
# without an explicit harness. MUST equal the CLI's own default (`opencode`,
# cli/sch/config.py) — it used to be `claude` here, so the effective harness of
# a new workspace depended on which component materialized it first (spec:
# harness-selection, "Default harness coerente tra client e registry").
# In practice the CLI always sends an explicit harness, which is exactly why the
# divergence was a latent trap rather than a visible bug.
DEFAULT_HARNESS = "opencode"
STORAGE_VALUES = ("s3", "session")


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _owner_id(event):
    identity = event.get("requestContext", {}).get("identity", {})
    caller_arn = identity.get("userArn")
    if not caller_arn:
        raise ValueError("verified caller identity is missing")
    return hashlib.sha256(caller_arn.encode("utf-8")).hexdigest()


def _workspace_identity(owner_id, name):
    digest = hashlib.sha256((owner_id + "\0" + name).encode("utf-8")).digest()
    return "ws-" + base64.b32encode(digest).decode("ascii").lower().rstrip("=")[:40]


def _name(event):
    name = event.get("pathParameters", {}).get("name", "")
    if not WORKSPACE_RE.fullmatch(name):
        raise ValueError("invalid workspace name")
    return name


def _body(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _storage_value(item):
    if "storage" not in item:
        return "session"
    value = item["storage"]
    if not isinstance(value, str) or value not in STORAGE_VALUES:
        raise ValueError("workspace has invalid storage metadata")
    return value


def _epoch_value(item):
    value = item.get("sessionEpoch", 1)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("workspace has invalid session epoch metadata")
    return value


def _record(item):
    return {
        "logicalWorkspace": item["logicalWorkspace"],
        "runtimeSessionId": item["runtimeSessionId"],
        "harness": item["harness"],
        "workspaceIdentity": item["workspaceIdentity"],
        "storage": _storage_value(item),
        "sessionEpoch": _epoch_value(item),
    }


def _public_record(item):
    record = _record(item)
    if item.get("deletionState") == "deleting":
        record["deletionState"] = "deleting"
    return record


def _new_record(owner_id, name, harness, storage):
    now = int(time.time())
    return {
        "ownerId": owner_id,
        "logicalWorkspace": name,
        "runtimeSessionId": "sch-registry-{}".format(uuid.uuid4()),
        "harness": harness,
        "workspaceIdentity": _workspace_identity(owner_id, name),
        "storage": storage,
        "sessionEpoch": 1,
        "createdAt": now,
        "updatedAt": now,
        "schemaVersion": 1,
    }


def _reconcile_storage(owner_id, name, item):
    if "storage" in item and "sessionEpoch" in item:
        _storage_value(item)
        _epoch_value(item)
        return item
    response = TABLE.update_item(
        Key={"ownerId": owner_id, "logicalWorkspace": name},
        UpdateExpression=(
            "SET storage = if_not_exists(storage, :storage), "
            "sessionEpoch = if_not_exists(sessionEpoch, :epoch), updatedAt = :now"
        ),
        ExpressionAttributeValues={
            ":storage": "session", ":epoch": 1, ":now": int(time.time()),
        },
        ReturnValues="ALL_NEW",
    )
    return response["Attributes"]


def _resolve(owner_id, name, body):
    if "harness" in body and (
        not isinstance(body["harness"], str) or not body["harness"]
    ):
        raise ValueError("invalid harness")
    if "storage" in body and (
        not isinstance(body["storage"], str) or not body["storage"]
    ):
        raise ValueError("invalid storage")
    if "defaultStorage" in body and (
        not isinstance(body["defaultStorage"], str) or not body["defaultStorage"]
    ):
        raise ValueError("invalid default storage")
    requested_harness = body.get("harness") or os.environ.get(
        "DEFAULT_HARNESS", DEFAULT_HARNESS
    )
    requested_storage = (
        body.get("storage") or body.get("defaultStorage")
        or os.environ.get("DEFAULT_STORAGE", "s3")
    )
    if requested_harness not in HARNESS_VALUES:
        raise ValueError("invalid harness")
    if requested_storage not in STORAGE_VALUES:
        raise ValueError("invalid storage")
    existing = TABLE.get_item(Key={"ownerId": owner_id, "logicalWorkspace": name}).get("Item")
    if existing:
        if existing.get("deletionState") == "deleting":
            return _response(409, {"error": "workspace is being deleted"})
        existing = _reconcile_storage(owner_id, name, existing)
        if body.get("harness") and existing["harness"] != requested_harness:
            return _response(409, {"error": "workspace harness is immutable", "workspace": _record(existing)})
        existing_storage = _storage_value(existing)
        if body.get("storage") and existing_storage != requested_storage:
            return _response(409, {"error": "workspace storage is immutable", "workspace": _record(existing)})
        return _response(200, {"workspace": _record(existing), "created": False})
    item = _new_record(owner_id, name, requested_harness, requested_storage)
    try:
        TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(ownerId) AND attribute_not_exists(logicalWorkspace)")
        return _response(201, {"workspace": _record(item), "created": True})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = TABLE.get_item(Key={"ownerId": owner_id, "logicalWorkspace": name}).get("Item")
        if not existing:
            raise
        existing = _reconcile_storage(owner_id, name, existing)
        if body.get("harness") and existing["harness"] != requested_harness:
            return _response(409, {"error": "workspace harness is immutable", "workspace": _record(existing)})
        existing_storage = _storage_value(existing)
        if body.get("storage") and existing_storage != requested_storage:
            return _response(409, {"error": "workspace storage is immutable", "workspace": _record(existing)})
        return _response(200, {"workspace": _record(existing), "created": False})


def _list(owner_id):
    response = TABLE.query(KeyConditionExpression=Key("ownerId").eq(owner_id))
    items = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = TABLE.query(
            KeyConditionExpression=Key("ownerId").eq(owner_id),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))
    return _response(200, {"workspaces": [_public_record(item) for item in items
                                           if item.get("deletionState") != "deleting"]})


def _rotate(owner_id, name):
    try:
        response = TABLE.update_item(
            Key={"ownerId": owner_id, "logicalWorkspace": name},
            UpdateExpression=(
                "SET runtimeSessionId = :sid, updatedAt = :now, "
                "sessionEpoch = if_not_exists(sessionEpoch, :zero) + :one"
            ),
            ConditionExpression="attribute_exists(ownerId) AND attribute_not_exists(deletionState)",
            ExpressionAttributeValues={
                ":sid": "sch-registry-{}".format(uuid.uuid4()),
                ":now": int(time.time()), ":zero": 0, ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            item = TABLE.get_item(Key={"ownerId": owner_id, "logicalWorkspace": name}).get("Item")
            if item and item.get("deletionState") == "deleting":
                return _response(409, {"error": "workspace is being deleted"})
            return _response(404, {"error": "unknown workspace"})
        raise
    return _response(200, {"workspace": _record(response["Attributes"])})


def _purge(identity):
    bucket = os.environ["CHECKPOINT_BUCKET"]
    prefixes = ["checkpoints/{}/".format(identity),
                "checkpoint-generations/{}/".format(identity),
                "workspace-writers/{}.json".format(identity)]
    for prefix in prefixes:
        key_marker = version_marker = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix}
            if key_marker:
                kwargs["KeyMarker"] = key_marker
            if version_marker:
                kwargs["VersionIdMarker"] = version_marker
            page = S3.list_object_versions(**kwargs)
            objects = [{"Key": x["Key"], "VersionId": x["VersionId"]}
                       for field in ("Versions", "DeleteMarkers")
                       for x in page.get(field, [])
                       if x["Key"] == prefix or (prefix.endswith("/") and x["Key"].startswith(prefix))]
            for start in range(0, len(objects), 1000):
                S3.delete_objects(Bucket=bucket, Delete={"Objects": objects[start:start + 1000], "Quiet": True})
            if not page.get("IsTruncated"):
                break
            key_marker = page.get("NextKeyMarker")
            version_marker = page.get("NextVersionIdMarker")
    for prefix in prefixes:
        page = S3.list_object_versions(Bucket=bucket, Prefix=prefix)
        if any(x["Key"] == prefix or (prefix.endswith("/") and x["Key"].startswith(prefix))
               for field in ("Versions", "DeleteMarkers") for x in page.get(field, [])):
            raise RuntimeError("S3 verification found remaining objects")


def _delete(owner_id, name):
    item = TABLE.get_item(Key={"ownerId": owner_id, "logicalWorkspace": name}).get("Item")
    if not item:
        return _response(404, {"error": "unknown workspace"})
    if item.get("deletionState") != "deleting":
        try:
            item = TABLE.update_item(
                Key={"ownerId": owner_id, "logicalWorkspace": name},
                UpdateExpression="SET deletionState = :state, updatedAt = :now",
                ConditionExpression="attribute_not_exists(deletionState)",
                ExpressionAttributeValues={":state": "deleting", ":now": int(time.time())},
                ReturnValues="ALL_NEW")["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                item = TABLE.get_item(Key={"ownerId": owner_id, "logicalWorkspace": name}).get("Item")
            else:
                raise
    try:
        try:
            RUNTIME.stop_runtime_session(agentRuntimeArn=os.environ["RUNTIME_ARN"],
                                         runtimeSessionId=item["runtimeSessionId"])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("ResourceNotFoundException", "ConflictException"):
                raise
        _purge(item["workspaceIdentity"])
        TABLE.delete_item(Key={"ownerId": owner_id, "logicalWorkspace": name},
                          ConditionExpression="deletionState = :state",
                          ExpressionAttributeValues={":state": "deleting"})
    except Exception:
        return _response(500, {"error": "workspace deletion incomplete", "workspace": _public_record(item)})
    return _response(200, {"workspace": _public_record(item), "deleted": True})


def _owner_items(owner_id):
    items = []
    response = TABLE.query(KeyConditionExpression=Key("ownerId").eq(owner_id))
    items.extend(response.get("Items", []))
    while response.get("LastEvaluatedKey"):
        response = TABLE.query(KeyConditionExpression=Key("ownerId").eq(owner_id),
                               ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def handler(event, _context):
    try:
        owner_id = _owner_id(event)
        method = event.get("httpMethod", "")
        path = event.get("resource", "")
        if method == "GET" and path == "/workspaces":
            return _list(owner_id)
        if method == "DELETE" and path == "/workspaces":
            records = _owner_items(owner_id)
            results = []
            for item in sorted(records, key=lambda value: value["logicalWorkspace"]):
                response = _delete(owner_id, item["logicalWorkspace"])
                results.append({"workspace": item["logicalWorkspace"], "status": "deleted" if response["statusCode"] < 300 else "failed"})
            failed = sum(1 for value in results if value["status"] == "failed")
            return _response(200 if not failed else 500, {"results": results, "deleted": len(results) - failed, "failed": failed})
        name = _name(event)
        if method == "POST" and path == "/workspaces/{name}/resolve":
            return _resolve(owner_id, name, _body(event))
        if method == "POST" and path == "/workspaces/{name}/rotate-session":
            return _rotate(owner_id, name)
        if method == "DELETE" and path == "/workspaces/{name}":
            return _delete(owner_id, name)
        return _response(404, {"error": "unknown operation"})
    except ValueError as exc:
        return _response(400, {"error": str(exc)})
    except Exception:
        return _response(500, {"error": "workspace registry operation failed"})
