"""`sch destroy`: remove every AWS resource of one SCH deployment, in one run.

Teardown used to be a shell script plus a list of manual commands for the
things it did not cover (the out-of-band CloudFormation bootstrap bucket, the
`-ecr` stack of pre-bootstrap deployments). Every forgotten step left a
resource behind, and the account was never actually empty.

Two properties matter more than brevity here:

- **It says where it is pointed before it deletes.** The account comes from
  STS at run time, never from a cached value: a stale cache pointing at
  another account is a real failure mode of this client, and the one mistake
  a teardown cannot undo.
- **It is re-runnable.** Missing resources are reported as already absent, so
  a run interrupted halfway (an expired session, a missing permission) is
  finished by running the command again.
"""

import argparse
import sys

from .. import awsteardown as teardown
from .. import cli as cli_mod
from .. import repo


def _parser():
    parser = argparse.ArgumentParser(
        prog="sch destroy",
        description="Delete all AWS resources of one SCH project/environment.",
    )
    parser.add_argument("-r", "--region", default="", help="AWS region")
    parser.add_argument("-p", "--project", default="", help="project name")
    parser.add_argument("-e", "--env", default="", help="environment")
    parser.add_argument(
        "--keep-checkpoints", action="store_true",
        help="keep the L2 checkpoint bucket (workspace data) and delete everything else",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be deleted and exit without touching anything",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="skip the typed confirmation (for automation on disposable environments)",
    )
    return parser


class Target:
    """One resource to delete, with the outcome of the attempt."""

    def __init__(self, kind, name, present):
        self.kind = kind
        self.name = name
        self.present = present
        self.outcome = ""


def _plan(project, env, region, account, keep_checkpoints):
    runtime_stack = "{}-{}-runtime".format(project, env)
    bootstrap_stack = "{}-{}-bootstrap".format(project, env)
    # Deployments created before the bootstrap stack existed carry an -ecr
    # stack instead; covering both here is what lets one command retire an
    # older environment without a second, older script.
    legacy_ecr_stack = "{}-{}-ecr".format(project, env)
    repository = "{}-{}".format(project, env)

    targets = [
        # The runtime stack first: its checkpoint bucket is DeletionPolicy:
        # Retain, so CloudFormation never tries to delete it and nothing has to
        # be cleared beforehand.
        Target("stack", runtime_stack, teardown.stack_exists(runtime_stack, region)),
    ]
    # ORDER RULE: anything the bootstrap stack OWNS and that CloudFormation
    # cannot delete while non-empty must be cleared before the stack itself.
    # Deleting the stack first leaves it in DELETE_FAILED on BuildSourcesBucket
    # ("the bucket you tried to delete is not empty") and on a repository that
    # still holds images.
    targets.append(
        Target("ecr repository", repository,
               teardown.repository_exists(repository, region))
    )
    build_sources = ""
    if account:
        build_sources = "{}-{}-build-sources-{}".format(project, env, account)
        targets.append(
            Target("bucket", build_sources,
                   teardown.bucket_exists(build_sources, region))
        )
    targets.append(
        Target("stack", bootstrap_stack, teardown.stack_exists(bootstrap_stack, region))
    )
    targets.append(
        Target("stack (legacy)", legacy_ecr_stack,
               teardown.stack_exists(legacy_ecr_stack, region))
    )
    if account:
        # Out of band: created by deploy.sh, owned by no stack.
        cfn_bootstrap = "{}-{}-cfn-bootstrap-{}".format(project, env, account)
        targets.append(
            Target("bucket", cfn_bootstrap, teardown.bucket_exists(cfn_bootstrap, region))
        )
        checkpoints = "{}-{}-checkpoints-{}".format(project, env, account)
        if not keep_checkpoints:
            targets.append(
                Target("bucket (L2 data)", checkpoints,
                       teardown.bucket_exists(checkpoints, region))
            )
    return targets


def _confirm(project, env, yes):
    # The phrase carries the environment name on purpose: it forces the operator
    # to confirm WHICH deployment is being destroyed, not merely that something is.
    if cli_mod.confirm_phrase("DELETE {}-{}".format(project, env), yes):
        return True
    print("sch: teardown cancelled", file=sys.stderr)
    return False


def cmd_destroy(cfg, args):
    opts = _parser().parse_args(args)
    region = opts.region or cfg.region
    project = opts.project or cfg.project
    env = opts.env or cfg.env

    try:
        account = teardown.account_id(region)
    except teardown.TeardownError as exc:
        print("sch: {}".format(exc), file=sys.stderr)
        return 1
    if not account:
        print(
            "sch: cannot resolve the AWS account (credentials configured for"
            " region {}?) — refusing to delete anything without knowing where"
            " it would happen".format(region),
            file=sys.stderr,
        )
        return 1

    print("sch: teardown target")
    print("sch:   account     {}".format(account))
    print("sch:   region      {}".format(region))
    print("sch:   project/env {}/{}".format(project, env))

    targets = _plan(project, env, region, account, opts.keep_checkpoints)
    print("sch: resources")
    for target in targets:
        print("sch:   {:<18} {:<48} {}".format(
            target.kind, target.name, "present" if target.present else "absent"
        ))
    if opts.keep_checkpoints:
        print("sch:   checkpoint bucket kept (--keep-checkpoints)")

    if not any(target.present for target in targets):
        print("sch: nothing to delete — this project/environment is already gone")
        return 0

    if opts.dry_run:
        print("sch: dry run — nothing was deleted")
        return 0

    print(
        "sch: this permanently deletes the runtime (all live sessions){}".format(
            "" if opts.keep_checkpoints else " and every workspace checkpoint"
        ),
        file=sys.stderr,
    )
    if not _confirm(project, env, opts.yes):
        return 1

    failures = []

    token = teardown.read_setenv_token(repo.repo_root())
    outcome = teardown.deregister_telegram_webhook(token)
    print("sch: telegram webhook: {}".format(outcome))

    # Order matters: the runtime stack owns the AgentCore runtime (deleting it
    # releases the session storage), the repository must be emptied before the
    # stack that owns it can go, and the buckets outlive their stacks either by
    # retention policy (checkpoints) or because nothing owns them (cfn bootstrap).
    for target in targets:
        if not target.present:
            target.outcome = "already absent"
            continue
        try:
            if target.kind.startswith("stack"):
                # ImageRebuildProject is retained on failure: deleting a
                # CodeBuild project needs codebuild:DeleteProject, and a
                # missing permission must not strand the rest of the teardown.
                retain = ("ImageRebuildProject",) if target.name.endswith("-runtime") else ()
                try:
                    target.outcome = teardown.delete_stack(target.name, region)
                except teardown.TeardownError:
                    if not retain:
                        raise
                    target.outcome = teardown.delete_stack(
                        target.name, region, retain_resources=retain
                    ) + " (retained ImageRebuildProject)"
            elif target.kind == "ecr repository":
                removed = teardown.purge_repository_images(target.name, region)
                target.outcome = "{} image(s) deleted".format(removed)
            else:
                target.outcome = teardown.delete_bucket(target.name, region)
        except teardown.TeardownError as exc:
            target.outcome = "FAILED: {}".format(exc)
            failures.append(target)
        print("sch: {:<18} {:<48} {}".format(
            target.kind, target.name, target.outcome
        ))

    # The cached runtime ARN and bucket name now point at deleted resources.
    # Leaving them would make the next command in another account invoke this
    # one's ARN — the exact failure this client already suffered once.
    for cache in (cfg.runtime_arn_cache, cfg.checkpoint_bucket_cache):
        try:
            cache.unlink()
            print("sch: removed stale cache {}".format(cache))
        except FileNotFoundError:
            pass
        except OSError as exc:
            print("sch: could not remove {}: {}".format(cache, exc), file=sys.stderr)

    print("sch: left behind on purpose (SCH never created these):")
    print("sch:   - IAM policies you attached to the deploy principal")
    print("sch:   - Bedrock model access / Marketplace agreements")
    print("sch:   - local state and the client itself (use 'sch uninstall')")

    if failures:
        print(
            "sch: {} resource(s) could not be deleted — fix the cause and"
            " re-run 'sch destroy' (it is re-runnable)".format(len(failures)),
            file=sys.stderr,
        )
        return 1
    print("sch: teardown complete")
    return 0
