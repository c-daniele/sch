"""`sch task`: submit a headless detached task."""

import os
import sys

from .. import cli as cli_mod
from .. import deps
from .. import gitnative
from .. import harness as harness_mod
from .. import procs, runtime, sync as sync_mod, workspace
from ..config import die, runtime_arn
from . import handoff as handoff_mod

_USAGE = (
    'usage: sch task <workspace> [--harness <opencode|claude|pi>] [--model <id>] '
    '[--variant <name>] '
    '[--branch <name>] [--continue] [--handoff [--handoff-session <id>] [--sanitize]] '
    '"<prompt>" [--timeout <s>]'
)


def _parse_args(args):
    if not args or not args[0]:
        die(_USAGE)
    ws = args[0]
    workspace.validate_workspace_name(ws)
    rest, sync_options = sync_mod.parse_options(args[1:], _USAGE)

    continue_flag = False
    timeout_raw = ""
    harness_flag = ""
    model = ""
    variant = ""
    branch_flag = ""
    handoff = False
    handoff_session = ""
    sanitize = False
    prompt_parts = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--continue":
            continue_flag = True
            i += 1
            continue
        if arg == "--handoff":
            handoff = True
            i += 1
            continue
        if arg == "--handoff-session":
            if i + 1 >= len(rest):
                die("usage: --handoff-session <id>")
            handoff_session = rest[i + 1]
            i += 2
            continue
        if arg == "--sanitize":
            sanitize = True
            i += 1
            continue
        if arg == "--harness":
            if i + 1 >= len(rest):
                die("usage: --harness <opencode|claude|pi>")
            harness_flag = rest[i + 1]
            i += 2
            continue
        if arg == "--model":
            if i + 1 >= len(rest):
                die("usage: --model <id>")
            model = cli_mod.validate_model_or_die(rest[i + 1])
            i += 2
            continue
        if arg == "--variant":
            if i + 1 >= len(rest):
                die("usage: --variant <name>")
            variant = cli_mod.validate_variant_or_die(rest[i + 1])
            i += 2
            continue
        if arg == "--branch":
            if i + 1 >= len(rest):
                die("usage: --branch <name>")
            branch_flag = gitnative.validate_branch_or_die(rest[i + 1])
            i += 2
            continue
        if arg == "--timeout":
            if i + 1 >= len(rest):
                die("usage: --timeout <seconds>")
            timeout_raw = rest[i + 1]
            i += 2
            continue
        if arg == "--":
            # Mirrors the bash reference's `prompt="$*"`: everything after
            # `--` REPLACES whatever prompt words were accumulated so far
            # (not appended), joined by a single space.
            prompt_parts = rest[i + 1 :]
            break
        if arg.startswith("-"):
            die("unknown option '{}'".format(arg))
        prompt_parts.append(arg)
        i += 1

    # Git-native mode is mutually exclusive with the mirror sync options
    # (add-git-native-workflow design D5; same pattern as --no-sync).
    gitnative.check_flag_exclusion_or_die(branch_flag, sync_options)

    if handoff_session and not handoff:
        die("--handoff-session requires --handoff")
    if sanitize and not handoff:
        die("--sanitize requires --handoff")
    if handoff and harness_flag and harness_flag not in harness_mod.VALID_HARNESSES:
        die(
            "invalid harness '{}' (expected: {})".format(
                harness_flag, "|".join(harness_mod.VALID_HARNESSES)
            )
        )
    if variant and harness_flag and harness_flag != "opencode":
        die(
            "--variant supports only harness='opencode' (reasoning effort is "
            "an opencode-only concept)"
        )

    prompt = " ".join(prompt_parts)
    if not prompt:
        die(_USAGE)
    return (
        ws, harness_flag, model, variant, branch_flag, continue_flag, timeout_raw,
        prompt, sync_options, handoff, handoff_session, sanitize,
    )


def cmd_task(cfg, args):
    (
        ws, harness_flag, model, variant, branch_flag, continue_flag, timeout_raw,
        prompt, sync_options, handoff, handoff_session, sanitize,
    ) = _parse_args(args)

    # --handoff is opencode-only by nature (it transfers an OpenCode
    # session): reject a divergent --harness before any local or remote
    # mutation, mirroring `sch handoff` (spec: session-handoff I1/I5).
    if handoff:
        handoff_mod._reject_non_opencode_harness_flag(ws, harness_flag)
        opencode_bin = deps.which_opencode()
        if not opencode_bin:
            die("'opencode' is required locally for '--handoff'; install it and ensure it is on PATH")
        # Export before resolve: an export failure dies before the workspace
        # is created or the runtime is touched.
        _, export_path, local_version = handoff_mod.export_local_session(
            opencode_bin, handoff_session, sanitize
        )
    else:
        export_path = ""
        local_version = "unknown"

    try:
        # provisioning=True: submitting a task to a session superseded by a
        # runtime version change is exactly the 2026-08-21 incident, so rotate
        # first (add-task-liveness-safety design D3).
        # With --handoff and no explicit --harness, force the opencode
        # binding (the transfer is opencode-only) instead of inheriting
        # SCH_DEFAULT_HARNESS.
        effective_harness_flag = harness_flag
        if handoff and not effective_harness_flag:
            effective_harness_flag = "opencode"
        resolved = harness_mod.resolve_harness(
            cfg, ws, effective_harness_flag, sync_options["storage"], provisioning=True
        )
        if handoff:
            # Enforcing gate for a workspace already bound to another
            # harness: fail before the warmup/seed (no remote mutation).
            # The read-only pre-check names the constraint; resolve_harness
            # above already refused a divergent explicit flag.
            handoff_mod._reject_non_opencode_binding(cfg, ws)
            if resolved.harness != "opencode":
                die(
                    "workspace '{}' is bound to harness='{}'; '--handoff' supports only "
                    "harness='opencode' (it transfers an OpenCode session) — use a workspace "
                    "bound to opencode, or omit '--handoff' to work in this "
                    "one".format(ws, resolved.harness)
                )
        if variant and resolved.harness != "opencode":
            # Enforcing gate for a workspace already bound to another
            # harness (spec R8a): reasoning effort is opencode-only — fail
            # before the warmup/seed (no remote mutation). The parse-time
            # pre-check above already refused a divergent explicit flag.
            die(
                "workspace '{}' is bound to harness='{}'; '--variant' supports only "
                "harness='opencode' (reasoning effort is an opencode-only "
                "concept)".format(ws, resolved.harness)
            )
        sid = resolved.sid
        harness = resolved.harness
        storage_backend = resolved.storage
        session_epoch = resolved.epoch
        runtime_workspace = resolved.identity or ws

        # Git-native mode (add-git-native-workflow): resolved BEFORE the sync
        # binding — a git-native workspace never starts a sync helper, and the
        # cross-mode conflicts die here with the current mode and the remedy.
        git_mode = gitnative.resolve_mode(cfg, ws, runtime_workspace, branch_flag, sync_options)
        sync_root = None
        if git_mode is None:
            sync_root = sync_mod.resolve_binding(cfg, runtime_workspace, sync_options)
            if sync_root:
                print("sch: sync binding selected: {}".format(sync_root), file=sys.stderr)

        timeout_s = None
        if timeout_raw:
            timeout_s = cli_mod.parse_int_or_die(timeout_raw, "--timeout")

        warmup = runtime.invoke_verified(
            cfg, sid, runtime.payload_noop(
                runtime_workspace, harness,
                "fresh" if resolved.was_created else "resumed",
                storage_backend, session_epoch,
            ), "task-warmup"
        )
        storage_error = runtime.storage_verification_error(warmup, storage_backend)
        if storage_error:
            die(storage_error)

        if git_mode is not None:
            # The task is submitted only after a completed seed (spec: "the task
            # starts only after the seed completes"). On a seeded workspace
            # (incl. --continue) this is a no-op reuse of branch/base.
            gitnative.ensure_seeded(
                cfg, ws, git_mode, runtime_arn(cfg), sid, runtime_workspace,
                harness, storage_backend, session_epoch,
            )

        if sync_root:
            try:
                procs.supervise_interactive(
                    sync_mod.helper_argv(
                        cfg, runtime_arn(cfg), sid, runtime_workspace, sync_root,
                        sync_mod.baseline_path(cfg, runtime_workspace),
                        sync_options["bootstrap"], sync_options["conflict"], "once",
                        storage_backend,
                        session_epoch,
                    )
                )
            except procs.SyncStartError as exc:
                die("sync preflight failed; task was not submitted: {}".format(exc))

        if handoff:
            # Seed-then-handoff: the branch is provisioned above, so the
            # import never sees an empty worktree on a freshly seeded
            # workspace. Stdout stays pure task_id: the imported sessionID
            # goes to stderr.
            imported_id = handoff_mod.upload_and_import(
                cfg, ws, export_path, resolved, runtime_workspace, local_version
            )
            print(
                "sch: handed off local session as remote '{}'".format(imported_id),
                file=sys.stderr,
            )
            continue_flag = True

        payload = runtime.payload_task(
            runtime_workspace, harness, prompt, continue_flag, timeout_s,
            storage_backend, session_epoch, model=model, variant=variant,
        )

        print(
            "sch: submitting headless task for workspace '{}' (harness={})...".format(
                ws, harness
            ),
            file=sys.stderr,
        )
        result = runtime.invoke_verified(cfg, sid, payload, "task")
        if not result.ok:
            die("task invocation failed")

        ack_status = result.get("status", "unknown")
        ack_task_id = result.get("task_id", "") or ""
        ack_warning = result.get("warning", "") or ""

        if ack_status != "accepted":
            extra = ", task_id={}".format(ack_task_id) if ack_task_id else ""
            die("task not accepted (status={}{})".format(ack_status, extra))
        if not ack_task_id:
            die("task accepted but no task_id returned")
        if model and result.get("model", "") != model:
            # Older runtime image that ignores the unknown `model` key: the task
            # was already accepted, so warn-and-continue (design D4 of
            # add-task-model-flag) — the task runs with the harness's default
            # model and there is no cancel action to invoke instead.
            print(
                "sch: warning: runtime did not echo requested model '{}'; "
                "the runtime image may not support --model and the task runs "
                "with the default model (continuing)".format(model),
                file=sys.stderr,
            )
        if variant and result.get("variant", "") != variant:
            # Same contract as --model above (spec R8a): an image predating
            # the flag runs the turn with the default effort.
            print(
                "sch: warning: runtime did not echo requested variant '{}'; "
                "the runtime image may not support --variant and the task runs "
                "with the default effort (continuing)".format(variant),
                file=sys.stderr,
            )
        if continue_flag and result.get("continue", False) is not True:
            # Same contract as `sch run --continue` and the --model echo
            # above (TASK-27): an image predating the feature ignores the
            # unknown `continue` key and always starts a fresh session.
            print(
                "sch: warning: runtime did not echo requested --continue; "
                "the runtime image may not support it and the task starts "
                "from a fresh session (continuing)",
                file=sys.stderr,
            )

        print(ack_task_id)
        if ack_warning:
            print("sch: WARNING {}".format(ack_warning), file=sys.stderr)
        workspace.mark_status(cfg, ws, "task-submitted")
        return 0
    finally:
        if export_path:
            try:
                os.remove(export_path)
            except OSError:
                pass
