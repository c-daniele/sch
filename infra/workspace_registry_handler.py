"""IAM-authenticated workspace registry Lambda handler.

API Gateway supplies the authenticated caller ARN in requestContext; clients
never provide an owner identifier. The stored owner key is a hash so principal
details never become DynamoDB keys or runtime path components.

With isolation on (ISOLATION_ENABLED=true, spec per-principal-isolation
R36-R39) the owner is the caller's bound identity (principal ID), a caller
without a deploy-time plane is refused with HTTP 403, and responses carry the
caller's plane (runtime ARN, access role ARN, owner prefix). With isolation
off the owner stays sha256(caller ARN) and responses are unchanged (R9).
"""

import base64
import decimal
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
SSM = boto3.client("ssm")
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

# per-principal-isolation R36/R39: plane lookups are cached for at most 60 s,
# hits and misses alike (residual risk X8).
PLANE_CACHE_TTL_S = 60
_PLANE_CACHE = {}
OWNER_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
OWNER_PREFIX_RE = re.compile(r"^o\.[0-9a-f]{16}$")
# aws:userid of an IAM user (AIDA...) or of a role session (AROA...:<name>).
USER_ID_RE = re.compile(r"^[A-Z0-9]{16,128}$")
ROLE_SESSION_ID_RE = re.compile(r"^(AROA[A-Z0-9]{12,124}):(.{2,64})$")
SSO_ROLE_RE = re.compile(r"^AWSReservedSSO_(.+)_[0-9a-f]{16}$")
RUNTIME_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/[a-zA-Z][a-zA-Z0-9_]{0,47}-[a-zA-Z0-9]{10}$")
ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$")


class Forbidden(Exception):
    """The caller is authenticated but not allowed (HTTP 403)."""


class Plane:
    """A caller's deploy-time plane (spec R14), read from its SSM parameter."""

    __slots__ = ("owner_id", "owner_key", "owner_prefix", "runtime_arn", "access_role_arn")

    def __init__(self, owner_id, data):
        owner_key = owner_id[:16]
        if (not isinstance(data, dict) or data.get("schemaVersion") != 1
                or data.get("ownerKey") != owner_key
                or data.get("ownerPrefix") != "o." + owner_key
                or not isinstance(data.get("runtimeArn"), str)
                or not RUNTIME_ARN_RE.fullmatch(data["runtimeArn"])
                or not isinstance(data.get("accessRoleArn"), str)
                or not ROLE_ARN_RE.fullmatch(data["accessRoleArn"])):
            raise RuntimeError("plane mapping parameter is malformed")
        self.owner_id = owner_id
        self.owner_key = owner_key
        self.owner_prefix = data["ownerPrefix"]
        self.runtime_arn = data["runtimeArn"]
        self.access_role_arn = data["accessRoleArn"]

    def public(self, owner_prefix=None):
        return {
            "runtimeArn": self.runtime_arn,
            "accessRoleArn": self.access_role_arn,
            "ownerPrefix": owner_prefix or self.owner_prefix,
        }


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


def _isolation_enabled():
    return os.environ.get("ISOLATION_ENABLED", "").strip().lower() == "true"


def _owner_candidates(principal_id):
    """R36: owner strings for a principal ID, in lookup order."""
    if USER_ID_RE.fullmatch(principal_id):
        return ["user:" + principal_id]
    match = ROLE_SESSION_ID_RE.fullmatch(principal_id)
    if match:
        return ["sso:" + principal_id, "role:" + match.group(1)]
    return []


def _suggested_entry(caller_arn):
    """R37: the ISOLATED_PRINCIPALS entry that would admit this caller."""
    resource = caller_arn.split(":", 5)[5] if caller_arn.count(":") >= 5 else ""
    if resource.startswith("user/"):
        return "user:" + resource.rsplit("/", 1)[1]
    if resource.startswith("assumed-role/"):
        parts = resource.split("/")
        if len(parts) >= 3:
            role, session = parts[1], "/".join(parts[2:])
            sso = SSO_ROLE_RE.fullmatch(role)
            if sso:
                return "sso:{}/{}".format(sso.group(1), session)
            return "role:" + role
    return ""


def _plane_parameter(owner_key):
    prefix = os.environ.get("PLANE_PARAMETER_PREFIX", "")
    if not prefix.startswith("/") or not prefix.endswith("/planes/"):
        raise RuntimeError("PLANE_PARAMETER_PREFIX is not configured")
    return prefix + owner_key


def _lookup_plane(owner_id):
    """R14/R39: the plane of an owner, or None. Cached for PLANE_CACHE_TTL_S."""
    owner_key = owner_id[:16]
    cached = _PLANE_CACHE.get(owner_key)
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        return cached[1]
    try:
        value = SSM.get_parameter(Name=_plane_parameter(owner_key))["Parameter"]["Value"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise
        plane = None
    else:
        try:
            plane = Plane(owner_id, json.loads(value))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("plane mapping parameter is malformed") from exc
    _PLANE_CACHE[owner_key] = (now + PLANE_CACHE_TTL_S, plane)
    return plane


def _deployment_account(event, context):
    arn = getattr(context, "invoked_function_arn", "") or ""
    parts = arn.split(":")
    if len(parts) >= 5 and re.fullmatch(r"\d{12}", parts[4]):
        return parts[4]
    account = event.get("requestContext", {}).get("accountId", "")
    if isinstance(account, str) and re.fullmatch(r"\d{12}", account):
        return account
    raise RuntimeError("cannot determine the deployment account")


def _isolated_owner(event, context):
    """R36/R37: map the caller to its plane by bound identity, or refuse."""
    identity = event.get("requestContext", {}).get("identity", {})
    caller_arn = identity.get("userArn")
    principal_id = identity.get("user")
    account = identity.get("accountId")
    if not caller_arn or not isinstance(principal_id, str) or not principal_id:
        raise ValueError("verified caller identity is missing")
    if account != _deployment_account(event, context):
        raise Forbidden("caller {} is not in the deployment account; isolated "
                        "principals must belong to this account".format(caller_arn))
    for owner in _owner_candidates(principal_id):
        owner_id = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        plane = _lookup_plane(owner_id)
        if plane is not None:
            return owner_id, plane
    entry = _suggested_entry(caller_arn)
    if entry:
        raise Forbidden("caller {} is not an isolated principal of this deployment; "
                        "add {} to ISOLATED_PRINCIPALS and redeploy".format(caller_arn, entry))
    raise Forbidden("caller {} is not an isolated principal of this deployment; "
                    "only IAM users, IAM Identity Center users and IAM roles can be "
                    "listed in ISOLATED_PRINCIPALS".format(caller_arn))


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
    # The boto3 DynamoDB resource returns numbers as Decimal: an integral
    # Decimal is a valid epoch (records read back from the table).
    if isinstance(value, decimal.Decimal) and value == value.to_integral_value():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("workspace has invalid session epoch metadata")
    return value


def _record(item, plane=None):
    record = {
        "logicalWorkspace": item["logicalWorkspace"],
        "runtimeSessionId": item["runtimeSessionId"],
        "harness": item["harness"],
        "workspaceIdentity": item["workspaceIdentity"],
        "storage": _storage_value(item),
        "sessionEpoch": _epoch_value(item),
    }
    if plane is not None:
        # R38: the stored, immutable owner prefix of the record.
        record["plane"] = plane.public(_owner_prefix_value(item, plane))
    return record


def _owner_prefix_value(item, plane):
    value = item.get("ownerPrefix") or plane.owner_prefix
    if not isinstance(value, str) or not OWNER_PREFIX_RE.fullmatch(value):
        raise ValueError("workspace has invalid owner prefix metadata")
    return value


def _public_record(item, plane=None):
    record = _record(item, plane)
    if item.get("deletionState") == "deleting":
        record["deletionState"] = "deleting"
    return record


def _out(status, body, plane=None):
    """A response; with isolation on it carries the caller's plane (R38)."""
    if plane is not None:
        body = dict(body, isolation=True, plane=plane.public())
    return _response(status, body)


def _new_record(owner_id, name, harness, storage, owner_prefix=None):
    now = int(time.time())
    item = {
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
    if owner_prefix is not None:
        item["ownerPrefix"] = owner_prefix
    return item


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


def _existing_response(owner_id, name, existing, body, requested_harness, requested_storage, plane):
    existing = _reconcile_storage(owner_id, name, existing)
    if body.get("harness") and existing["harness"] != requested_harness:
        return _out(409, {"error": "workspace harness is immutable", "workspace": _record(existing, plane)}, plane)
    existing_storage = _storage_value(existing)
    if body.get("storage") and existing_storage != requested_storage:
        return _out(409, {"error": "workspace storage is immutable", "workspace": _record(existing, plane)}, plane)
    return _out(200, {"workspace": _record(existing, plane), "created": False}, plane)


def _resolve(owner_id, name, body, plane=None):
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
            return _out(409, {"error": "workspace is being deleted"}, plane)
        return _existing_response(owner_id, name, existing, body, requested_harness,
                                  requested_storage, plane)
    item = _new_record(owner_id, name, requested_harness, requested_storage,
                       plane.owner_prefix if plane is not None else None)
    try:
        TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(ownerId) AND attribute_not_exists(logicalWorkspace)")
        return _out(201, {"workspace": _record(item, plane), "created": True}, plane)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = TABLE.get_item(Key={"ownerId": owner_id, "logicalWorkspace": name}).get("Item")
        if not existing:
            raise
        return _existing_response(owner_id, name, existing, body, requested_harness,
                                  requested_storage, plane)


def _list(owner_id, plane=None):
    response = TABLE.query(KeyConditionExpression=Key("ownerId").eq(owner_id))
    items = response.get("Items", [])
    while response.get("LastEvaluatedKey"):
        response = TABLE.query(
            KeyConditionExpression=Key("ownerId").eq(owner_id),
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))
    return _out(200, {"workspaces": [_public_record(item, plane) for item in items
                                      if item.get("deletionState") != "deleting"]}, plane)


def _rotate(owner_id, name, plane=None):
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
                return _out(409, {"error": "workspace is being deleted"}, plane)
            return _out(404, {"error": "unknown workspace"}, plane)
        raise
    return _out(200, {"workspace": _record(response["Attributes"], plane)}, plane)


def _purge_prefixes(identity, owner_prefix=None):
    """The exact keys and trees of one workspace (R26 with an owner prefix)."""
    if owner_prefix is None:
        return ["checkpoints/{}/".format(identity),
                "checkpoint-generations/{}/".format(identity),
                "workspace-writers/{}.json".format(identity)]
    if not OWNER_PREFIX_RE.fullmatch(owner_prefix):
        raise ValueError("invalid owner prefix")
    return ["checkpoints/{}/{}/".format(owner_prefix, identity),
            "checkpoint-generations/{}/{}/".format(owner_prefix, identity),
            "workspace-writers/{}/{}.json".format(owner_prefix, identity),
            "builds/{}/{}/".format(owner_prefix, identity)]


def _purge(identity, owner_prefix=None):
    if owner_prefix is not None and not WORKSPACE_RE.fullmatch(identity):
        raise ValueError("invalid workspace identity")
    bucket = os.environ["CHECKPOINT_BUCKET"]
    prefixes = _purge_prefixes(identity, owner_prefix)
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


def _delete(owner_id, name, plane=None):
    item = TABLE.get_item(Key={"ownerId": owner_id, "logicalWorkspace": name}).get("Item")
    if not item:
        return _out(404, {"error": "unknown workspace"}, plane)
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
        # R38: with isolation on the session lives on the caller's plane
        # runtime and its keys under the record's stored owner prefix.
        runtime_arn = plane.runtime_arn if plane is not None else os.environ["RUNTIME_ARN"]
        owner_prefix = _owner_prefix_value(item, plane) if plane is not None else None
        try:
            RUNTIME.stop_runtime_session(agentRuntimeArn=runtime_arn,
                                         runtimeSessionId=item["runtimeSessionId"])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("ResourceNotFoundException", "ConflictException"):
                raise
        _purge(item["workspaceIdentity"], owner_prefix)
        TABLE.delete_item(Key={"ownerId": owner_id, "logicalWorkspace": name},
                          ConditionExpression="deletionState = :state",
                          ExpressionAttributeValues={":state": "deleting"})
    except Exception:
        return _out(500, {"error": "workspace deletion incomplete",
                          "workspace": _public_record(item, plane)}, plane)
    return _out(200, {"workspace": _public_record(item, plane), "deleted": True}, plane)


def _owner_items(owner_id):
    items = []
    response = TABLE.query(KeyConditionExpression=Key("ownerId").eq(owner_id))
    items.extend(response.get("Items", []))
    while response.get("LastEvaluatedKey"):
        response = TABLE.query(KeyConditionExpression=Key("ownerId").eq(owner_id),
                               ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return items


def handler(event, context):
    try:
        plane = None
        if _isolation_enabled():
            # R36/R37: mapping and refusal come before any table access.
            owner_id, plane = _isolated_owner(event, context)
        else:
            owner_id = _owner_id(event)
        method = event.get("httpMethod", "")
        path = event.get("resource", "")
        if method == "GET" and path == "/workspaces":
            return _list(owner_id, plane)
        if method == "DELETE" and path == "/workspaces":
            records = _owner_items(owner_id)
            results = []
            for item in sorted(records, key=lambda value: value["logicalWorkspace"]):
                response = _delete(owner_id, item["logicalWorkspace"], plane)
                results.append({"workspace": item["logicalWorkspace"], "status": "deleted" if response["statusCode"] < 300 else "failed"})
            failed = sum(1 for value in results if value["status"] == "failed")
            return _out(200 if not failed else 500, {"results": results, "deleted": len(results) - failed, "failed": failed}, plane)
        name = _name(event)
        if method == "POST" and path == "/workspaces/{name}/resolve":
            return _resolve(owner_id, name, _body(event), plane)
        if method == "POST" and path == "/workspaces/{name}/rotate-session":
            return _rotate(owner_id, name, plane)
        if method == "DELETE" and path == "/workspaces/{name}":
            return _delete(owner_id, name, plane)
        return _response(404, {"error": "unknown operation"})
    except Forbidden as exc:
        return _response(403, {"error": str(exc)})
    except ValueError as exc:
        return _response(400, {"error": str(exc)})
    except Exception:
        return _response(500, {"error": "workspace registry operation failed"})
