#!/bin/bash
# Creates the two test IAM users of test-principals.yaml (B listed, C unlisted)
# and the local AWS CLI profiles <project>-iso-b and <project>-iso-c. Access
# keys go straight from the API response into `aws configure set`; nothing
# secret is printed. Re-runnable: old keys of the two users are replaced.
# Run with your operator profile (AWS_PROFILE) active.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${SCH_REGION:-eu-west-1}"
PROJECT="${SCH_PROJECT:-sch}"
ENVIRONMENT="${SCH_ENV:-dev}"
STACK="${PROJECT}-${ENVIRONMENT}-isolation-test-principals"

aws cloudformation deploy \
    --stack-name "${STACK}" \
    --template-file "${DIR}/test-principals.yaml" \
    --parameter-overrides "ProjectName=${PROJECT}" "Environment=${ENVIRONMENT}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --no-fail-on-empty-changeset \
    --region "${REGION}"

for who in b c; do
    user="${PROJECT}-iso-${who}"
    profile="${PROJECT}-iso-${who}"
    for key in $(aws iam list-access-keys --user-name "${user}" \
            --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
        aws iam delete-access-key --user-name "${user}" --access-key-id "${key}"
    done
    key_json="$(aws iam create-access-key --user-name "${user}" --output json)"
    aws configure set aws_access_key_id \
        "$(printf '%s' "${key_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["AccessKeyId"])')" \
        --profile "${profile}"
    aws configure set aws_secret_access_key \
        "$(printf '%s' "${key_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["AccessKey"]["SecretAccessKey"])')" \
        --profile "${profile}"
    aws configure set region "${REGION}" --profile "${profile}"
    unset key_json
    echo "profile ${profile} -> IAM user ${user}"
done

echo "note: new IAM keys can take a few seconds to become usable"
echo "next: add \"user:${PROJECT}-iso-b\" (and your own user) to ISOLATED_PRINCIPALS, never ${PROJECT}-iso-c"
