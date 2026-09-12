"""Minimal SigV4 client for the optional IAM-authenticated workspace registry."""

import datetime
import hashlib
import hmac
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import re

from .config import die

IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class RegistryWorkspace:
    __slots__ = ("name", "sid", "harness", "identity", "storage", "epoch", "was_created")

    def __init__(self, data, was_created=False):
        required = ("logicalWorkspace", "runtimeSessionId", "harness", "workspaceIdentity")
        if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key] for key in required):
            raise ValueError("registry returned an invalid workspace record")
        if not IDENTITY_RE.fullmatch(data["workspaceIdentity"]):
            raise ValueError("registry returned an unsafe workspace identity")
        self.sid = data["runtimeSessionId"]
        self.harness = data["harness"]
        self.identity = data["workspaceIdentity"]
        self.storage = "session" if "storage" not in data else data["storage"]
        if not isinstance(self.storage, str) or self.storage not in ("s3", "session"):
            raise ValueError("registry returned an invalid storage backend")
        self.name = data["logicalWorkspace"]
        self.epoch = data.get("sessionEpoch", 0)
        if not isinstance(self.epoch, int) or isinstance(self.epoch, bool) or self.epoch < 0:
            raise ValueError("registry returned an invalid session epoch")
        self.was_created = was_created


def enabled(cfg):
    return bool(getattr(cfg, "workspace_registry_url", ""))


def _credentials():
    try:
        result = subprocess.run(
            ["aws", "configure", "export-credentials", "--format", "process"],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError("cannot load AWS credentials for workspace registry") from exc
    if result.returncode != 0:
        raise RuntimeError("cannot load AWS credentials for workspace registry")
    try:
        data = json.loads(result.stdout)
        return data["AccessKeyId"], data["SecretAccessKey"], data.get("SessionToken", "")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("AWS CLI returned invalid credentials") from exc


def _sign(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _request(cfg, method, path, body=None):
    parsed = urllib.parse.urlparse(cfg.workspace_registry_url)
    url = cfg.workspace_registry_url + "/" + path.lstrip("/")
    payload = b"" if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    access_key, secret_key, token = _credentials()
    canonical_uri = urllib.parse.quote(urllib.parse.urlparse(url).path or "/", safe="/-_.~")
    payload_hash = hashlib.sha256(payload).hexdigest()
    headers = {"content-type": "application/json", "host": parsed.netloc, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    if token:
        headers["x-amz-security-token"] = token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join("{}:{}\n".format(key, headers[key]) for key in sorted(headers))
    canonical_request = "\n".join([method, canonical_uri, "", canonical_headers, signed_headers, payload_hash])
    scope = "{}/{}/execute-api/aws4_request".format(date, cfg.region)
    string_to_sign = "AWS4-HMAC-SHA256\n{}\n{}\n{}".format(amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest())
    signing_key = _sign(_sign(_sign(_sign(("AWS4" + secret_key).encode(), date), cfg.region), "execute-api"), "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = "AWS4-HMAC-SHA256 Credential={}/{}, SignedHeaders={}, Signature={}".format(access_key, scope, signed_headers, signature)
    request = urllib.request.Request(url, data=payload if body is not None else None, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError("workspace registry request failed") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("workspace registry returned invalid JSON") from exc
    if status < 200 or status >= 300:
        raise RuntimeError(
            "workspace registry rejected request: {}".format(
                data.get("error") or data.get("message") or status
            )
        )
    return data


def resolve(cfg, workspace, harness="", storage="", default_storage=""):
    body = {}
    if harness:
        body["harness"] = harness
    if storage:
        body["storage"] = storage
    if default_storage:
        body["defaultStorage"] = default_storage
    data = _request(cfg, "POST", "/workspaces/{}/resolve".format(urllib.parse.quote(workspace, safe="")), body)
    return RegistryWorkspace(data.get("workspace"), bool(data.get("created")))


def list_workspaces(cfg):
    data = _request(cfg, "GET", "/workspaces")
    records = data.get("workspaces")
    if not isinstance(records, list):
        raise RuntimeError("registry returned an invalid workspace list")
    return [RegistryWorkspace(record) for record in records]


def rotate(cfg, workspace):
    data = _request(cfg, "POST", "/workspaces/{}/rotate-session".format(urllib.parse.quote(workspace, safe="")), {})
    return RegistryWorkspace(data.get("workspace"))


def delete(cfg, workspace):
    data = _request(cfg, "DELETE", "/workspaces/{}".format(urllib.parse.quote(workspace, safe="")))
    result = data.get("workspace", data)
    if not isinstance(result, dict):
        raise RuntimeError("registry returned an invalid deletion response")
    return result


def delete_all(cfg):
    return _request(cfg, "DELETE", "/workspaces")


def resolve_or_die(cfg, workspace, harness="", storage="", default_storage=""):
    try:
        return resolve(cfg, workspace, harness, storage, default_storage)
    except (RuntimeError, ValueError) as exc:
        die(str(exc))
