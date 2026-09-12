#!/bin/bash
# imds-credential-process.sh — AWS credential_process bridge for the microVM.
#
# The AgentCore microVM exposes execution-role credentials via IMDSv2 only
# (IMDSv1 returns 401, no AWS_CONTAINER_CREDENTIALS_* env). boto3 handles this
# fine, but the AWS JS SDK credential chain used by OpenCode hangs on it
# (observed empirically). This script bridges the gap: the SDK invokes it via
# `credential_process` (in ~/.aws/config) and gets auto-refreshing credentials
# with proper Expiration handling.
set -euo pipefail

IMDS="http://169.254.169.254"

TOKEN=$(curl -sf -m 5 -X PUT "${IMDS}/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 300")

ROLE=$(curl -sf -m 5 -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    "${IMDS}/latest/meta-data/iam/security-credentials/")

curl -sf -m 5 -H "X-aws-ec2-metadata-token: ${TOKEN}" \
    "${IMDS}/latest/meta-data/iam/security-credentials/${ROLE}" \
| python3.11 -c '
import json, sys
c = json.load(sys.stdin)
print(json.dumps({
    "Version": 1,
    "AccessKeyId": c["AccessKeyId"],
    "SecretAccessKey": c["SecretAccessKey"],
    "SessionToken": c["Token"],
    "Expiration": c["Expiration"],
}))
'
