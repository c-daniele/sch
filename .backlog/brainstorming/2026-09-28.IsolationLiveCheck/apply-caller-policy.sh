#!/bin/bash
# Gives one listed IAM user the minimal caller policy of
# docs/getting-started.md ("Caller permissions on an isolated stack"), built
# from the live stack (account, region, registry API, the user's owner key),
# as the customer managed policy <project>-<env>-caller-<user>. Then detaches
# the broad test policy <project>-<env>-isolation-test-caller from that user,
# so the test-principals stack can be deleted. Attach first, detach last: the
# user never loses access in between. Run with your operator profile active.
#
# Usage: apply-caller-policy.sh <iam-user-name>
set -euo pipefail

USER_NAME="${1:?usage: apply-caller-policy.sh <iam-user-name>}"
REGION="${SCH_REGION:-eu-west-1}"
PROJECT="${SCH_PROJECT:-sch}"
ENVIRONMENT="${SCH_ENV:-dev}"
STACK="${PROJECT}-${ENVIRONMENT}-runtime"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
USER_ID="$(aws iam get-user --user-name "${USER_NAME}" --query User.UserId --output text)"
OWNER_KEY="$(printf 'user:%s' "${USER_ID}" \
    | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:16])')"
INVOKE_ARN="$(aws cloudformation describe-stacks --stack-name "${STACK}" --region "${REGION}" \
    --query "Stacks[0].Outputs[?OutputKey=='WorkspaceRegistryInvokeArn'].OutputValue|[0]" --output text)"
if [ -z "${INVOKE_ARN}" ] || [ "${INVOKE_ARN}" = "None" ]; then
    echo "no WorkspaceRegistryInvokeArn output on ${STACK}" >&2
    exit 1
fi

RUNTIME="arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:runtime/${PROJECT}_${ENVIRONMENT}_o_${OWNER_KEY}-*"
POLICY_NAME="${PROJECT}-${ENVIRONMENT}-caller-${USER_NAME}"
POLICY_ARN="arn:aws:iam::${ACCOUNT}:policy/${POLICY_NAME}"
TEST_POLICY_ARN="arn:aws:iam::${ACCOUNT}:policy/${PROJECT}-${ENVIRONMENT}-isolation-test-caller"

DOC="$(python3 - "${INVOKE_ARN}" "${RUNTIME}" "${ACCOUNT}" "${REGION}" "${PROJECT}" "${ENVIRONMENT}" "${OWNER_KEY}" "${STACK}" <<'EOF'
import json, sys
invoke, runtime, account, region, project, env, key, stack = sys.argv[1:]
print(json.dumps({
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": "execute-api:Invoke", "Resource": invoke},
        {"Effect": "Allow",
         "Action": ["bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeCommand",
                    "bedrock-agentcore:InvokeAgentRuntimeCommandShell",
                    "bedrock-agentcore:InvokeAgentRuntimeWithWebSocketStream",
                    "bedrock-agentcore:StopRuntimeSession",
                    "bedrock-agentcore:GetAgentRuntime"],
         "Resource": [runtime, runtime + "/runtime-endpoint/DEFAULT"]},
        {"Effect": "Allow", "Action": "sts:AssumeRole",
         "Resource": "arn:aws:iam::{}:role/{}-{}-o-{}-access".format(account, project, env, key)},
        {"Effect": "Allow", "Action": "cloudformation:DescribeStacks",
         "Resource": "arn:aws:cloudformation:{}:{}:stack/{}/*".format(region, account, stack)},
    ],
}))
EOF
)"

if aws iam get-policy --policy-arn "${POLICY_ARN}" >/dev/null 2>&1; then
    # Re-runnable: a managed policy keeps at most five versions.
    for v in $(aws iam list-policy-versions --policy-arn "${POLICY_ARN}" \
            --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
        aws iam delete-policy-version --policy-arn "${POLICY_ARN}" --version-id "${v}"
    done
    aws iam create-policy-version --policy-arn "${POLICY_ARN}" --policy-document "${DOC}" --set-as-default >/dev/null
else
    aws iam create-policy --policy-name "${POLICY_NAME}" --policy-document "${DOC}" \
        --description "SCH caller permissions of ${USER_NAME} on the isolated ${PROJECT}/${ENVIRONMENT} stack" >/dev/null
fi
aws iam attach-user-policy --user-name "${USER_NAME}" --policy-arn "${POLICY_ARN}"
echo "attached ${POLICY_NAME} to ${USER_NAME} (owner key ${OWNER_KEY})"

if aws iam list-attached-user-policies --user-name "${USER_NAME}" \
        --query 'AttachedPolicies[].PolicyArn' --output text | grep -q "isolation-test-caller"; then
    aws iam detach-user-policy --user-name "${USER_NAME}" --policy-arn "${TEST_POLICY_ARN}"
    echo "detached the test policy from ${USER_NAME}"
fi
echo "note: IAM changes can take a few seconds to apply"
