#!/bin/bash
# Removes what setup-test-principals.sh created: the access keys, the stack
# with the two test users and their policy. The local CLI profiles are left
# for you to delete by hand (sections [<project>-iso-b] and [<project>-iso-c]
# in ~/.aws/credentials and ~/.aws/config): aws configure cannot remove them.
# Run with your operator profile (AWS_PROFILE) active, after the last deploy
# that dropped user:<project>-iso-b from ISOLATED_PRINCIPALS.
set -euo pipefail

REGION="${SCH_REGION:-eu-west-1}"
PROJECT="${SCH_PROJECT:-sch}"
ENVIRONMENT="${SCH_ENV:-dev}"
STACK="${PROJECT}-${ENVIRONMENT}-isolation-test-principals"

for who in b c; do
    user="${PROJECT}-iso-${who}"
    for key in $(aws iam list-access-keys --user-name "${user}" \
            --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null); do
        aws iam delete-access-key --user-name "${user}" --access-key-id "${key}"
    done
done

aws cloudformation delete-stack --stack-name "${STACK}" --region "${REGION}"
aws cloudformation wait stack-delete-complete --stack-name "${STACK}" --region "${REGION}"
echo "deleted stack ${STACK}; remove the profiles ${PROJECT}-iso-b and ${PROJECT}-iso-c from ~/.aws by hand"
