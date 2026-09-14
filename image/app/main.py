"""SCH shim: AgentCore Runtime contract on :8080.

Minimal by design (see design D1, Phase 0): OpenCode does NOT run as a daemon
here. This process only:

- satisfies the AgentCore Runtime contract (``/invocations`` + ``/ping``)
  via ``BedrockAgentCoreApp``;
- runs ``init-workspace.sh`` (idempotent) at startup so the session
  workspace is ready before any interactive shell is opened;
- answers ``noop`` / ``info`` / ``checkpoint`` / ``prepare-run`` invocations,
  used to force provisioning of the microVM, inspect image version /
  workspace state, drive the L2 checkpoint cycle, and arm the `sch run`
  run-once harness autostart;
- runs the L2 durability checkpoint loop (sch-l2-durability-s3-checkpoint):
  periodic + synchronous checkpoint of the whole workspace to S3, and
  automatic restore from S3 when the session storage is found empty at
  boot (session storage expiry, runtime version update, or a rotated
  ``runtimeSessionId`` for the same workspace).

The OpenCode TUI is launched by the user inside the interactive shell
(``agentcore exec --it`` -> ``opencode``), not by this shim.
"""

import atexit
import asyncio
import calendar
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3
from bedrock_agentcore import BedrockAgentCoreApp

from tunnel_framing import FramedPeer, CloseRequested
from fs_sync_lease import FsSyncLeaseRegistry
import telegram_notifier
import telegram_interaction

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("sch_shim")

SESSION_WORKSPACE_ROOT = Path("/mnt/workspace")
S3_WORKSPACE_ROOT = Path("/home/sch/workspace")
WORKSPACE_ROOT = Path(os.environ.get("SCH_WORKSPACE_ROOT", str(SESSION_WORKSPACE_ROOT)))
REPO_DIR = WORKSPACE_ROOT / "repo"
STATE_DIR = WORKSPACE_ROOT / "state"
ACTIVE_WORKSPACE_FILE = Path("/home/sch/.sch-workspace.json")
SESSION_RESTORE_MARKER = SESSION_WORKSPACE_ROOT / ".sch-restore-promotion.json"
INIT_SCRIPT = Path("/app/init-workspace.sh")
# Mirrors init-workspace.sh's own resolution — used by _verify_workspace_seeded()
# to confirm the seed actually landed on the real mount (see FRESH_SETTLE_WAIT).
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(STATE_DIR / "config")))
OPENCODE_CONFIG_FILE = CONFIG_DIR / "opencode" / "opencode.json"

# --- Multi-harness (sch-multi-harness, design D1/D2/D3) ------------------------
# Claude Code state lives on LOCAL disk (CLAUDE_CONFIG_DIR) and is mirrored to
# /mnt/workspace/state/claude by the L2 checkpoint loop (same pattern as
# opencode.db: fcntl/async-restore hazards rule out putting it on the mount
# directly). The wrapper dispatcher gates on per-harness readiness markers.
CLAUDE_CONFIG_DIR_LOCAL = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/home/sch/.claude"))
CLAUDE_STATE_REPLICA = STATE_DIR / "claude"
CLAUDE_READY_MARKER = CLAUDE_CONFIG_DIR_LOCAL / ".ready"
# sch-context7-builtin follow-up fix: Claude Code only ever reads a
# project-scoped `.mcp.json` from the PROJECT ROOT directory (cwd where
# `claude` is launched) — it does NOT read `$CLAUDE_CONFIG_DIR/.mcp.json`
# (confirmed against code.claude.com/docs/en/mcp: "Claude Code doesn't read
# paths such as ~/.claude/.mcp.json"; verified empirically against a live
# session — moving the file to the project root is what makes `claude mcp
# list` actually see aws-docs/aws-mcp/context7 instead of reporting no
# servers configured at all). The file now lives on the mount (REPO_DIR,
# already durable via the existing repo.tar.gz L2 checkpoint — no new
# checkpoint plumbing needed), not on local disk, so it needs no L2
# mirror/restore of its own (unlike settings.json, which stays under
# CLAUDE_CONFIG_DIR_LOCAL and is still mirrored via _mirror_claude_state).
CLAUDE_MCP_FILE = REPO_DIR / ".mcp.json"

# --- Pi harness (add-pi-harness, design D2/D3/D4/D5) ---------------------------
# Pi state follows the claude pattern exactly: the live write path is
# $PI_CODING_AGENT_DIR (local disk — the mount has no fcntl support and its
# asynchronous restore is a clobber hazard), mirrored to
# /mnt/workspace/state/pi by the L2 checkpoint loop and restored from there on
# empty-L1 boot before the readiness marker is written. Pi stores only JSONL
# (no SQLite), so the directory mirror needs no special handling.
PI_CONFIG_DIR_LOCAL = Path(os.environ.get("PI_CODING_AGENT_DIR", "/home/sch/.pi/agent"))
PI_STATE_REPLICA = STATE_DIR / "pi"
PI_READY_MARKER = PI_CONFIG_DIR_LOCAL / ".ready"
# Seed canary for _verify_workspace_seeded: settings.json is the artifact
# without which the TUI would start on Pi's own default provider (`google`),
# which has no credentials in this microVM (design D2/D8).
PI_SETTINGS_FILE = PI_CONFIG_DIR_LOCAL / "settings.json"
# Role system prompts seeded by init-workspace.sh (design D4): Pi has no agent
# files, so the role contract is passed with --append-system-prompt.
PI_ROLE_REMOTE_AUTO = PI_CONFIG_DIR_LOCAL / "roles" / "remote-auto.md"
# Pi's native provider for the Bedrock execution-role path (design D3).
PI_BEDROCK_PROVIDER = "amazon-bedrock"

# --- Git-native workflow (add-git-native-workflow, design D2/D3/D4/D7) ---------
# Bundle staging path (task 1.1): OUTSIDE the repo worktree by design (the
# bundle must never appear in the agent's worktree or in the repo.tar.gz
# checkpoint), but INSIDE STATE_DIR so it follows the storage backend's
# canonical root (`/mnt/workspace/state/bundles` for storage=session,
# `/home/sch/workspace/state/bundles` for storage=s3 — both rebound by
# _configure_workspace_paths) and is reachable by the tunnel file channel
# (an exec-mode fs_sync_worker rooted here). The seed metadata file lives in
# STATE_DIR too, so it rides the existing state.tar.gz L2 checkpoint and a
# workspace restored from checkpoint keeps knowing its branch/baseSha
# (recovery scenario, spec git-native-workflow "Recovering work after a
# session interruption").
BUNDLE_STAGING_DIR = STATE_DIR / "bundles"
GIT_NATIVE_STATE_FILE = STATE_DIR / "git-native.json"
SEED_BUNDLE_NAME = "seed.bundle"
DELIVERY_BUNDLE_NAME = "delivery.bundle"
HANDOFF_FILE_NAME = "handoff.json"
# Default harness for a raw shell opened without `sch` (no payload, no marker):
# OpenCode — the multi-harness contract is opt-in of the `sch` wrapper (spec:
# harness-selection, "Assenza di harness nel payload non seleziona il
# default"). New workspaces created via `sch` default to `claude` client-side.
DEFAULT_HARNESS = "opencode"
SUPPORTED_HARNESSES = ("opencode", "claude", "pi")
SUPPORTED_STORAGE_BACKENDS = ("s3", "session")

# --- OpenCode DB backup/restore (design D2, revised twice) ---------------------
# The session storage mount does not support POSIX fcntl locks (ENOLCK), so
# SQLite cannot run on it. opencode.db lives on LOCAL disk (OPENCODE_DB) and is
# checkpointed to the mount: restore at boot, periodic backup, best-effort
# backup at exit. The backup destination uses the unix-dotfile VFS (flock-free,
# works on the mount) — but only for storage=session; for storage=s3 the backup
# path is on local disk where the default VFS works.
#
# Second revision (WAL regression, sch-fix-db-backup-wal): opencode >= 1.18
# keeps its sqlite DB in WAL journal mode, and the backup API copies the WAL
# header into the destination. Re-opening an existing WAL-headered file through
# the unix-dotfile VFS fails ("unable to open database file": WAL needs shared
# memory, which that VFS cannot provide) — so backing up in place worked
# exactly once per file lifetime and then failed forever, silently freezing the
# day-0 (empty) backup and losing every session on the next microVM recycle.
# Fix: every pass backs up into a FRESH temp file (equivalent to the
# always-working first pass) and atomically renames it over DB_BACKUP_PATH.
OPENCODE_DB_LOCAL = Path(os.environ.get("OPENCODE_DB", "/home/sch/.opencode/opencode.db"))
DB_BACKUP_PATH = Path(
    os.environ.get(
        "SCH_DB_BACKUP_PATH", str(STATE_DIR / "data" / "opencode" / "opencode.db.backup")
    )
)
DB_BACKUP_INTERVAL = max(10, int(os.environ.get("SCH_DB_BACKUP_INTERVAL", "60")))
# Marker ON THE MOUNT: distinguishes "fresh session" from "resumed session
# whose storage is still being restored asynchronously". Also carries the
# workspace identity (design D2) across resumes, once known.
MOUNT_MARKER = STATE_DIR / ".sch-initialized"
# Marker on LOCAL disk: workspace ready for use (opencode wrapper gates on it).
READY_MARKER = Path("/home/sch/.opencode/.ready")
# Run-once autostart marker (`sch run`, action=prepare-run): consumed by
# /etc/profile.d/sch-run.sh in the FIRST interactive login shell, which then
# cd's into /mnt/workspace/repo and exec's the workspace's harness. Local disk
# on purpose: per-microVM, never checkpointed, gone on restart.
RUN_ONCE_MARKER = Path(os.environ.get("SCH_RUN_ONCE_MARKER", "/home/sch/.sch-run-once.json"))

# --- Per-user provider API keys (add-user-provider-keys, design D3) ------------
# The provider keys are a property of the USER, not of the deployment: `sch`
# reads them from the caller's ~/.sch/env and forwards them in the payload of
# EVERY invoke. This shim stages them on tmpfs — never on the checkpointed
# mount, never in the persisted shim state, never in a log — and the harness
# dispatcher (scripts/harness-wrapper.sh) reads them back from there, which is
# also what makes them reachable from `agentcore exec --it` login shells (they
# inherit no container ENV).
#
# /run/sch is created by the image (0700, owned by the runtime user): measured
# in a live AgentCore microVM, /run is root-owned 0755 and NOT a separate tmpfs
# mount, so the dir must exist in the image for a uid-1000 shim to write it. What
# the design asks for holds either way — the path is outside /mnt/workspace and
# outside /home/sch, so no L2 tarball, DB backup or state mirror can pick it up,
# and the content dies with the microVM (spec: user-provider-keys, "Nessuna
# persistenza").
#
# Semantics are TOTAL REPLACEMENT — "ultimo invoke vince", no merge, no epoch, no
# writer fence (design D4): each invoke carries the full set, an invoke without
# the field clears it. The staging happens BEFORE the storage-backend barrier the
# bootstrap thread waits on, so the keys are on disk before the seeding script
# recomputes the claude reconciliation and before any harness process is spawned.
# TASK-19 (cross-account Bedrock): BEDROCK_API_KEY carries an Amazon Bedrock
# API key (bearer token) issued by a DIFFERENT account than the deployment's.
# Staged and dispatched like any provider key; the dispatcher maps it onto
# AWS_BEARER_TOKEN_BEDROCK, which only Bedrock clients consume — checkpoint S3,
# DynamoDB, AgentCore and the aws-mcp MCP server keep riding the execution role.
# TASK-26 (opt-in GitHub access): GITHUB_TOKEN carries the operator's GitHub
# token (fine-grained PAT). It rides the same staging, transport and secrecy
# rules, but it is NOT a model provider: the dispatcher maps it onto
# GH_TOKEN/GITHUB_TOKEN for `gh`, and the git-native reconciliation below
# applies it to the repo's credential helper — only on git-native workspaces,
# never persisted (spec git-native-workflow, "Opt-in GitHub access").
PROVIDER_KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENCODE_API_KEY",
    "OPENROUTER_API_KEY",
    "KILO_API_KEY",
    "BEDROCK_API_KEY",
    "GITHUB_TOKEN",
)
# The SCH_ prefix keeps the staged names INERT: nothing auto-detects them, and
# the dispatcher is the single decision point that maps them onto the canonical
# provider names, per harness (unchanged from add-provider-api-keys).
PROVIDER_KEY_ENV_NAMES = tuple("SCH_" + name for name in PROVIDER_KEY_NAMES)
# Overridable for container-free tests only; the real path is fixed and shared
# with harness-wrapper.sh.
PROVIDER_KEYS_FILE = Path(
    os.environ.get("SCH_PROVIDER_KEYS_FILE", "/run/sch/provider-keys.env")
)
# Git credential store for the opt-in GitHub access (TASK-26): holds the
# single `https://x-access-token:<token>@github.com` line the repo-local
# `credential.helper` points at. Same tmpfs dir, same 0600/atomic/total-
# replacement discipline as PROVIDER_KEYS_FILE — and likewise never under a
# checkpoint root, so the token dies with the microVM.
GIT_CREDENTIALS_FILE = Path(
    os.environ.get("SCH_GIT_CREDENTIALS_FILE", "/run/sch/git-credentials")
)

# --- Remote UI tunnel (sch-remote-ui-tunnel, design D2/D4/D5, post-D9 pivot) ---
# The bridge for `sch attach`/`sch acp` uses InvokeAgentRuntimeWithWebSocketStream
# (see design.md "Nota di revisione"), a distinct operation from the
# interactive shell channel — no run-once marker or PTY handoff needed: the
# target (tcp port or exec argv) arrives as the WebSocket's first message
# (see the `@app.websocket` handler below), and this same shim process pumps
# bytes directly via asyncio. No shared budget with `sch shell`/`sch open`/
# `sch run` (D9 resolved by removal of the shared-channel dependency).
# Fixed local port `opencode web` listens on inside the microVM for `sch
# attach` and `sch web`. Fixed (not random) so `serve-ensure` can probe
# liveness without an extra discovery round-trip; matches OpenCode's own
# documented example port for `opencode attach <url>` (opencode --help).
OPENCODE_SERVE_PORT = int(os.environ.get("SCH_OPENCODE_SERVE_PORT", "4096"))
# Orphan timeout for the websocket tunnel handler: if the local bridge dies
# without sending a clean close frame, tear down the target after this many
# seconds of silence (design D4, same default as the retired PTY helper).
TUNNEL_ORPHAN_TIMEOUT_S = int(os.environ.get("SCH_TUNNEL_ORPHAN_TIMEOUT", "1800"))
# How long to wait for an asynchronously-restored mount to settle when no
# hint is available (restore visibility lag observed empirically: 30-60s).
MOUNT_SETTLE_TIMEOUT = int(os.environ.get("SCH_MOUNT_SETTLE_TIMEOUT", "120"))
# How long to wait when the caller explicitly hinted "resumed".
RESUME_WAIT = int(os.environ.get("SCH_RESUME_WAIT", "180"))
# Short mandatory settle wait even when hinted "fresh" (discovered debugging
# "no MCP tools after first opencode launch, works after close+reopen"):
# hint="fresh" used to return INSTANTLY with zero wait, assuming there is
# nothing to "restore". But the session-storage mount attachment itself
# (not just content restore) can be asynchronous w.r.t. container/exec
# readiness — a fast interactive user can reach a shell and read
# /mnt/workspace/state/config/opencode/opencode.json before the real mount
# has attached, racing init-workspace.sh's writes (which may land on a
# temporary/local placeholder at that path, later shadowed once the real
# mount attaches). This bounded wait plus the verify-and-retry loop in
# _bootstrap() are belt-and-braces against that race.
FRESH_SETTLE_WAIT = int(os.environ.get("SCH_FRESH_SETTLE_WAIT", "5"))
# Bounded retries for the verify-after-seed check in _bootstrap() (also
# reused, with the same knobs, for the L2 restore verify-and-retry, D4).
INIT_VERIFY_RETRIES = int(os.environ.get("SCH_INIT_VERIFY_RETRIES", "6"))
INIT_VERIFY_RETRY_WAIT = int(os.environ.get("SCH_INIT_VERIFY_RETRY_WAIT", "3"))
# The fs tunnel can arrive immediately after the warm-up invocation returns,
# while the asynchronous workspace bootstrap is still creating the worktree.
# Bound that wait so a failed bootstrap does not leave a tunnel opening forever.
FS_WORKSPACE_READY_TIMEOUT_S = int(os.environ.get("SCH_FS_WORKSPACE_READY_TIMEOUT", "60"))
# `sch run --continue` (prepare-run) consults the restored session store to
# pick the session to resume, so on a cold boot it must wait for the bootstrap
# (L2 restore included) like the headless task worker does (TASK-27/TASK-29,
# decision-8). Same 240 s bound as the task worker; the CLI raises the aws CLI
# read timeout accordingly, and AgentCore allows 15 min per synchronous request.
PREPARE_RUN_READY_TIMEOUT_S = int(os.environ.get("SCH_PREPARE_RUN_READY_TIMEOUT", "240"))

# --- L2 durability checkpoint (design D1/D2/D3/D6/D7) --------------------------
# S3 bucket injected by the runtime stack (infra/agent_runtime.yaml); empty
# means the feature is simply off (e.g. an older stack not yet migrated).
CHECKPOINT_BUCKET = os.environ.get("SCH_CHECKPOINT_BUCKET", "").strip()
# Default 60s, min 10s; "0" degrades to Phase-0 behaviour (db -> mount only,
# no S3 upload at all) without any redeploy (spec: runtime-image).
_raw_checkpoint_interval = int(os.environ.get("SCH_CHECKPOINT_INTERVAL", "60"))
CHECKPOINT_INTERVAL = 0 if _raw_checkpoint_interval <= 0 else max(10, _raw_checkpoint_interval)
# Local (non-mount) scratch space for building tar.gz archives before upload;
# never left on the mount, never counted against the 1GB session budget.
CHECKPOINT_TMP_DIR = Path(os.environ.get("SCH_CHECKPOINT_TMP_DIR", "/tmp/sch-checkpoint"))
# sch generates runtimeSessionId as sch-<workspace>-<uuid4> (bin/sch); used
# as the fallback workspace-identity derivation (design D2) when no explicit
# hint is available. The workspace name itself may contain hyphens, so the
# fixed-shape UUIDv4 suffix anchors the split from the right.
WORKSPACE_FROM_SESSION_ID_RE = re.compile(
    r"^sch-(?P<ws>.+)-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
WORKSPACE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")

# --- CommandShell client presence --------------------------------------------
# Presence is intentionally ephemeral and separate from _INTERACTIVE_ACTIVE:
# the latter describes whether an interactive harness exists, while these
# short leases describe whether a local CommandShell client can currently see
# it. Hooks read the atomic snapshot without needing shim or Telegram secrets.
COMMAND_SHELL_PRESENCE_FILE = Path(
    os.environ.get("SCH_COMMAND_SHELL_PRESENCE_FILE", "/tmp/sch-command-shell-presence.json")
)
COMMAND_SHELL_PRESENCE_TTL_MIN_S = 5
COMMAND_SHELL_PRESENCE_TTL_MAX_S = 120
COMMAND_SHELL_PRESENCE_MAX_LEASES = 128
COMMAND_SHELL_PRESENCE_HISTORY_MAX = 256
COMMAND_SHELL_PRESENCE_HISTORY_RETENTION_S = 300
COMMAND_SHELL_PRESENCE_SWEEP_S = 1
COMMAND_SHELL_PRESENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TELEGRAM_ENABLED_MARKER = Path(
    os.environ.get("SCH_TELEGRAM_ENABLED_MARKER", "/tmp/sch-telegram-enabled")
)

app = BedrockAgentCoreApp()

BOOT_STATE: dict = {"phase": "starting"}
_WORKSPACE_READY = threading.Event()
# Caller-provided storage hint ("fresh"/"resumed"), delivered via the first
# invocation payload; cuts the boot classification wait short.
_STORAGE_HINT: dict = {"value": None}
_STORAGE_BACKEND: dict = {"value": None}
_STORAGE_BACKEND_LOCK = threading.Lock()
# Workspace identity (design D2): set from the first invocation payload that
# carries it ({"workspace": "<ws>"}), else derived from the runtimeSessionId
# prefix, else (if a resumed session) read back from the mount marker.
_WORKSPACE_NAME: dict = {"value": None}
# Most recently observed runtimeSessionId (context.session_id), recorded in
# checkpoint manifests as "the session that produced this checkpoint".
_SESSION_ID: dict = {"value": None}
_SESSION_EPOCH: dict = {"value": 0}
_SESSION_EPOCH_LOCK = threading.Lock()
_WRITER_TOKEN = uuid.uuid4().hex
_WRITER_CLAIMED: dict = {"workspace": None}
_WRITER_CLAIM_LOCK = threading.Lock()
# Multi-harness (design D1): per-workspace persisted harness choice, set from
# the first invocation payload that carries it ({"harness": "<opencode|claude>"}),
# else read back from the mount marker on a resumed session. Drives init-
# workspace.sh seeding (via SCH_HARNESS env), the L2 claude state replica,
# the per-harness headless argv, and orphan reconciliation.
_HARNESS: dict = {"value": None}

# --- Telegram notifications (add-telegram-notifications, design D1/D2/D5) ------
# Opt-in push channel: fully inert unless SCH_TELEGRAM_BOT_TOKEN and
# SCH_TELEGRAM_CHAT_ID are both set (telegram_notifier.enabled()). The
# notifier singleton is created at bootstrap (phase=ready) and owns the only
# thread that ever talks to api.telegram.org; the shim's own threads emit
# through _telegram_notify (non-blocking, never raising). That singleton only
# exists at the end of the boot sequence, so an event emitted earlier (the
# `task` action is servable before bootstrap completes) is written to the
# notifier's spool dir and delivered at its first poll instead of being
# dropped (add-task-liveness-safety, design D6). The per-workspace
# topic mapping lives NEXT TO the checkpoint objects on S3
# (checkpoints/<ws>/telegram-topic.json) so a later microVM of the same
# workspace reuses the same forum topic (spec: 'Un topic Telegram per
# workspace'). It is deliberately NOT writer-fenced: the mapping is shared
# observability state, not authoritative workspace state.
TELEGRAM_TOPIC_OBJECT = "telegram-topic.json"
# Stall threshold (task 2.2, OQ-N1 default: 5 minutes).
TELEGRAM_STALL_SECONDS = max(60, int(os.environ.get("SCH_TELEGRAM_STALL_SECONDS", "300")))
_TELEGRAM: dict = {
    "notifier": None,
    "interaction": None,
    "broker": None,
    "marker_cleanup_registered": False,
}
# task_ids already notified as stalled (spec: at most one stall per task).
_TELEGRAM_STALLED_TASKS: set = set()

# --- Interactive busy keep-alive (add-interactive-busy-keepalive) ---------------
# Root cause addressed: only headless tasks advertise HealthyBusy on /ping
# (add_async_task in _run_task), so a turn started interactively (run TUI
# after detach, attach/web, Telegram injection) never pauses AgentCore's
# 15-min idle clock and the microVM gets reaped MID-TURN with no signal.
# The opencode plugin (sch-telegram.js) now writes per-session activity
# markers under SCH_ACTIVITY_DIR from ANY opencode process; the busy-watcher
# thread below holds an advisory async task while at least one session shows
# fresh "busy" activity, and releases it on clean idle, staleness or cap.
ACTIVITY_DIR = Path(os.environ.get("SCH_ACTIVITY_DIR", "/tmp/sch-activity"))
BUSY_POLL_SECONDS = max(1, int(os.environ.get("SCH_BUSY_POLL_SECONDS", "3")))
# A "busy" marker older than this is stale: the writing process most likely
# died (or wedged) without emitting session.idle. Must stay comfortably above
# the longest silent stretch of a legitimate turn (a long-running tool emits
# no part updates between tool.execute.before and .after).
BUSY_STALE_SECONDS = max(60, int(os.environ.get("SCH_BUSY_STALE_SECONDS", "600")))
# Hard cap on one continuous busy episode: without it a wedged-but-chatty
# turn would keep the (billed) microVM alive until the 8h MaxLifetime.
BUSY_MAX_HOLD_SECONDS = max(300, int(os.environ.get("SCH_BUSY_MAX_HOLD_SECONDS", "7200")))
# Activity marker files older than this are garbage-collected by the watcher.
BUSY_GC_SECONDS = max(BUSY_STALE_SECONDS * 2, 21600)
_BUSY_LOCK = threading.Lock()
_BUSY_STATE: dict = {
    "handle": None,      # advisory async-task handle while held
    "since": None,       # epoch when the current hold started
    "capped": False,     # cap reached: do not re-hold until the episode ends
    "sessions": [],      # sessions considered fresh-busy at the last tick
    "last_release_reason": None,
}

# --- Telegram inbound interaction (add-telegram-interaction, design D1/D3) -----
# Opt-in on top of the notification channel: BOTH tables must be present in
# the environment (set by the stack only when ENABLE_TELEGRAM_INTERACTION is
# on) AND the notifier must be configured. Absent -> the shim behaves
# byte-identically to the notification-only deployment (design D7).
TELEGRAM_COMMANDS_TABLE = os.environ.get("SCH_TELEGRAM_COMMANDS_TABLE", "").strip()
TELEGRAM_ROUTING_TABLE = os.environ.get("SCH_TELEGRAM_ROUTING_TABLE", "").strip()
# Routing key for the plain-chat fallback (chat without Topics) — must match
# telegram_webhook_handler.PLAIN_CHAT_THREAD_KEY.
TELEGRAM_PLAIN_CHAT_THREAD_KEY = "chat"

# In-memory checkpoint status, exposed via the `info` action (spec:
# runtime-image, "Checkpoint status observability"). Reset on shim
# restart within the same microVM: harmless, at most one extra full upload.
CHECKPOINT_STATE: dict = {
    "last_result": None,
    "last_attempt_utc": None,
    "last_success_utc": None,
    "last_fingerprints": {},
    "last_sizes": {},
}
# Serializes the periodic loop and the synchronous `checkpoint` action
# (design D3): never two checkpoint passes uploading concurrently. The
# periodic loop is a single sequential thread by construction, so a slow
# cycle simply delays the next tick rather than piling up work.
_CHECKPOINT_LOCK = threading.Lock()

# --- Headless task state (Fase 1, sch-headless-tasks) --------------------------
# Application timeout strictly below AgentCore MaxLifetime (8h) so the shim
# owns the terminal state (design D7). Default 7h; clamp to safe range.
_TASK_TIMEOUT_DEFAULT = 25200
_TASK_TIMEOUT_MIN = 5
_TASK_TIMEOUT_MAX = 27900
_raw_task_timeout = int(os.environ.get("SCH_TASK_TIMEOUT_S", str(_TASK_TIMEOUT_DEFAULT)))
SCH_TASK_TIMEOUT_S = max(_TASK_TIMEOUT_MIN, min(_TASK_TIMEOUT_MAX, _raw_task_timeout))
TASK_STATUS_OBJECT = "task-status.json"
# Terminal-notification delivery protocol shared with the external watchdog
# (TASK-28, spec telegram-notifications R10). Present in a terminal record
# only when the Telegram channel is configured: "pending" is written together
# with the terminal state, "delivered" (plus notified_utc/notified_by) once
# the Bot API accepted the message. A record still pending after the
# watchdog's grace period is re-sent from outside the microVM.
NOTIFICATION_STATUS_FIELD = "notification_status"
NOTIFICATION_PENDING = "pending"
NOTIFICATION_DELIVERED = "delivered"
NOTIFICATION_FIELDS = (NOTIFICATION_STATUS_FIELD, "notified_utc", "notified_by")
# Keep the terminal harness response inspectable through `sch status` without
# letting an unexpectedly verbose task create an oversized status object.
TASK_OUTPUT_MAX_CHARS = 12000
# Per-invocation auto-approval flag(s) for `opencode run` headless (OQ3).
# The pinned opencode-ai (OPENCODE_VERSION in image/Dockerfile) uses `--auto`
# for auto-approving permissions that are not explicitly denied. This is
# scoped to the argv of the headless run only; the TUI path is unchanged.
_TASK_AUTO_APPROVE_FLAGS = [
    f.strip()
    for f in os.environ.get("SCH_TASK_AUTO_APPROVE_FLAGS", "--auto").split(",")
    if f.strip()
]

# Agent selection for `opencode run` headless (sch-remote-agents): the seeded
# custom primary agent `remote-auto` carries the unattended-execution contract
# (never wait for input, verify, commit, compact final report — see
# image/opencode-templates/agents/remote-auto.md). Argv-only, same scoping
# rationale as the auto-approve flag above: the TUI path keeps the seeded
# `default_agent` (remote-interactive). Override with SCH_TASK_AGENT; set it
# to the empty string to fall back to OpenCode's default agent. Unknown agent
# names are safe: `opencode run --agent <missing>` warns and falls back to
# the default agent instead of failing.
_TASK_AGENT = os.environ.get("SCH_TASK_AGENT", "remote-auto").strip()

# Single in-process task slot per workspace (design D2/D3).
_TASK_LOCK = threading.Lock()
_TASK_STATE: dict = {
    "task_id": None,
    "state": "idle",  # idle | running | succeeded | failed | timed-out | interrupted
    "prompt": None,
    "started_utc": None,
    "finished_utc": None,
    "heartbeat_utc": None,
    "exit_code": None,
    "opencode_session_id": None,
    "model": None,  # explicit per-invocation model, None = harness default
    "error": None,
    "thread": None,
    "async_task_handle": None,
}
# In-memory mirror of the persisted task status, exposed via `info`.
_TASK_STATUS: dict = {"state": "none"}
# Advisory flag set by `sch shell` / cleared by `sch stop` via mark-interactive.
_INTERACTIVE_ACTIVE: bool = False

# --- Remote UI tunnel (sch-remote-ui-tunnel, design D5) -------------------------
# Supervised `opencode web` process for `sch attach` and `sch web`. Started lazily on the
# first `serve-ensure` action, restarted on crash by the supervisor thread.
# Single instance per microVM (one workspace = one microVM = at most one
# opencode harness process), so no per-shellId dimension here (unlike a
# future channel pool, which is a client-side/local-bridge concept, not a
# remote-process one — the remote side only ever needs ONE OpenCode backend
# regardless of how many local channels eventually connect to it).
_SERVE_LOCK = threading.Lock()
_SERVE_STATE: dict = {
    "proc": None,
    "port": None,
    "started_utc": None,
    "restart_count": 0,
    "supervisor_started": False,
}


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CommandShellPresenceRegistry:
    """Bounded, thread-safe registry of short-lived CommandShell leases."""

    def __init__(
        self,
        snapshot_path: Path = COMMAND_SHELL_PRESENCE_FILE,
        *,
        max_leases: int = COMMAND_SHELL_PRESENCE_MAX_LEASES,
        history_max: int = COMMAND_SHELL_PRESENCE_HISTORY_MAX,
        history_retention_s: float = COMMAND_SHELL_PRESENCE_HISTORY_RETENTION_S,
        now_fn=time.time,
        on_attached=None,
    ):
        self.snapshot_path = Path(snapshot_path)
        self.max_leases = max(1, max_leases)
        self.history_max = max(1, history_max)
        self.history_retention_s = max(0.0, history_retention_s)
        self._now = now_fn
        self._on_attached = on_attached
        self._lock = threading.Lock()
        self._leases: dict[tuple[str, str], dict] = {}
        self._history: list[dict] = []
        with self._lock:
            self._publish_locked(self._valid_now())

    @staticmethod
    def _validate_identifier(name: str, value) -> str:
        if not isinstance(value, str) or not COMMAND_SHELL_PRESENCE_ID_RE.fullmatch(value):
            raise ValueError(
                f"invalid {name}: expected 1-128 characters from [A-Za-z0-9._:-]"
            )
        return value

    @staticmethod
    def bounded_ttl(ttl_s) -> float:
        if (
            not isinstance(ttl_s, (int, float))
            or isinstance(ttl_s, bool)
            or not float(ttl_s).is_integer()
            or not 0 < float(ttl_s) < float("inf")
        ):
            raise ValueError("invalid ttl_s: expected a positive whole number")
        return float(
            max(
                COMMAND_SHELL_PRESENCE_TTL_MIN_S,
                min(COMMAND_SHELL_PRESENCE_TTL_MAX_S, int(ttl_s)),
            )
        )

    def _valid_now(self, now=None) -> float:
        value = self._now() if now is None else now
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not 0 <= float(value) < float("inf")
        ):
            raise ValueError("invalid clock value")
        return float(value)

    def _record_locked(
        self,
        ts: float,
        reason: str,
        shell_id: str | None = None,
        attachment_id: str | None = None,
    ) -> None:
        entry = {
            "ts": ts,
            "state": "attached" if self._leases else "detached",
            "reason": reason,
        }
        if shell_id is not None:
            entry["shellId"] = shell_id
        if attachment_id is not None:
            entry["attachmentId"] = attachment_id
        self._history.append(entry)

    def _record_transition_locked(
        self,
        was_attached: bool,
        ts: float,
        reason: str,
        shell_id: str | None = None,
        attachment_id: str | None = None,
    ) -> None:
        if was_attached != bool(self._leases):
            self._record_locked(ts, reason, shell_id, attachment_id)

    def _prune_history_locked(self, now: float) -> None:
        cutoff = now - self.history_retention_s
        baseline = None
        retained = []
        for entry in self._history:
            if entry["ts"] < cutoff:
                baseline = entry
            else:
                retained.append(entry)
        if baseline is not None:
            retained.insert(0, {
                "ts": cutoff,
                "state": baseline["state"],
                "reason": "retained-baseline",
            })
        if len(retained) > self.history_max:
            if baseline is not None and self.history_max > 1:
                retained = [retained[0], *retained[-(self.history_max - 1):]]
            else:
                retained = retained[-self.history_max:]
        self._history[:] = retained

    def _expire_locked(self, now: float) -> bool:
        was_attached = bool(self._leases)
        stale = sorted(
            (
                (lease["expires_at"], key)
                for key, lease in self._leases.items()
                if lease["expires_at"] <= now
            ),
            key=lambda item: item[0],
        )
        for expires_at, key in stale:
            lease = self._leases.pop(key, None)
            if lease is not None and not self._leases:
                self._record_transition_locked(
                    was_attached,
                    expires_at,
                    "expired",
                    lease["shellId"],
                    lease["attachmentId"],
                )
        self._prune_history_locked(now)
        return bool(stale)

    def _snapshot_locked(self, now: float) -> dict:
        leases = sorted(
            (dict(lease) for lease in self._leases.values()),
            key=lambda lease: (lease["shellId"], lease["attachmentId"]),
        )
        return {
            "version": 1,
            "state": "attached" if leases else "detached",
            "generated_at": now,
            "expires_at": max((lease["expires_at"] for lease in leases), default=None),
            "leases": leases,
            "history": [dict(entry) for entry in self._history],
        }

    def _publish_locked(self, now: float) -> None:
        snapshot = self._snapshot_locked(now)
        path = self.snapshot_path
        temp = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        except OSError as exc:
            logger.warning("cannot publish CommandShell presence snapshot: %s", exc)
        finally:
            temp.unlink(missing_ok=True)

    def update(self, shell_id, attachment_id, state, ttl_s, *, now=None) -> dict:
        shell_id = self._validate_identifier("shellId", shell_id)
        attachment_id = self._validate_identifier("attachmentId", attachment_id)
        if state not in ("attached", "detached"):
            raise ValueError("invalid state: expected 'attached' or 'detached'")
        ttl = self.bounded_ttl(ttl_s)
        current = self._valid_now(now)
        key = (shell_id, attachment_id)
        became_attached = False
        with self._lock:
            expired = self._expire_locked(current)
            was_attached = bool(self._leases)
            existing = self._leases.get(key)
            if state == "attached":
                if existing is None and len(self._leases) >= self.max_leases:
                    if expired:
                        self._publish_locked(current)
                    raise ValueError("CommandShell presence registry is full")
                self._leases[key] = {
                    "shellId": shell_id,
                    "attachmentId": attachment_id,
                    "attached_at": existing["attached_at"] if existing else current,
                    "expires_at": current + ttl,
                }
                self._record_transition_locked(
                    was_attached,
                    current,
                    "renewed" if existing else "attached",
                    shell_id,
                    attachment_id,
                )
                changed = True
            else:
                changed = self._leases.pop(key, None) is not None
                if changed:
                    self._record_transition_locked(
                        was_attached, current, "detached", shell_id, attachment_id
                    )
            self._prune_history_locked(current)
            if changed or expired:
                self._publish_locked(current)
            snapshot = self._snapshot_locked(current)
            became_attached = not was_attached and snapshot["state"] == "attached"
        if became_attached and self._on_attached is not None:
            try:
                self._on_attached()
            except Exception as exc:  # noqa: BLE001
                logger.warning("CommandShell attach callback failed: %s", exc)
        return {
            "state": snapshot["state"],
            "attached": snapshot["state"] == "attached",
            "ttl_s": int(ttl),
            "lease_count": len(snapshot["leases"]),
            "expires_at": snapshot["expires_at"],
        }

    def set_on_attached(self, callback) -> None:
        with self._lock:
            self._on_attached = callback

    def expire_stale(self, *, now=None) -> bool:
        current = self._valid_now(now)
        with self._lock:
            changed = self._expire_locked(current)
            if changed:
                self._publish_locked(current)
            return changed

    def current_attached(self, *, now=None) -> bool:
        current = self._valid_now(now)
        with self._lock:
            if self._expire_locked(current):
                self._publish_locked(current)
            return bool(self._leases)

    def attached_at_emission(self, emitted_at, *, now=None) -> bool:
        current = self._valid_now(now)
        if (
            not isinstance(emitted_at, (int, float))
            or isinstance(emitted_at, bool)
            or not 0 <= float(emitted_at) <= current
            or current - float(emitted_at) > self.history_retention_s
        ):
            return False
        emitted = float(emitted_at)
        with self._lock:
            if self._expire_locked(current):
                self._publish_locked(current)
            state = "detached"
            for transition in self._history:
                if transition["ts"] > emitted:
                    break
                state = transition["state"]
            return state == "attached"

    def snapshot(self, *, now=None) -> dict:
        current = self._valid_now(now)
        with self._lock:
            if self._expire_locked(current):
                self._publish_locked(current)
            return self._snapshot_locked(current)


_COMMAND_SHELL_PRESENCE = CommandShellPresenceRegistry()


def command_shell_current_attached(*, now=None) -> bool:
    return _COMMAND_SHELL_PRESENCE.current_attached(now=now)


def command_shell_attached_at_emission(emitted_at, *, now=None) -> bool:
    return _COMMAND_SHELL_PRESENCE.attached_at_emission(emitted_at, now=now)


def command_shell_snapshot_attached(path: Path | None = None, *, now=None) -> bool:
    """Fail-open-to-detached reader for hooks and other local consumers."""
    current = time.time() if now is None else now
    try:
        if (
            not isinstance(current, (int, float))
            or isinstance(current, bool)
            or not 0 <= float(current) < float("inf")
        ):
            return False
        data = json.loads((path or COMMAND_SHELL_PRESENCE_FILE).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            return False
        leases = data.get("leases")
        if data.get("state") != "attached" or not isinstance(leases, list) or not leases:
            return False
        valid_until = []
        for lease in leases:
            if not isinstance(lease, dict):
                return False
            _COMMAND_SHELL_PRESENCE._validate_identifier("shellId", lease.get("shellId"))
            _COMMAND_SHELL_PRESENCE._validate_identifier(
                "attachmentId", lease.get("attachmentId")
            )
            attached_at = lease.get("attached_at")
            expires_at = lease.get("expires_at")
            if (
                not isinstance(attached_at, (int, float))
                or isinstance(attached_at, bool)
                or not isinstance(expires_at, (int, float))
                or isinstance(expires_at, bool)
                or not 0 <= float(attached_at) < float(expires_at) < float("inf")
            ):
                return False
            valid_until.append(float(expires_at))
        snapshot_expiry = data.get("expires_at")
        return (
            isinstance(snapshot_expiry, (int, float))
            and not isinstance(snapshot_expiry, bool)
            and float(snapshot_expiry) == max(valid_until)
            and float(current) < max(valid_until)
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _command_shell_presence_expiry_loop() -> None:
    while True:
        time.sleep(COMMAND_SHELL_PRESENCE_SWEEP_S)
        try:
            _COMMAND_SHELL_PRESENCE.expire_stale()
        except Exception as exc:  # noqa: BLE001
            logger.warning("CommandShell presence expiry sweep failed: %s", exc)


def _checkpoint_enabled() -> bool:
    return bool(CHECKPOINT_BUCKET) and CHECKPOINT_INTERVAL > 0


def _configure_workspace_paths(storage_backend: str) -> None:
    global WORKSPACE_ROOT, REPO_DIR, STATE_DIR, CONFIG_DIR  # noqa: PLW0603
    global OPENCODE_CONFIG_FILE, CLAUDE_STATE_REPLICA, CLAUDE_MCP_FILE  # noqa: PLW0603
    global PI_STATE_REPLICA  # noqa: PLW0603
    global DB_BACKUP_PATH, MOUNT_MARKER  # noqa: PLW0603
    global BUNDLE_STAGING_DIR, GIT_NATIVE_STATE_FILE  # noqa: PLW0603

    WORKSPACE_ROOT = S3_WORKSPACE_ROOT if storage_backend == "s3" else SESSION_WORKSPACE_ROOT
    REPO_DIR = WORKSPACE_ROOT / "repo"
    STATE_DIR = WORKSPACE_ROOT / "state"
    CONFIG_DIR = STATE_DIR / "config"
    OPENCODE_CONFIG_FILE = CONFIG_DIR / "opencode" / "opencode.json"
    CLAUDE_STATE_REPLICA = STATE_DIR / "claude"
    CLAUDE_MCP_FILE = REPO_DIR / ".mcp.json"
    # add-pi-harness: the pi L2 replica follows the storage backend's root like
    # every other on-mount path (the LIVE pi dir is on local disk and never moves).
    PI_STATE_REPLICA = STATE_DIR / "pi"
    DB_BACKUP_PATH = STATE_DIR / "data" / "opencode" / "opencode.db.backup"
    MOUNT_MARKER = STATE_DIR / ".sch-initialized"
    BUNDLE_STAGING_DIR = STATE_DIR / "bundles"
    GIT_NATIVE_STATE_FILE = STATE_DIR / "git-native.json"
    os.environ["SCH_WORKSPACE_ROOT"] = str(WORKSPACE_ROOT)
    os.environ["XDG_DATA_HOME"] = str(STATE_DIR / "data")
    os.environ["XDG_CONFIG_HOME"] = str(CONFIG_DIR)
    ACTIVE_WORKSPACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = ACTIVE_WORKSPACE_FILE.with_name(
        f".{ACTIVE_WORKSPACE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temp.write_text(json.dumps({
            "storage": storage_backend,
            "root": str(WORKSPACE_ROOT),
            "repo": str(REPO_DIR),
            "state": str(STATE_DIR),
        }))
        os.chmod(temp, 0o600)
        os.replace(temp, ACTIVE_WORKSPACE_FILE)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _set_storage_backend(storage_backend: str | None) -> bool:
    if not storage_backend:
        return True
    value = storage_backend.strip().lower()
    if value not in SUPPORTED_STORAGE_BACKENDS:
        logger.warning("ignoring invalid storage backend '%s'", value)
        return False
    with _STORAGE_BACKEND_LOCK:
        current = _STORAGE_BACKEND["value"]
        if current and current != value:
            logger.warning("storage backend mismatch: active=%s requested=%s", current, value)
            return False
        if current is None:
            _configure_workspace_paths(value)
            _STORAGE_BACKEND["value"] = value
            logger.info("storage backend set: %s (root=%s)", value, WORKSPACE_ROOT)
        return True


def _resolve_storage_backend() -> str:
    return _STORAGE_BACKEND["value"] or "session"


def _set_session_epoch(value: int) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return False
    with _SESSION_EPOCH_LOCK:
        current = _SESSION_EPOCH["value"]
        if current and value != current:
            return False
        if value:
            _SESSION_EPOCH["value"] = value
        return True


def _derive_workspace_from_session_id(session_id) -> str | None:
    if not session_id:
        return None
    m = WORKSPACE_FROM_SESSION_ID_RE.match(session_id)
    return m.group("ws") if m else None


def _set_workspace_name(name: str | None) -> None:
    if name and not WORKSPACE_IDENTITY_RE.fullmatch(name):
        logger.warning("ignoring unsafe workspace identity")
        return
    if name and _WORKSPACE_NAME["value"] is None:
        _WORKSPACE_NAME["value"] = name
        logger.info("workspace identity set: %s", name)


def _resolve_workspace_name() -> str | None:
    """Best-known workspace identity right now (design D2): the in-memory
    value (payload hint or session-id fallback, already applied by
    _set_workspace_name), else the mount marker (covers resumed sessions
    whose shim process restarted without a fresh hint)."""
    if _WORKSPACE_NAME["value"]:
        return _WORKSPACE_NAME["value"]
    marker_ws = _read_marker().get("workspace")
    if marker_ws:
        _WORKSPACE_NAME["value"] = marker_ws
        return marker_ws
    return None


def _set_harness(harness: str | None) -> None:
    """Record the workspace's harness choice (from payload or marker). First
    non-None wins (a subsequent divergent payload is rejected at the task
    entrypoint, not silently overwritten — spec: harness-selection,
    "Mutual exclusivity per workspace")."""
    if not harness:
        return
    h = harness.strip().lower()
    if h not in SUPPORTED_HARNESSES:
        logger.warning("ignoring unknown harness value '%s' (expected %s)", h, SUPPORTED_HARNESSES)
        return
    if _HARNESS["value"] is None:
        _HARNESS["value"] = h
        # Propagate to the env so init-workspace.sh (subprocess.run inherits
        # the parent env) and the headless task subprocess both see it.
        os.environ["SCH_HARNESS"] = h
        logger.info("harness set: %s", h)


def _resolve_harness() -> str:
    """Best-known harness right now: in-memory value (payload/marker already
    applied by _set_harness), else the mount marker (covers resumed sessions
    whose shim process restarted without a fresh payload), else the image
    default (opencode — multi-harness is opt-in of `sch`; raw shells without
    `sch` keep the OpenCode-only behavior)."""
    if _HARNESS["value"]:
        return _HARNESS["value"]
    marker_h = _read_marker().get("harness")
    if marker_h and marker_h in SUPPORTED_HARNESSES:
        _HARNESS["value"] = marker_h
        os.environ["SCH_HARNESS"] = marker_h
        return marker_h
    return DEFAULT_HARNESS


# --- Per-user provider keys: staging + child environment (design D3/D7) --------


def _sanitize_provider_keys(value) -> dict:
    """Allowlist filter for the payload's ``provider_keys`` field.

    Only the names of :data:`PROVIDER_KEY_NAMES` survive, and only with a
    non-empty single-line string value: an empty credential makes a harness fail
    in a confusing way, so "sent but empty" must behave exactly like "not sent"
    (same rule the dispatcher applies). Nothing here is ever logged — a value
    never leaves this function except into the 0600 tmpfs file.
    """
    if not isinstance(value, dict):
        return {}
    keys = {}
    for name in PROVIDER_KEY_NAMES:
        raw = value.get(name)
        if not isinstance(raw, str):
            continue
        raw = raw.strip()
        if not raw or "\n" in raw or "\r" in raw or "\0" in raw:
            continue
        keys[name] = raw
    return keys


def _stage_provider_keys(payload: dict) -> list:
    """Write the payload's provider keys to the tmpfs staging file.

    Total replacement (design D4): the file ends up containing EXACTLY the set
    carried by this invoke — an invoke without the field removes it. Atomic
    (write to a sibling temp file with mode 0600, then rename) so a harness
    launching concurrently either sees the whole old set or the whole new one,
    never a half-written file.

    Returns the list of key NAMES staged, for logging/diagnostics. Never
    raises: a failure here must not fail the invocation (the session simply
    stays Bedrock-only, exactly like a user with no keys configured).
    """
    keys = _sanitize_provider_keys(payload.get("provider_keys"))
    try:
        if not keys:
            # Absent field == "this user has no provider keys": leave no file
            # behind, so a rotation that REMOVES a key really removes it.
            with contextlib.suppress(FileNotFoundError):
                PROVIDER_KEYS_FILE.unlink()
            return []
        PROVIDER_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(PROVIDER_KEYS_FILE.parent, 0o700)
        tmp_path = PROVIDER_KEYS_FILE.with_name(PROVIDER_KEYS_FILE.name + ".tmp")
        body = "".join(
            "SCH_{}={}\n".format(name, keys[name]) for name in PROVIDER_KEY_NAMES
            if name in keys
        )
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, PROVIDER_KEYS_FILE)
        return sorted(keys)
    except Exception as exc:  # noqa: BLE001 — never fail an invocation over this
        # Loud but non-fatal: the session degrades to Bedrock-only, which is a
        # silent capability loss unless it is reported. The most likely cause is
        # a /run/sch that the runtime user cannot write (the image creates it
        # 0700 sch:sch at build time — /run is root-owned in the running
        # microVM, so a rebuilt image is the remedy).
        logger.error(
            "could not stage provider keys (%d configured) at %s: %s — this "
            "session will have no external provider (Bedrock only)",
            len(keys), PROVIDER_KEYS_FILE, exc,
        )
        return []


def _read_staged_provider_keys() -> dict:
    """The staged keys as ``{SCH_<NAME>: value}``, or ``{}``.

    The single reader of the staging file on the Python side; the dispatcher has
    its own (shell) reader for the login-shell path.
    """
    try:
        text = PROVIDER_KEYS_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    env = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if not sep or name not in PROVIDER_KEY_ENV_NAMES or not value:
            continue
        env[name] = value
    return env


def _child_env_with_provider_keys(env: dict = None) -> dict:
    """A child-process environment carrying exactly the staged key set.

    Used for every process this shim spawns that may talk to a provider
    (headless tasks, the `opencode web`/serve supervisor, init-workspace.sh).
    The names are POPPED first: the set is authoritative, so a key removed
    by the user must disappear from the child's environment even if this shim
    process (or a previous deployment's container ENV) still had it — that is
    what makes the claude Bedrock rollback work (design D6).
    """
    child = dict(os.environ if env is None else env)
    for name in PROVIDER_KEY_ENV_NAMES:
        child.pop(name, None)
    child.update(_read_staged_provider_keys())
    return child


def _resolve_latest_claude_session() -> str | None:
    """Return the most-recently-modified Claude Code session id (JSONL
    basename) for the current worktree, or None on any failure (degrade to a
    fresh session — same contract as _resolve_latest_opencode_session, design
    D5). Claude Code stores sessions as
    ``$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session-id>.jsonl``; the
    ``<encoded-cwd>`` is Claude's path-encoding (slashes → hyphens), derived
    here from the worktree's absolute path so we don't depend on Claude
    internals. An upstream layout change produces an empty match → degraded
    behavior (fresh session) rather than a crash (design D5 risk)."""
    try:
        encoded_cwd = str(REPO_DIR).replace("/", "-")
        projects_dir = CLAUDE_CONFIG_DIR_LOCAL / "projects" / encoded_cwd
        if not projects_dir.is_dir():
            return None
        jsonl_files = sorted(
            (p for p in projects_dir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not jsonl_files:
            return None
        return jsonl_files[0].stem  # basename without .jsonl
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to resolve latest claude session: %s", exc)
        return None


def _harness_ready_marker(harness: str) -> Path:
    """The readiness marker the dispatcher (harness-wrapper.sh) gates on for
    ``harness``. Per-harness on purpose: writing only the resolved harness's
    marker means a stray invocation of another harness's binary waits instead of
    starting against a workspace seeded for something else (design D3)."""
    if harness == "claude":
        return CLAUDE_READY_MARKER
    if harness == "pi":
        return PI_READY_MARKER
    return READY_MARKER


def _pi_session_dir_name(cwd: Path | str) -> str:
    """Pi's own cwd→session-directory encoding, verified against 0.84.2
    (dist/core/session-manager.js getDefaultSessionDirPath): the resolved cwd
    loses its leading separator, every remaining ``/``, ``\\`` and ``:`` becomes
    ``-``, and the result is wrapped in ``--``. Kept as a named function so a
    layout change between pre-1.0 versions is a one-line fix with a test
    (design D5 / OQ-PI-SESSIONS)."""
    resolved = str(cwd)
    body = re.sub(r"^[/\\]", "", resolved)
    body = re.sub(r"[/\\:]", "-", body)
    return f"--{body}--"


def _resolve_latest_pi_session() -> str | None:
    """Return the most-recently-modified Pi session file path for the current
    worktree, or None on any failure (degrade to a fresh session — same
    contract as the opencode and claude resolvers, design D5).

    Pi stores sessions as
    ``$PI_CODING_AGENT_DIR/sessions/<cwd-encoded>/<timestamp>_<uuid>.jsonl``
    with a first line ``{"type":"session","version":3,"id":...,"cwd":...}``.
    The ABSOLUTE PATH is returned rather than the uuid: ``--session`` accepts
    either, and a path needs no id extraction (one fewer place to break when the
    header schema moves). The header is still validated so a stale/foreign file
    that happens to live in the directory is not resumed.

    Pi is pre-1.0 and this on-disk layout is explicitly treated as unstable
    (design.md OQ-PI-SESSIONS): any mismatch returns None, which starts a fresh
    session instead of failing the task. The documented fallback if the layout
    turns out to churn across versions is to drop this resolver and pass Pi's
    native ``-c`` (resume the latest session for the cwd) instead — same
    behavior, no observability of the resolved id."""
    try:
        sessions_dir = PI_CONFIG_DIR_LOCAL / "sessions" / _pi_session_dir_name(REPO_DIR)
        if not sessions_dir.is_dir():
            return None
        candidates = sorted(
            (p for p in sessions_dir.glob("*.jsonl") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            with candidate.open("r", encoding="utf-8") as handle:
                first_line = handle.readline()
            try:
                header = json.loads(first_line)
            except ValueError:
                continue
            if not isinstance(header, dict) or header.get("type") != "session":
                continue
            if header.get("version") != 3 or header.get("cwd") != str(REPO_DIR):
                continue
            if not header.get("id"):
                continue
            return str(candidate)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to resolve latest pi session: %s", exc)
        return None


def _mirror_pi_state() -> str:
    """Mirror $PI_CODING_AGENT_DIR → /mnt/workspace/state/pi (the L2 replica on
    the mount). Same contract as _mirror_claude_state: recursive, mtime-
    preserving, idempotent, never deletes — Pi's JSONL sessions are append-only
    so the per-tick cost stays low (design D2)."""
    try:
        if not PI_CONFIG_DIR_LOCAL.exists():
            return "no-local-pi-state"
        PI_STATE_REPLICA.parent.mkdir(parents=True, exist_ok=True)
        PI_STATE_REPLICA.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            str(PI_CONFIG_DIR_LOCAL),
            str(PI_STATE_REPLICA),
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".ready"),
        )
        # Readiness belongs to the current boot and must never survive via L2.
        (PI_STATE_REPLICA / ".ready").unlink(missing_ok=True)
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("pi state mirror failed: %s", exc)
        return f"error: {exc}"


def _restore_pi_state() -> str:
    """Restore $PI_CODING_AGENT_DIR from /mnt/workspace/state/pi on empty-L1
    boot, BEFORE the pi readiness marker is written, so the `pi` binary finds
    the restored JSONL sessions on its first launch (spec: runtime-image,
    "Stato Pi su disco locale e replica L2 sul mount"). Never overwrites L1
    content that already exists (never-overwrite-L1, same as claude)."""
    try:
        if not PI_STATE_REPLICA.exists():
            return "no-replica"
        if PI_CONFIG_DIR_LOCAL.exists() and any(PI_CONFIG_DIR_LOCAL.iterdir()):
            # L1 already populated — never clobber (anti-clobber guard, D4).
            return "l1-present"
        PI_CONFIG_DIR_LOCAL.mkdir(parents=True, exist_ok=True)
        # Checkpoints produced before .ready was excluded may still contain it.
        (PI_STATE_REPLICA / ".ready").unlink(missing_ok=True)
        shutil.copytree(
            str(PI_STATE_REPLICA),
            str(PI_CONFIG_DIR_LOCAL),
            dirs_exist_ok=True,
        )
        PI_READY_MARKER.unlink(missing_ok=True)
        logger.info("restored pi state from %s", PI_STATE_REPLICA)
        return "restored"
    except Exception as exc:  # noqa: BLE001
        logger.error("pi state restore failed: %s", exc, exc_info=True)
        return f"error: {exc}"


def _mirror_claude_state() -> str:
    """Mirror $HOME/.claude (CLAUDE_CONFIG_DIR_LOCAL, the live local-disk
    path the `claude` binary writes to) → /mnt/workspace/state/claude (the L2
    replica on the mount). rsync-style: copy files, preserving mtimes; cheap
    because JSONL transcripts are append-only and small between ticks
    (design D2). Idempotent; never deletes (no L1→L2 clobber risk: the L2
    replica is the persistence copy, L1 is always authoritative on restore)."""
    try:
        if not CLAUDE_CONFIG_DIR_LOCAL.exists():
            return "no-local-claude-state"
        CLAUDE_STATE_REPLICA.parent.mkdir(parents=True, exist_ok=True)
        CLAUDE_STATE_REPLICA.mkdir(parents=True, exist_ok=True)
        # Use shutil.copytree with dirs_exist_ok=True for a recursive, mtime-
        # preserving overwrite. Faster than a per-file loop and Python-native.
        shutil.copytree(
            str(CLAUDE_CONFIG_DIR_LOCAL),
            str(CLAUDE_STATE_REPLICA),
            dirs_exist_ok=True,
        )
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("claude state mirror failed: %s", exc)
        return f"error: {exc}"


def _restore_claude_state() -> str:
    """Restore $HOME/.claude from /mnt/workspace/state/claude (the L2 replica
    on the mount) on empty-L1 boot, BEFORE the readiness marker is written
    (design D2 / D9, task 6.3). Never overwrites L1 content that already
    exists (never-overwrite-L1 property, same as opencode.db restore)."""
    try:
        if not CLAUDE_STATE_REPLICA.exists():
            return "no-replica"
        if CLAUDE_CONFIG_DIR_LOCAL.exists() and any(CLAUDE_CONFIG_DIR_LOCAL.iterdir()):
            # L1 already populated — never clobber (anti-clobber guard, D4).
            return "l1-present"
        CLAUDE_CONFIG_DIR_LOCAL.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            str(CLAUDE_STATE_REPLICA),
            str(CLAUDE_CONFIG_DIR_LOCAL),
            dirs_exist_ok=True,
        )
        logger.info("restored claude state from %s", CLAUDE_STATE_REPLICA)
        return "restored"
    except Exception as exc:  # noqa: BLE001
        logger.error("claude state restore failed: %s", exc, exc_info=True)
        return f"error: {exc}"


def _wait_for_mount_restore() -> str:
    """Wait for the session storage content to appear after resume.

    The session storage restore is ASYNCHRONOUS with respect to container
    start (content becomes visible with a 30-60s lag on resume). Callers that
    know whether the session is fresh or resumed can hint via the first
    invocation payload ({"storage": "fresh"|"resumed"}); otherwise fall back
    to a timeout-based classification:
    - marker/content appears within the window -> resumed;
    - nothing appears                          -> fresh.
    """
    if _resolve_storage_backend() == "s3":
        return "s3-local"
    deadline = time.monotonic() + MOUNT_SETTLE_TIMEOUT
    extended = False
    observed_content = False
    while time.monotonic() < deadline:
        hint = _STORAGE_HINT["value"]
        if hint == "fresh":
            logger.info("fresh hinted: settling %ss before seeding (mount attach race guard)",
                        FRESH_SETTLE_WAIT)
            fresh_deadline = time.monotonic() + FRESH_SETTLE_WAIT
            while time.monotonic() < fresh_deadline:
                if SESSION_RESTORE_MARKER.exists():
                    _recover_incomplete_session_restore()
                    return "fresh-incomplete-restore-recovered"
                time.sleep(0.25)
            return "fresh (hinted)"
        if hint == "resumed" and not extended:
            deadline = time.monotonic() + RESUME_WAIT
            extended = True
            logger.info("resume hinted: extending mount wait to %ss", RESUME_WAIT)
        if SESSION_RESTORE_MARKER.exists():
            _recover_incomplete_session_restore()
            return "incomplete-restore-recovered"
        if MOUNT_MARKER.exists() or DB_BACKUP_PATH.exists() or (REPO_DIR / ".git").exists():
            observed_content = True
        time.sleep(1)
    if SESSION_RESTORE_MARKER.exists():
        _recover_incomplete_session_restore()
        return "late-incomplete-restore-recovered"
    if observed_content:
        return "resumed-settled"
    return "fresh" if not extended else "resume-hinted-but-empty"


def _mount_storage_empty() -> bool:
    """True when neither the worktree nor prior OpenCode state show any
    sign of pre-existing content. Anti-clobber guard for L2 restore (design
    D4): L1 data, if any, ALWAYS wins over an S3 checkpoint."""
    try:
        repo_has_content = REPO_DIR.exists() and any(REPO_DIR.iterdir())
        data_dir = STATE_DIR / "data"
        data_has_content = data_dir.exists() and any(data_dir.iterdir())
        return not repo_has_content and not data_has_content
    except OSError:
        return False


def _recover_incomplete_session_restore() -> None:
    if _resolve_storage_backend() != "session" or not SESSION_RESTORE_MARKER.exists():
        return
    logger.warning("recovering incomplete session-storage restore promotion")
    shutil.rmtree(REPO_DIR, ignore_errors=True)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    for path in SESSION_WORKSPACE_ROOT.glob(".sch-restore-*"):
        shutil.rmtree(path, ignore_errors=True)
    SESSION_RESTORE_MARKER.unlink(missing_ok=True)


def _session_restore_guard_loop() -> None:
    while True:
        if SESSION_RESTORE_MARKER.exists():
            logger.critical(
                "late session restore promotion appeared after readiness; terminating fail-closed"
            )
            _WORKSPACE_READY.clear()
            BOOT_STATE["phase"] = "error"
            BOOT_STATE["error"] = "late session restore promotion"
            READY_MARKER.unlink(missing_ok=True)
            CLAUDE_READY_MARKER.unlink(missing_ok=True)
            PI_READY_MARKER.unlink(missing_ok=True)
            os._exit(75)
        time.sleep(0.5)


def _read_marker() -> dict:
    try:
        return json.loads(MOUNT_MARKER.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write_marker(**updates) -> None:
    data = _read_marker()
    data.update(updates)
    try:
        MOUNT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        MOUNT_MARKER.write_text(json.dumps(data))
    except OSError as exc:
        logger.warning("cannot write mount marker: %s", exc)


def _restore_db() -> str:
    """Restore opencode.db from the mount backup (fresh microVM boot).

    The session-storage restore is asynchronous AND unordered: the marker
    file can appear before the (larger) DB backup file. If the marker says a
    backup should exist, wait for it explicitly.
    """
    try:
        if OPENCODE_DB_LOCAL.exists():
            # Shim restart within the same microVM: keep the (newer) local DB.
            return "local-db-present"
        if _read_marker().get("db_backup") and not DB_BACKUP_PATH.exists():
            logger.info("marker promises a DB backup; waiting for it to appear...")
            deadline = time.monotonic() + MOUNT_SETTLE_TIMEOUT * 2
            while time.monotonic() < deadline and not DB_BACKUP_PATH.exists():
                time.sleep(1)
        if not DB_BACKUP_PATH.exists():
            return "backup-promised-but-missing" if _read_marker().get("db_backup") else "no-backup"
        OPENCODE_DB_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(DB_BACKUP_PATH, OPENCODE_DB_LOCAL)
        logger.info("restored opencode.db from %s", DB_BACKUP_PATH)
        return "restored"
    except Exception as exc:  # noqa: BLE001
        logger.error("DB restore failed: %s", exc, exc_info=True)
        return f"error: {exc}"


def _db_backup_tmp_path() -> Path:
    """Staging destination for _backup_db_once (WAL regression fix): a
    sibling of DB_BACKUP_PATH so the final os.replace stays on the same
    filesystem (atomic)."""
    return DB_BACKUP_PATH.with_name(DB_BACKUP_PATH.name + ".tmp")


def _backup_db_once() -> str:
    """Consistent snapshot of the local DB into active workspace state."""
    if not OPENCODE_DB_LOCAL.exists():
        return "no-local-db"
    try:
        # Safety guard: if the boot classified the session as fresh (no
        # backup found) but a checkpoint has appeared on the mount since
        # (late asynchronous restore), do NOT clobber it silently — move it
        # aside first so no data is ever lost.
        if BOOT_STATE.get("phase") == "ready" \
                and BOOT_STATE.get("restore_result") == "no-backup" \
                and DB_BACKUP_PATH.exists() \
                and not BOOT_STATE.get("late_backup_handled"):
            conflict = DB_BACKUP_PATH.with_suffix(f".conflict-{int(time.time())}")
            shutil.move(DB_BACKUP_PATH, conflict)
            BOOT_STATE["late_backup_handled"] = True
            logger.warning("late checkpoint found on mount; preserved as %s", conflict)
        DB_BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        # WAL regression fix (see module comment at DB_BACKUP_PATH): always
        # back up into a FRESH file — sqlite cannot re-open an existing
        # WAL-headered file through the unix-dotfile VFS — then atomically
        # rename over the real backup path (checkpoint/restore readers never
        # see a half-written backup).
        tmp_path = _db_backup_tmp_path()
        tmp_path.unlink(missing_ok=True)
        src = sqlite3.connect(str(OPENCODE_DB_LOCAL))
        if _resolve_storage_backend() == "s3":
            # storage=s3: DB_BACKUP_PATH is on local disk — the default VFS
            # (full fcntl support) works; no dotfile workaround needed.
            dst = sqlite3.connect(str(tmp_path))
        else:
            dst = sqlite3.connect(f"file:{tmp_path}?vfs=unix-dotfile", uri=True)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            src.close()
            dst.close()
        os.replace(tmp_path, DB_BACKUP_PATH)
        _write_marker(db_backup=True, last_backup_utc=_utcnow())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.error("DB backup failed: %s", exc)
        return f"error: {exc}"


def _backup_db_durable() -> dict:
    """Persist only the DB before acknowledging a handoff."""
    if _resolve_storage_backend() != "s3":
        result = _backup_db_once()
        return {"status": "ok" if result == "ok" else "error", "db_backup": result}
    workspace = _resolve_workspace_name()
    if not workspace or not CHECKPOINT_BUCKET:
        return {"status": "error", "db_backup": "missing workspace or checkpoint bucket"}
    bootstrap = False
    with _CHECKPOINT_LOCK:
        result = _backup_db_once()
        if result != "ok":
            return {"status": "error", "db_backup": result}
        try:
            previous = _download_manifest(workspace)
        except ManifestReadError as exc:
            return {"status": "error", "db_backup": str(exc)}
        if not previous or previous.get("published") is False:
            bootstrap = True
        else:
            previous_etag = previous.get("_etag")
            manifest = {key: value for key, value in previous.items() if key != "_etag"}
            artifacts = dict(manifest.get("artifacts") or {})
            generation = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
            artifact_key = f"checkpoint-generations/{workspace}/{generation}/opencode.db.backup"
            if not _upload_file_key(DB_BACKUP_PATH, artifact_key):
                return {"status": "error", "db_backup": "db-upload"}
            artifacts["opencode.db.backup"] = artifact_key
            sizes = dict(manifest.get("sizes") or {})
            sizes["db"] = DB_BACKUP_PATH.stat().st_size
            manifest.update({
                "ts": _utcnow(), "generation": generation,
                "artifacts": artifacts, "sizes": sizes,
            })
            try:
                _s3().put_object_tagging(
                    Bucket=CHECKPOINT_BUCKET, Key=artifact_key,
                    Tagging={"TagSet": [{"Key": "active", "Value": "true"}]},
                )
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "db_backup": f"artifact-tag: {exc}"}
            if not _upload_manifest(workspace, manifest, previous_etag):
                return {"status": "error", "db_backup": "manifest-upload"}
            return {"status": "ok", "db_backup": "ok", "artifact": artifact_key}

    if bootstrap:
        # A fresh S3 workspace has no repo/state artifact references to inherit
        # yet. Bootstrap that commit point once; later handoffs stay DB-only.
        initial = _do_checkpoint(force=True)
        if initial.get("status") != "ok" or not initial.get("manifest_written"):
            detail = ", ".join(initial.get("errors") or []) or initial.get("status", "unknown")
            return {"status": "error", "db_backup": f"base checkpoint failed: {detail}"}
        return {"status": "ok", "db_backup": "ok", "bootstrap": True}


# --- L2 checkpoint engine: fingerprints (design D3) -----------------------------

def _git_head(repo_dir: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() if out.returncode == 0 else "no-head"
    except Exception:  # noqa: BLE001
        return "no-head"


def _git_status_hash(repo_dir: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain=v1"],
            capture_output=True, text=True, timeout=30,
        )
        content = out.stdout if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        content = ""
    return hashlib.sha256(content.encode()).hexdigest()


def _tree_fingerprint(
    path: Path,
    exclude_dirs: frozenset = frozenset(),
    exclude_files: frozenset = frozenset(),
) -> str:
    """Content-aware identity including paths, modes, content and deletions."""
    digest = hashlib.sha256()
    if not path.exists():
        digest.update(b"absent")
        return digest.hexdigest()
    for root, dirs, files in os.walk(path):
        root_path = Path(root)
        kept_dirs = []
        for name in sorted(dirs):
            entry = root_path / name
            if entry in exclude_dirs:
                continue
            relative = entry.relative_to(path).as_posix().encode()
            if entry.is_symlink():
                digest.update(b"L")
                digest.update(relative)
                try:
                    digest.update(os.readlink(entry).encode())
                except OSError as exc:
                    digest.update(str(exc).encode())
                continue
            digest.update(b"D")
            digest.update(relative)
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            file_path = root_path / name
            if file_path in exclude_files:
                continue
            try:
                stat = file_path.lstat()
                relative = file_path.relative_to(path).as_posix().encode()
                if file_path.is_symlink():
                    digest.update(b"L")
                    digest.update(relative)
                    digest.update(os.readlink(file_path).encode())
                    continue
                if not file_path.is_file():
                    continue
                digest.update(b"F")
                digest.update(len(relative).to_bytes(4, "big"))
                digest.update(relative)
                digest.update((stat.st_mode & 0o111).to_bytes(2, "big"))
                digest.update(stat.st_size.to_bytes(8, "big"))
                with open(file_path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
            except OSError as exc:
                digest.update(f"error:{file_path}:{exc}".encode())
    return digest.hexdigest()


def _fingerprint_repo() -> str:
    return _tree_fingerprint(REPO_DIR)


def _fingerprint_state() -> str:
    lock_dir = Path(str(DB_BACKUP_PATH) + ".lock")
    return _tree_fingerprint(
        STATE_DIR,
        exclude_dirs=frozenset({lock_dir}),
        exclude_files=frozenset({DB_BACKUP_PATH, _db_backup_tmp_path()}),
    )


def _fingerprint_db() -> str:
    if not DB_BACKUP_PATH.exists():
        return "absent"
    h = hashlib.sha256()
    try:
        with open(DB_BACKUP_PATH, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        return f"error:{exc}"


def _fingerprint_claude_state() -> str:
    return _tree_fingerprint(CLAUDE_STATE_REPLICA)


def _fingerprint_pi_state() -> str:
    return _tree_fingerprint(PI_STATE_REPLICA)


# --- L2 checkpoint engine: tar archives (design D3/D7) --------------------------

def _run_tar_create(args: list, dest_tar: Path) -> bool:
    """Run a `tar -czf` invocation with a single retry on the transient
    "file changed as we read it" warning (design D3: fuzzy backup of a live
    worktree). If tar still produced a non-empty archive despite a non-zero
    exit, use it anyway — a slightly-inconsistent-but-recent snapshot beats
    losing the checkpoint cycle entirely."""
    last_err = ""
    for attempt in range(2):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0:
            return True
        last_err = (proc.stderr or "").strip()
        if "file changed as we read it" in last_err and attempt == 0:
            logger.warning("tar reported a live file change archiving %s; retrying once",
                            dest_tar.name)
            continue
        if dest_tar.exists() and dest_tar.stat().st_size > 0:
            logger.warning("tar exited %s for %s (%s); using best-effort archive",
                            proc.returncode, dest_tar.name, last_err)
            return True
        logger.error("tar failed for %s: %s", dest_tar.name, last_err)
        return False
    return False


def _create_archive(src_dir: Path, dest_tar: Path, excludes: tuple = ()) -> bool:
    if not src_dir.exists():
        return False
    dest_tar.parent.mkdir(parents=True, exist_ok=True)
    args = ["tar", "-C", str(src_dir.parent), "-czf", str(dest_tar)]
    for ex in excludes:
        try:
            rel = ex.relative_to(src_dir.parent)
        except ValueError:
            continue
        args += ["--exclude", str(rel)]
    args.append(src_dir.name)
    return _run_tar_create(args, dest_tar)


def _extract_archive(tar_path: Path, dest_dir: Path) -> bool:
    if not tar_path.exists():
        return False
    dest_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["tar", "-xzf", str(tar_path), "-C", str(dest_dir)],
        capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        logger.error("extract failed for %s: %s", tar_path.name, (proc.stderr or "").strip())
        return False
    return True


# --- L2 checkpoint engine: S3 (design D1/D6/D7, boto3) --------------------------

_S3_CLIENT_LOCK = threading.Lock()
_s3_client = None


class ManifestReadError(RuntimeError):
    pass


class WriterFenceError(RuntimeError):
    pass


class TaskStatusReadError(RuntimeError):
    pass


def _s3():
    global _s3_client  # noqa: PLW0603
    if _s3_client is None:
        with _S3_CLIENT_LOCK:
            if _s3_client is None:
                _s3_client = boto3.client("s3")
    return _s3_client


def _s3_key(workspace: str, name: str) -> str:
    return f"checkpoints/{workspace}/{name}"


def _error_code(exc: Exception) -> str:
    return (
        getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if hasattr(exc, "response") else ""
    )


def _fence_json_object(key: str, defaults: dict) -> None:
    for _attempt in range(3):
        previous_etag = None
        body = dict(defaults)
        existed = False
        try:
            current = _s3().get_object(Bucket=CHECKPOINT_BUCKET, Key=key)
            existed = True
            previous_etag = current["ETag"]
            decoded = json.loads(current["Body"].read().decode())
            if not isinstance(decoded, dict):
                raise WriterFenceError(f"cannot fence malformed object {key}")
            body.update(decoded)
        except WriterFenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) not in ("NoSuchKey", "404"):
                raise WriterFenceError(f"cannot read fenced object {key}: {exc}") from exc
        current_epoch = body.get("session_epoch", 0)
        if not isinstance(current_epoch, int) or current_epoch > _SESSION_EPOCH["value"]:
            raise WriterFenceError(f"object {key} belongs to a newer session epoch")
        if existed and key.endswith("/manifest.json") and "published" not in decoded:
            body["published"] = True
        body.update({
            "writer_token": _WRITER_TOKEN,
            "session_epoch": _SESSION_EPOCH["value"],
            "session_id": _SESSION_ID["value"],
            "fenced_utc": _utcnow(),
        })
        try:
            _conditional_put_json(key, body, previous_etag)
            return
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) not in ("PreconditionFailed", "412") and "precondition" not in str(exc).lower():
                raise WriterFenceError(f"cannot fence object {key}: {exc}") from exc
    raise WriterFenceError(f"concurrent writers prevented fencing object {key}")


def _claim_writer(workspace: str) -> None:
    if not CHECKPOINT_BUCKET or _WRITER_CLAIMED["workspace"] == workspace:
        return
    key = f"workspace-writers/{workspace}.json"
    with _WRITER_CLAIM_LOCK:
        if _WRITER_CLAIMED["workspace"] == workspace:
            return
        for _attempt in range(5):
            params = {}
            try:
                current = _s3().get_object(Bucket=CHECKPOINT_BUCKET, Key=key)
                current_claim = json.loads(current["Body"].read().decode())
                current_epoch = int(current_claim.get("session_epoch", 0))
                if current_epoch > _SESSION_EPOCH["value"]:
                    raise WriterFenceError(
                        f"workspace writer epoch {current_epoch} is newer than {_SESSION_EPOCH['value']}"
                    )
                if current_epoch == _SESSION_EPOCH["value"] \
                        and current_claim.get("writer_token") not in (None, _WRITER_TOKEN) \
                        and current_claim.get("session_id") != _SESSION_ID["value"]:
                    raise WriterFenceError("workspace epoch already has another writer")
                params["IfMatch"] = current["ETag"]
            except WriterFenceError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _error_code(exc) not in ("NoSuchKey", "404"):
                    raise WriterFenceError(f"cannot read writer claim: {exc}") from exc
                params["IfNoneMatch"] = "*"
            body = {
                "writer_token": _WRITER_TOKEN,
                "session_id": _SESSION_ID["value"],
                "session_epoch": _SESSION_EPOCH["value"],
                "claimed_utc": _utcnow(),
            }
            try:
                _s3().put_object(
                    Bucket=CHECKPOINT_BUCKET, Key=key,
                    Body=json.dumps(body).encode(), ContentType="application/json",
                    **params,
                )
                break
            except Exception as exc:  # noqa: BLE001
                if _error_code(exc) not in ("PreconditionFailed", "412") \
                        and "precondition" not in str(exc).lower():
                    raise WriterFenceError(f"cannot claim workspace writer: {exc}") from exc
        else:
            raise WriterFenceError("concurrent writers prevented workspace claim")
        _assert_writer_claim(workspace, claim_if_needed=False)
        _fence_json_object(
            _s3_key(workspace, "manifest.json"),
            {"published": False},
        )
        _assert_writer_claim(workspace, claim_if_needed=False)
        _fence_json_object(
            _s3_key(workspace, TASK_STATUS_OBJECT),
            {"state": "none"},
        )
        _WRITER_CLAIMED["workspace"] = workspace


def _assert_writer_claim(workspace: str, claim_if_needed: bool = True) -> None:
    if claim_if_needed:
        _claim_writer(workspace)
    key = f"workspace-writers/{workspace}.json"
    try:
        obj = _s3().get_object(Bucket=CHECKPOINT_BUCKET, Key=key)
        claim = json.loads(obj["Body"].read().decode())
    except Exception as exc:  # noqa: BLE001
        raise WriterFenceError(f"cannot verify writer claim: {exc}") from exc
    if claim.get("writer_token") != _WRITER_TOKEN:
        raise WriterFenceError("workspace writer claim was superseded")


def _conditional_put_json(key: str, body: dict, previous_etag: str | None) -> dict:
    params = {"IfMatch": previous_etag} if previous_etag else {"IfNoneMatch": "*"}
    return _s3().put_object(
        Bucket=CHECKPOINT_BUCKET, Key=key,
        Body=json.dumps(body).encode(), ContentType="application/json",
        **params,
    )


def _upload_file(local_path: Path, workspace: str, name: str) -> bool:
    return _upload_file_key(local_path, _s3_key(workspace, name))


def _upload_file_key(local_path: Path, key: str) -> bool:
    try:
        _s3().upload_file(
            str(local_path), CHECKPOINT_BUCKET, key,
            ExtraArgs={"Tagging": "active=candidate"},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("checkpoint upload failed (%s): %s", key, exc)
        return False


def _download_file(workspace: str, name: str, dest: Path) -> bool:
    return _download_key(_s3_key(workspace, name), dest)


def _download_key(key: str, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _s3().download_file(CHECKPOINT_BUCKET, key, str(dest))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("checkpoint download failed (%s): %s", key, exc)
        return False


def _upload_manifest(workspace: str, manifest: dict, previous_etag: str | None = None) -> bool:
    try:
        _assert_writer_claim(workspace)
        if previous_etag:
            current = _s3().get_object(
                Bucket=CHECKPOINT_BUCKET, Key=_s3_key(workspace, "manifest.json")
            )
            current_body = json.loads(current["Body"].read().decode())
            if current_body.get("writer_token") != _WRITER_TOKEN:
                raise WriterFenceError("manifest writer token was superseded")
            previous_etag = current["ETag"]
        manifest = {
            **manifest,
            "writer_token": _WRITER_TOKEN,
            "session_epoch": _SESSION_EPOCH["value"],
            "session_id": _SESSION_ID["value"],
            "published": True,
        }
        _conditional_put_json(
            _s3_key(workspace, "manifest.json"), manifest, previous_etag
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("manifest upload failed for '%s': %s", workspace, exc)
        return False


def _manifest_exists(workspace: str) -> bool:
    try:
        _s3().head_object(Bucket=CHECKPOINT_BUCKET, Key=_s3_key(workspace, "manifest.json"))
        return True
    except Exception:  # noqa: BLE001
        return False


# --- Headless task S3 status helpers (sch-headless-tasks) ----------------------

def _upload_task_status(workspace: str, payload: dict) -> bool:
    """Overwrite-only JSON PUT for the sibling task-status object."""
    if not CHECKPOINT_BUCKET:
        logger.warning("cannot upload task status: SCH_CHECKPOINT_BUCKET unset")
        return False
    try:
        _assert_writer_claim(workspace)
        body = dict(payload)
        body.setdefault("image_version", os.environ.get("SCH_IMAGE_VERSION", "unknown"))
        body.update({
            "writer_token": _WRITER_TOKEN,
            "session_epoch": _SESSION_EPOCH["value"],
            "session_id": _SESSION_ID["value"],
        })
        previous_etag = None
        try:
            current = _s3().get_object(
                Bucket=CHECKPOINT_BUCKET, Key=_s3_key(workspace, TASK_STATUS_OBJECT)
            )
            previous_etag = current["ETag"]
            current_body = json.loads(current["Body"].read().decode())
            if current_body.get("writer_token") != _WRITER_TOKEN:
                raise WriterFenceError("task status writer token was superseded")
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) not in ("NoSuchKey", "404"):
                raise
        _conditional_put_json(
            _s3_key(workspace, TASK_STATUS_OBJECT), body, previous_etag
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("task status upload failed for '%s': %s", workspace, exc)
        return False


def _download_task_status(workspace: str) -> dict:
    """Return the persisted task status, or canonical {'state': 'none'} on missing object."""
    if not CHECKPOINT_BUCKET:
        return {"state": "none"}
    try:
        obj = _s3().get_object(Bucket=CHECKPOINT_BUCKET, Key=_s3_key(workspace, TASK_STATUS_OBJECT))
        return json.loads(obj["Body"].read().decode())
    except Exception as exc:  # noqa: BLE001
        err_code = _error_code(exc)
        if err_code in ("NoSuchKey", "404"):
            return {"state": "none"}
        raise TaskStatusReadError(f"task status download failed: {exc}") from exc


def _mark_task_terminal_notified(workspace: str, task_id: str) -> bool:
    """Flip the persisted terminal record of ``task_id`` to
    ``notification_status: delivered`` (TASK-28).

    Runs on the notifier thread right after the Bot API accepted the
    terminal message. Read-modify-write fenced twice: the writer token must
    still be ours, and the conditional PUT (If-Match on the ETag just read)
    refuses to land on an object that changed underneath — typically a new
    submit that already replaced the record with its own ``running`` state,
    which must never be overwritten with a stale terminal body. Refusing is
    safe in every case: the worst outcome is a duplicate terminal message
    from the watchdog, never a lost one.
    """
    if not CHECKPOINT_BUCKET:
        return False
    key = _s3_key(workspace, TASK_STATUS_OBJECT)
    try:
        _assert_writer_claim(workspace)
        current = _s3().get_object(Bucket=CHECKPOINT_BUCKET, Key=key)
        etag = current["ETag"]
        body = json.loads(current["Body"].read().decode())
        if not isinstance(body, dict) or body.get("task_id") != task_id \
                or body.get("state") == "running":
            logger.info(
                "task status of '%s' moved on from task %s; not marking it delivered",
                workspace, task_id[:8],
            )
            return False
        if body.get("writer_token") != _WRITER_TOKEN:
            raise WriterFenceError("task status writer token was superseded")
        if body.get(NOTIFICATION_STATUS_FIELD) == NOTIFICATION_DELIVERED:
            return True
        body[NOTIFICATION_STATUS_FIELD] = NOTIFICATION_DELIVERED
        body["notified_utc"] = _utcnow()
        body["notified_by"] = "shim"
        _conditional_put_json(key, body, etag)
        return True
    except Exception as exc:  # noqa: BLE001 — never propagate into the notifier
        logger.warning(
            "terminal notification of task %s not marked delivered for '%s': %s",
            task_id[:8], workspace, exc,
        )
        return False


def _reconcile_orphan_task_status(workspace: str) -> None:
    """On shim boot, rewrite a persisted 'running' status to 'interrupted'.

    Multi-harness (task 6.7): the orphan's `harness` field is preserved in
    the interrupted rewrite for observability — useful if the operator wants
    to examine the transcript locally (the harness tells them whether to look
    in opencode.db, ~/.claude/projects/ or ~/.pi/agent/sessions/).
    `Healthy` (not `HealthyBusy`) is restored by virtue of no async task handle
    being registered at boot; a subsequent submit is accepted only for the
    harness matching the persisted marker (enforced in _handle_task_action).

    The rewrite itself is harness-agnostic by construction (add-pi-harness task
    4.7): the persisted status already carries the harness that produced it, and
    the argv for a follow-up submit is rebuilt from that harness by
    _build_headless_argv — so `pi` needs no branch here, only the marker-driven
    mutual-exclusivity check it already goes through."""
    try:
        status = _download_task_status(workspace)
    except TaskStatusReadError as exc:
        raise RuntimeError(str(exc)) from exc
    if status.get("state") != "running":
        return
    now = _utcnow()
    reconciled = {
        **status,
        "state": "interrupted",
        "outcome": "unknown",
        "finished_utc": now,
        "checkpoint_status": "unknown",
    }
    # Preserve the orphan's harness for observability; _resolve_harness()
    # (from the marker) drives the next-submit mutual-exclusivity check.
    if _upload_task_status(workspace, reconciled):
        logger.info(
            "reconciled orphan task status to interrupted for workspace '%s' (harness=%s)",
            workspace, status.get("harness", "unknown"),
        )
    else:
        logger.warning("failed to upload reconciled orphan task status for '%s'", workspace)


# --- Telegram notifier wiring (add-telegram-notifications) ---------------------

def _load_telegram_topic_state(workspace: str) -> dict | None:
    """Persisted topic mapping from the workspace's checkpoint prefix, or None."""
    if not CHECKPOINT_BUCKET:
        return None
    try:
        obj = _s3().get_object(
            Bucket=CHECKPOINT_BUCKET, Key=_s3_key(workspace, TELEGRAM_TOPIC_OBJECT)
        )
        data = json.loads(obj["Body"].read().decode())
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) not in ("NoSuchKey", "404"):
            logger.warning("telegram topic mapping read failed for '%s': %s", workspace, exc)
        return None


def _save_telegram_topic_state(workspace: str, state: dict) -> None:
    """Plain overwrite PUT (no fence: shared observability state — see the
    TELEGRAM_TOPIC_OBJECT comment). Contains only chat_id/thread_id/fallback,
    never the bot token (spec: 'Token never in clear')."""
    if not CHECKPOINT_BUCKET:
        return
    _s3().put_object(
        Bucket=CHECKPOINT_BUCKET,
        Key=_s3_key(workspace, TELEGRAM_TOPIC_OBJECT),
        Body=json.dumps(state).encode(),
        ContentType="application/json",
    )


# --- Telegram inbound interaction wiring (add-telegram-interaction) ------------

_DDB_CLIENT = {"client": None}


def _ddb():
    if _DDB_CLIENT["client"] is None:
        _DDB_CLIENT["client"] = boto3.client("dynamodb")
    return _DDB_CLIENT["client"]


def _telegram_interaction_enabled() -> bool:
    """Inbound channel opt-in gate (design D7): notification config AND both
    tables present in the environment."""
    return bool(
        TELEGRAM_COMMANDS_TABLE
        and TELEGRAM_ROUTING_TABLE
        and telegram_notifier.enabled()
    )


def _publish_telegram_routing(workspace: str, thread_id) -> None:
    """Reverse lookup item for the webhook Lambda (design D2, task 2.2):
    thread_id -> workspace, written by the same microVM that owns the topic.
    Raises on failure — the notifier logs and retries on the next boot."""
    if not _telegram_interaction_enabled() or not workspace:
        return
    key = str(thread_id) if isinstance(thread_id, int) else TELEGRAM_PLAIN_CHAT_THREAD_KEY
    _ddb().put_item(
        TableName=TELEGRAM_ROUTING_TABLE,
        Item={
            "threadId": {"S": key},
            "workspace": {"S": workspace},
            "updatedAt": {"N": str(int(time.time()))},
        },
    )
    logger.info("telegram routing published: thread %s -> workspace '%s'", key, workspace)


def _telegram_interaction_gate() -> bool:
    """Poll only while something can consume a command (design D3): an
    interactive session, a running task, the supervised opencode backend, or
    a permission request awaiting a decision. A warm quiescent runtime also
    polls: otherwise a Telegram follow-up could never be the event that starts
    its own ``task --continue``. The notifier thread does not keep the runtime
    busy, so normal idle shutdown remains unchanged."""
    if _INTERACTIVE_ACTIVE or _serve_is_alive():
        return True
    if _TASK_STATE.get("state") == "running":
        return True
    broker = _TELEGRAM.get("broker")
    try:
        if broker and broker.has_pending_requests():
            return True
    except Exception:  # noqa: BLE001
        pass
    return bool(_WORKSPACE_READY.is_set())


def _seeded_opencode_model() -> tuple | None:
    """(providerID, modelID) from the seeded opencode config, for the
    injection fallback when the serve API requires an explicit model."""
    try:
        config = json.loads(OPENCODE_CONFIG_FILE.read_text(encoding="utf-8"))
        model = config.get("model") or ""
        if isinstance(model, str) and "/" in model:
            provider, model_id = model.split("/", 1)
            if provider and model_id:
                return provider, model_id
    except Exception:  # noqa: BLE001
        pass
    return None


def _opencode_resume_model(session_id: str | None) -> str | None:
    """Use the runtime default when a session names an unavailable provider."""
    if not session_id or not OPENCODE_DB_LOCAL.exists():
        return None
    try:
        with sqlite3.connect(str(OPENCODE_DB_LOCAL)) as conn:
            row = conn.execute("SELECT model FROM session WHERE id = ?", (session_id,)).fetchone()
        session_model = json.loads(row[0]) if row and row[0] else {}
        provider = session_model.get("providerID") or ""
        if provider and provider not in _opencode_available_providers():
            config = json.loads(OPENCODE_CONFIG_FILE.read_text(encoding="utf-8"))
            default_model = config.get("model") or ""
            if isinstance(default_model, str) and "/" in default_model:
                return default_model
    except Exception:  # noqa: BLE001
        pass
    return None


# Staged-key → opencode provider IDs (mirrors the dispatcher mapping in
# harness-wrapper.sh: a staged key makes its provider selectable in opencode
# with no `opencode auth login`). The seeded opencode.json names only
# amazon-bedrock, so judging availability off the file alone would wrongly
# report every key-based provider as unavailable.
_STAGED_KEY_PROVIDERS = {
    "SCH_ANTHROPIC_API_KEY": ("anthropic",),
    "SCH_OPENCODE_API_KEY": ("opencode", "opencode-go"),
    "SCH_OPENROUTER_API_KEY": ("openrouter",),
    "SCH_KILO_API_KEY": ("kilo",),
    # SCH_BEDROCK_API_KEY re-auths amazon-bedrock itself (bearer token), and
    # amazon-bedrock additionally rides the execution role — available with or
    # without any key, hence added unconditionally below.
}


def _opencode_available_providers() -> set:
    """Provider IDs opencode can serve right now: config-file entries, plus
    staged-key providers, plus amazon-bedrock via the execution role."""
    try:
        config = json.loads(OPENCODE_CONFIG_FILE.read_text(encoding="utf-8"))
        configured = set((config.get("provider") or {}).keys())
    except Exception:  # noqa: BLE001
        configured = set()
    staged = _read_staged_provider_keys()
    for key, providers in _STAGED_KEY_PROVIDERS.items():
        if staged.get(key):
            configured.update(providers)
    configured.add("amazon-bedrock")
    return configured


def _opencode_session_model_variant(session_id: str | None) -> tuple[str | None, str | None]:
    """(model, variant) stored on an opencode session row, validated.

    The session row's ``model`` JSON (``{providerID, id, variant?}``) is the
    durable record of the model (and reasoning-effort variant, e.g. ``high``)
    the operator selected in the TUI. Returns ``(None, None)`` when there is
    no session, no row, no stored model, or anything fails to parse/validate
    — callers degrade to the previous behavior (harness default). A stored
    variant of ``"default"`` (opencode's sentinel for "no variant") is
    normalized to ``None``.
    """
    if not session_id or not OPENCODE_DB_LOCAL.exists():
        return None, None
    try:
        with sqlite3.connect(str(OPENCODE_DB_LOCAL)) as conn:
            row = conn.execute("SELECT model FROM session WHERE id = ?", (session_id,)).fetchone()
        session_model = json.loads(row[0]) if row and row[0] else {}
        if not isinstance(session_model, dict):
            return None, None
        provider = session_model.get("providerID") or session_model.get("provider") or ""
        model_id = (
            session_model.get("id") or session_model.get("modelID")
            or session_model.get("modelId") or ""
        )
        if not isinstance(provider, str) or not isinstance(model_id, str):
            return None, None
        model = "{}/{}".format(provider, model_id) if provider and model_id else ""
        if not model or not MODEL_ID_RE.fullmatch(model):
            return None, None
        variant = session_model.get("variant") or ""
        if not isinstance(variant, str) or variant in ("", "default"):
            return model, None
        if not MODEL_ID_RE.fullmatch(variant):
            return model, None
        return model, variant
    except Exception:  # noqa: BLE001
        return None, None


def _opencode_continue_model(session_id: str | None) -> tuple[str | None, str | None]:
    """(model, variant) a headless ``--continue`` should forward for opencode.

    Without an explicit ``--model`` the headless argv switches the agent to
    ``remote-auto`` while passing no model: opencode resolves such a
    model-less prompt to the agent's configured model, discarding the model
    the operator selected in the TUI (and clobbering the session row). The
    reasoning-effort variant (``--variant``, e.g. ``high``) is likewise lost,
    since it resolves from the new agent. Forwarding the resumed session's
    own stored model+variant preserves the TUI selection.

    Precedence: an unavailable-provider session still resolves to the runtime
    default model with no variant (existing ``_opencode_resume_model``
    override — the stored provider cannot serve); otherwise the session's own
    stored model+variant; ``(None, None)`` degrades to the harness default.
    """
    override = _opencode_resume_model(session_id)
    if override:
        return override, None
    return _opencode_session_model_variant(session_id)


def _opencode_api(method: str, path: str, payload: dict = None, timeout: float = 10.0):
    port = _SERVE_STATE.get("port") or OPENCODE_SERVE_PORT
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else None


def _opencode_inject_text(text: str) -> dict:
    """Inject ``text`` as a user message into the most recent session of the
    supervised ``opencode web`` backend (design D5 case 2 / task 4.1).

    Returns {ok, session_id?, title?, error?}. The message POST runs the
    whole turn server-side, so it is fired on a worker thread: we wait a
    short beat to catch immediate errors (bad session, schema rejection).
    The turn-end milestone is the only success response sent to Telegram."""
    if not _serve_is_alive():
        return {"ok": False, "error": "backend opencode non attivo"}
    try:
        sessions = _opencode_api("GET", "/session") or []
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"lista sessioni non disponibile ({exc})"}
    candidates = [
        s for s in sessions
        if isinstance(s, dict) and s.get("id") and not s.get("parentID")
    ]
    if not candidates:
        return {"ok": False, "error": "no session on the backend"}
    candidates.sort(
        key=lambda s: ((s.get("time") or {}).get("updated") or 0), reverse=True
    )
    target = candidates[0]
    session_id = target["id"]

    outcome: dict = {}

    def _post() -> None:
        # Preference order (verified against the opencode server API):
        # /prompt_async (204, does not wait for the turn) then /message
        # (synchronous, waits for the whole turn); each optionally retried
        # with the seeded model when the server rejects a model-less body.
        base = {"parts": [{"type": "text", "text": text}]}
        model = _seeded_opencode_model()
        attempts = [("/prompt_async", base, 15.0)]
        if model:
            attempts.append((
                "/prompt_async",
                dict(base, model={"providerID": model[0], "modelID": model[1]}),
                15.0,
            ))
        attempts.append(("/message", base, 1800.0))
        if model:
            attempts.append((
                "/message",
                dict(base, model={"providerID": model[0], "modelID": model[1]}),
                1800.0,
            ))
        last_error = None
        for suffix, payload, timeout in attempts:
            try:
                _opencode_api(
                    "POST", f"/session/{session_id}{suffix}", payload, timeout=timeout
                )
                outcome["done"] = True
                return
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}"
                if exc.code in (400, 404, 422):
                    continue  # older/newer API shape: try the next form
                break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                break
        outcome["error"] = last_error or "iniezione fallita"

    worker = threading.Thread(
        target=_post, name="sch-telegram-inject", daemon=True
    )
    worker.start()
    worker.join(2.5)
    if outcome.get("error"):
        return {"ok": False, "error": outcome["error"], "session_id": session_id}
    return {
        "ok": True,
        "session_id": session_id,
        "title": (target.get("title") or "").strip(),
    }


def _dispatch_telegram_text(text: str) -> None:
    """Free-text dispatch by workspace state, in D5 order:

    1. pending approval        -> courtesy (only the buttons decide);
    2. opencode backend active -> inject into the most recent session;
    3. claude/pi interactive   -> explicit limit + alternatives;
    4. quiescent               -> follow-up ``task --continue`` (existing
                                  path, unchanged concurrency rules).

    add-pi-harness (task 7.3): `pi` shares case 3 with claude. Pi does have an
    RPC mode that could accept injected input, but the interactive path here is
    the TUI, which has no such channel — so the limit is the same and so is the
    answer (spec: telegram-interaction, "Limite claude esplicito sul testo
    libero", extended to pi).
    """
    text = (text or "").strip()
    if not text:
        return
    workspace = _resolve_workspace_name()
    broker = _TELEGRAM.get("broker")
    harness = _resolve_harness()

    # 1. A pending approval is never decided by text (design D5).
    try:
        pending = bool(broker and broker.has_pending_requests())
    except Exception:  # noqa: BLE001
        pending = False
    if pending:
        _telegram_notify("interaction", {
            "text": (
                "An approval request is pending: use the Approve/Deny "
                "buttons on the request message. Free-form text is not "
                "interpreted as a decision."
            ),
        }, workspace)
        return

    # 2. Active opencode backend: inject (task 4.1). Sessions exist on the
    # backend -> the text becomes a user message in the most recent one.
    if harness == "opencode" and _serve_is_alive():
        result = _opencode_inject_text(text)
        if result.get("ok"):
            return
        if result.get("error") != "no session on the backend":
            # Actionable error (task 4.2): injection was the right path but
            # failed — explain and propose the follow-up.
            _telegram_notify("interaction", {
                "text": (
                    f"Injection failed: {result.get('error', '?')}. "
                    "Alternatively: wait for the session to finish and "
                    "resend the text as a follow-up, or reconnect with "
                    "'sch web' / 'sch attach'."
                ),
            }, workspace)
            return
        # Backend alive but empty: the workspace is effectively quiescent —
        # fall through to the follow-up path (D5 case 4).

    # 3. Claude/pi interactive TUI: accepted limit, explicit answer (task 2.3;
    # add-pi-harness task 7.3 for pi). The message names the harness so the
    # operator knows which limit they hit.
    if harness in ("claude", "pi") and _INTERACTIVE_ACTIVE:
        _telegram_notify("interaction", {
            "text": (
                f"An interactive {harness} session is in progress: text cannot "
                "be injected into the TUI (known limitation). Alternatives: "
                "reconnect with 'sch run', or wait for the turn/session to "
                "finish and resend the text as a follow-up."
            ),
        }, workspace)
        return

    # 3-bis. Interactive opencode without a working backend: injection is
    # impossible and a concurrent headless task would race the TUI (task 4.2).
    if harness == "opencode" and _INTERACTIVE_ACTIVE and not _serve_is_alive():
        _telegram_notify("interaction", {
            "text": (
                "Interactive opencode session without an active shared "
                "backend: text cannot be injected. Use 'sch web' or "
                "'sch attach' to start the backend, or wait for the session "
                "to finish and resend the text as a follow-up."
            ),
        }, workspace)
        return

    # 4. Quiescent: follow-up on the existing task path (task 2.4 — same
    # concurrency rules; a running task refuses with its id).
    response = _handle_task_action({"prompt": text, "continue": True})
    status = response.get("status")
    if status == "accepted":
        # The task-submitted lifecycle notification is the confirmation.
        return
    if status == "busy":
        task_ref = (response.get("task_id") or "")[:8]
        _telegram_notify("interaction", {
            "text": (
                f"A task is already running ({task_ref}): the follow-up "
                "was not started. Retry once the task has finished."
            ),
        }, workspace)
        return
    _telegram_notify("interaction", {
        "text": f"Follow-up not started: {response.get('message', 'unknown error')}",
    }, workspace)


def _start_telegram_notifier() -> None:
    """Create+start the notifier thread iff the feature is configured (spec:
    'Attivazione opt-in' — with the env absent this is a no-op and the
    runtime behaves byte-identically to before the change)."""
    if _TELEGRAM["notifier"] is not None:
        return
    notifier = telegram_notifier.build_from_env(
        load_topic_state=_load_telegram_topic_state,
        save_topic_state=_save_telegram_topic_state,
        default_workspace=_resolve_workspace_name,
        publish_routing=_publish_telegram_routing,
        presence_at_emission=command_shell_attached_at_emission,
        current_presence=command_shell_current_attached,
        on_sent=_on_telegram_sent,
    )
    if notifier is None:
        _remove_telegram_enabled_marker()
        return
    # add-telegram-interaction: attach the inbound manager only when the
    # command/routing tables are configured (design D7: without them the
    # permission hooks stay observational and the poll never exists).
    if _telegram_interaction_enabled():
        broker = telegram_interaction.ApprovalBroker()
        broker.ensure_dirs()
        broker.sweep()
        _TELEGRAM["broker"] = broker
        manager = telegram_interaction.InteractionManager(
            queue=telegram_interaction.CommandQueue(
                _ddb(), TELEGRAM_COMMANDS_TABLE, _resolve_workspace_name,
            ),
            broker=broker,
            client=notifier.client,
            chat_id=telegram_notifier.config()[1],
            notify=notifier.notify,
            gate=_telegram_interaction_gate,
            dispatch_text=_dispatch_telegram_text,
        )
        notifier.set_interaction(manager)
        _TELEGRAM["interaction"] = manager
        logger.info("telegram inbound interaction enabled (commands table configured)")
    _COMMAND_SHELL_PRESENCE.set_on_attached(notifier.on_presence_attached)
    if command_shell_current_attached():
        notifier.on_presence_attached()
    notifier.start()
    _TELEGRAM["notifier"] = notifier
    logger.info("telegram notifier started (chat configured)")


def _flush_telegram_notifier() -> None:
    """Best-effort flush of pending terminal notifications at ordered
    shutdown (task 2.3 — registered next to the final DB checkpoint)."""
    try:
        notifier = _TELEGRAM["notifier"]
        if notifier is not None:
            notifier.flush(timeout=5.0)
    finally:
        _remove_telegram_enabled_marker()


def _remove_telegram_enabled_marker() -> None:
    try:
        TELEGRAM_ENABLED_MARKER.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("cannot remove Telegram enabled marker: %s", exc)


def _publish_telegram_enabled_marker() -> None:
    """Atomically expose notifier capability before a harness can start."""
    if not telegram_notifier.enabled():
        _remove_telegram_enabled_marker()
        return
    payload = {
        "version": 1,
        "interaction_enabled": _telegram_interaction_enabled(),
    }
    path = TELEGRAM_ENABLED_MARKER
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("cannot publish Telegram enabled marker: %s", exc)
    finally:
        temp.unlink(missing_ok=True)


def _spool_telegram_event(etype: str, payload: dict, workspace: str | None) -> None:
    """Write one lifecycle event into the notifier's spool dir (task 4.4).

    Same envelope and same tmp-then-rename discipline as the harness hooks,
    so ``Notifier._poll_spool`` consumes it with the priority the event type
    already has. Never raises: an observability path must not break a submit.
    """
    event = {
        "type": etype,
        "workspace": workspace,
        "payload": payload,
        "ts": time.time(),
        "source": "shim",
    }
    try:
        spool_dir = Path(telegram_notifier.SPOOL_DIR)
        spool_dir.mkdir(parents=True, exist_ok=True)
        final = spool_dir / f"{time.time_ns()}-{os.getpid()}-shim.json"
        tmp = Path(f"{final}.tmp")
        tmp.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, final)
    except Exception as exc:  # noqa: BLE001 — never propagate to primary flows
        logger.warning("telegram spool fallback failed for %s: %s", etype, exc)


def _on_telegram_sent(event: dict) -> None:
    """Notifier success hook (TASK-28): a delivered ``task-terminal`` marks
    its S3 record so the watchdog knows it has nothing to re-send. Every
    other event type is ignored — milestones stay best-effort by design."""
    if event.get("type") != "task-terminal":
        return
    task_id = (event.get("payload") or {}).get("task_id")
    workspace = event.get("workspace") or _resolve_workspace_name()
    if not task_id or not workspace:
        return
    _mark_task_terminal_notified(workspace, task_id)


def _telegram_notify(etype: str, payload: dict, workspace: str | None = None) -> None:
    """Non-blocking emit from the shim's threads; no-op when unconfigured.

    Cold boot (add-task-liveness-safety, spec: 'No lifecycle event lost at
    cold boot', design D6): the notifier singleton is only
    created at the END of _bootstrap, while _handle_task_action answers an
    invocation as soon as the workspace identity is resolved (it never waits
    for _WORKSPACE_READY — only the task worker does). A `task-submitted`
    emitted in that window used to be dropped on the floor, on a brand-new
    microVM, which is the exact shape of the incident. With the channel
    configured but the notifier not registered yet, the event goes to the
    spool dir instead and is delivered at the notifier's first poll. The
    opt-in contract is unchanged: the fallback is gated on enabled(), not on
    the singleton, so without the Telegram env this stays a silent no-op
    (no directory, no file).
    """
    notifier = _TELEGRAM["notifier"]
    if notifier is not None:
        notifier.notify(etype, payload, workspace)
        return
    if telegram_notifier.enabled():
        _spool_telegram_event(etype, payload, workspace)


def _parse_utc(value: str | None) -> float | None:
    """Epoch seconds for a _utcnow()-formatted string, or None."""
    if not value:
        return None
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None


# --- L2 checkpoint engine: checkpoint pass + restore (design D3/D4) -------------

def _do_checkpoint(force: bool) -> dict:
    """One checkpoint pass (periodic or synchronous/forced): db backup to
    the mount (always, unchanged Phase-0 mechanic), then per-artifact S3
    upload guided by change detection unless `force` (used by the `checkpoint`
    action / `sch stop`, which also forces a manifest write even with no
    detected changes so the final timestamp is certain). Serialized against
    the periodic loop and other concurrent forced calls via _CHECKPOINT_LOCK."""
    with _CHECKPOINT_LOCK:
        result: dict = {"db_backup": _backup_db_once()}
        workspace = _resolve_workspace_name()
        if not workspace:
            result["status"] = "skipped-no-workspace"
            CHECKPOINT_STATE["last_result"] = result["status"]
            CHECKPOINT_STATE["last_attempt_utc"] = _utcnow()
            return result
        if not CHECKPOINT_BUCKET:
            result["status"] = "skipped-no-bucket"
            CHECKPOINT_STATE["last_result"] = result["status"]
            CHECKPOINT_STATE["last_attempt_utc"] = _utcnow()
            return result

        # Multi-harness (design D2/D9, tasks 6.1/6.2): when harness=claude,
        # mirror the local-disk claude state to the mount replica first, then
        # include it in the per-workspace tarball. When harness=opencode, the
        # claude artifact is skipped entirely (no upload for OpenCode-only
        # workspaces — spec: workspace-checkpointing, "Skip del claude state
        # su harness=opencode").
        # add-pi-harness (design D2, task 4.4): harness=pi follows the same
        # pattern with its own replica and artifact. Each per-harness artifact
        # exists ONLY for its own harness (spec: workspace-checkpointing, "Skip
        # dello stato pi su altri harness" and its claude counterpart).
        harness = _resolve_harness()
        if harness == "claude":
            mirror_result = _mirror_claude_state()
            result["claude_mirror"] = mirror_result
        else:
            result["claude_mirror"] = f"skipped-{harness}"
        if harness == "pi":
            result["pi_mirror"] = _mirror_pi_state()
        else:
            result["pi_mirror"] = f"skipped-{harness}"

        CHECKPOINT_STATE["last_attempt_utc"] = _utcnow()
        prev_fp = CHECKPOINT_STATE.get("last_fingerprints", {})
        prev_sizes = dict(CHECKPOINT_STATE.get("last_sizes", {}))
        new_fp = {
            "repo": _fingerprint_repo(),
            "state": _fingerprint_state(),
            "db": _fingerprint_db(),
        }
        if harness == "claude":
            new_fp["claude"] = _fingerprint_claude_state()
        if harness == "pi":
            new_fp["pi"] = _fingerprint_pi_state()
        uploaded: list = []
        errors: list = []
        tmp_dir = CHECKPOINT_TMP_DIR / workspace
        s3_backend = _resolve_storage_backend() == "s3"
        try:
            previous_manifest = _download_manifest(workspace)
        except ManifestReadError as exc:
            result.update({
                "status": "error", "errors": [str(exc)],
                "manifest_written": False,
            })
            CHECKPOINT_STATE["last_result"] = "error"
            return result
        previous_etag = (previous_manifest or {}).get("_etag")
        artifacts = dict((previous_manifest or {}).get("artifacts") or {})
        generation = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"

        def upload_artifact(path: Path, name: str) -> bool:
            if not s3_backend:
                return _upload_file(path, workspace, name)
            key = f"checkpoint-generations/{workspace}/{generation}/{name}"
            if not _upload_file_key(path, key):
                return False
            artifacts[name] = key
            return True

        try:
            if force or new_fp["repo"] != prev_fp.get("repo"):
                tar_path = tmp_dir / "repo.tar.gz"
                if _create_archive(REPO_DIR, tar_path):
                    prev_sizes["repo"] = tar_path.stat().st_size
                    if upload_artifact(tar_path, "repo.tar.gz"):
                        uploaded.append("repo")
                    else:
                        errors.append("repo-upload")
                else:
                    errors.append("repo-archive")

            if force or new_fp["state"] != prev_fp.get("state"):
                tar_path = tmp_dir / "state.tar.gz"
                if _create_archive(
                    STATE_DIR, tar_path,
                    excludes=(DB_BACKUP_PATH, _db_backup_tmp_path()),
                ):
                    prev_sizes["state"] = tar_path.stat().st_size
                    if upload_artifact(tar_path, "state.tar.gz"):
                        uploaded.append("state")
                    else:
                        errors.append("state-upload")
                else:
                    errors.append("state-archive")

            if (force or new_fp["db"] != prev_fp.get("db")) and DB_BACKUP_PATH.exists():
                prev_sizes["db"] = DB_BACKUP_PATH.stat().st_size
                if upload_artifact(DB_BACKUP_PATH, "opencode.db.backup"):
                    uploaded.append("db")
                else:
                    errors.append("db-upload")

            # Claude state artifact (task 6.1): only when harness=claude and
            # the replica is non-empty. Change detection via mtime fingerprint
            # (no upload if unchanged); read-after-write verify mirrors the
            # existing opencode pattern (task 6.2).
            if harness == "claude" and CLAUDE_STATE_REPLICA.exists() and (
                force or new_fp.get("claude") != prev_fp.get("claude")
            ):
                tar_path = tmp_dir / "claude.tar.gz"
                if _create_archive(CLAUDE_STATE_REPLICA, tar_path):
                    prev_sizes["claude"] = tar_path.stat().st_size
                    if upload_artifact(tar_path, "claude.tar.gz"):
                        uploaded.append("claude")
                    else:
                        errors.append("claude-upload")
                else:
                    errors.append("claude-archive")
            elif harness == "claude" and "claude.tar.gz" not in artifacts:
                errors.append("claude-state-missing")

            # Pi state artifact (add-pi-harness, task 4.4): exact mirror of the
            # claude branch above — only for harness=pi, only when the replica
            # exists, change-detected by mtime fingerprint. The artifact is
            # absent from opencode and claude manifests, and claude.tar.gz is
            # absent from pi ones (spec: workspace-checkpointing).
            if harness == "pi" and PI_STATE_REPLICA.exists() and (
                force or new_fp.get("pi") != prev_fp.get("pi")
            ):
                tar_path = tmp_dir / "pi.tar.gz"
                if _create_archive(PI_STATE_REPLICA, tar_path):
                    prev_sizes["pi"] = tar_path.stat().st_size
                    if upload_artifact(tar_path, "pi.tar.gz"):
                        uploaded.append("pi")
                    else:
                        errors.append("pi-upload")
                else:
                    errors.append("pi-archive")
            elif harness == "pi" and "pi.tar.gz" not in artifacts:
                errors.append("pi-state-missing")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        manifest_written = False
        if (uploaded or force) and (not s3_backend or not errors):
            manifest = {
                "ts": _utcnow(),
                "session_id": _SESSION_ID["value"],
                "image_version": os.environ.get("SCH_IMAGE_VERSION", "unknown"),
                "harness": harness,  # task 6.4: drives init-workspace.sh seeding on restore
                "storage": _resolve_storage_backend(),
                "session_epoch": _SESSION_EPOCH["value"],
                "fingerprint": new_fp,
                "sizes": prev_sizes,
            }
            if s3_backend:
                manifest["generation"] = generation
                manifest["artifacts"] = artifacts
                for artifact_key in artifacts.values():
                    try:
                        _s3().put_object_tagging(
                            Bucket=CHECKPOINT_BUCKET, Key=artifact_key,
                            Tagging={"TagSet": [{"Key": "active", "Value": "true"}]},
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"artifact-tag:{artifact_key}:{exc}")
            if errors:
                manifest_written = False
            else:
                manifest_written = _upload_manifest(workspace, manifest, previous_etag)
            if not manifest_written:
                errors.append("manifest-upload")

        if manifest_written:
            CHECKPOINT_STATE["last_fingerprints"] = new_fp
            CHECKPOINT_STATE["last_sizes"] = prev_sizes

        # Surface a failed DB backup in the checkpoint status (WAL regression
        # fix: this used to be swallowed, reporting "ok" for days while the
        # session DB was silently frozen at day 0). Appended only AFTER the
        # manifest decision on purpose: a db-backup failure must degrade the
        # status to "partial", not veto the repo/state commit.
        if str(result.get("db_backup", "")).startswith("error"):
            errors.append(f"db-backup:{result['db_backup']}")

        status = "ok" if not errors else ("partial" if (uploaded or manifest_written) else "error")
        result.update({
            "status": status,
            "workspace": workspace,
            "harness": harness,
            "storage": _resolve_storage_backend(),
            "uploaded": uploaded,
            "errors": errors,
            "manifest_written": manifest_written,
        })
        CHECKPOINT_STATE["last_result"] = status
        if status in ("ok", "partial"):
            CHECKPOINT_STATE["last_success_utc"] = _utcnow()
        return result


def _checkpoint_loop() -> None:
    """Extends the Phase-0 DB-only backup loop into the full L2 checkpoint
    loop (design D3). With SCH_CHECKPOINT_INTERVAL=0, degrades to the
    Phase-0 behaviour (db -> mount only, no S3), without any redeploy."""
    while True:
        time.sleep(CHECKPOINT_INTERVAL if CHECKPOINT_INTERVAL else DB_BACKUP_INTERVAL)
        if CHECKPOINT_INTERVAL == 0:
            _backup_db_once()
            continue
        try:
            _do_checkpoint(force=False)
        except Exception as exc:  # noqa: BLE001 — loop must never die
            logger.error("checkpoint loop iteration failed: %s", exc, exc_info=True)


def _verify_l2_restore() -> bool:
    """Read-after-write sanity check for _restore_l2 (design D4, mirrors
    _verify_workspace_seeded for the seed path).

    Multi-harness (task 6.3): for harness=claude, the claude replica on the
    mount must also be present (un-mirror target checked separately by
    _restore_claude_state)."""
    try:
        if not (REPO_DIR.is_dir() and (STATE_DIR / "config").is_dir()):
            return False
        if _resolve_storage_backend() == "session" and not DB_BACKUP_PATH.exists():
            return False
        if _resolve_harness() == "claude" and not CLAUDE_STATE_REPLICA.exists():
            return False
        if _resolve_harness() == "pi" and not PI_STATE_REPLICA.exists():
            return False
        return True
    except OSError:
        return False


def _validate_manifest(manifest: dict, storage_backend: str) -> dict:
    if not isinstance(manifest, dict):
        raise ManifestReadError("manifest is not a JSON object")
    if manifest.get("published") is False:
        return manifest
    harness = manifest.get("harness") or "opencode"
    manifest["harness"] = harness
    if harness not in SUPPORTED_HARNESSES:
        raise ManifestReadError("manifest has invalid harness")
    manifest_storage = manifest.get("storage")
    if manifest_storage and manifest_storage not in SUPPORTED_STORAGE_BACKENDS:
        raise ManifestReadError("manifest has invalid storage backend")
    manifest_epoch = manifest.get("session_epoch", 0)
    if not isinstance(manifest_epoch, int) or manifest_epoch < 0:
        raise ManifestReadError("manifest has invalid session epoch")
    if storage_backend == "s3":
        artifacts = manifest.get("artifacts")
        required = {"repo.tar.gz", "state.tar.gz"}
        if harness == "claude":
            required.add("claude.tar.gz")
        if harness == "pi":
            required.add("pi.tar.gz")
        if not isinstance(artifacts, dict) or any(
            not isinstance(artifacts.get(name), str) or not artifacts[name]
            for name in required
        ):
            raise ManifestReadError("manifest is missing required artifact references")
    return manifest


def _download_manifest(workspace: str) -> dict | None:
    """Return a validated manifest, None only for an actual missing key."""
    try:
        obj = _s3().get_object(Bucket=CHECKPOINT_BUCKET, Key=_s3_key(workspace, "manifest.json"))
        try:
            manifest = json.loads(obj["Body"].read().decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManifestReadError("manifest contains invalid JSON") from exc
        manifest = _validate_manifest(manifest, _resolve_storage_backend())
        manifest["_etag"] = obj.get("ETag")
        return manifest
    except Exception as exc:  # noqa: BLE001
        err_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
        if err_code in ("NoSuchKey", "404"):
            return None
        if isinstance(exc, ManifestReadError):
            raise
        raise ManifestReadError(f"manifest download failed: {exc}") from exc


def _restore_l2(workspace: str) -> dict:
    """Download + extract the S3 checkpoint into the active workspace root.
    Caller is responsible for the anti-clobber guard (_mount_storage_empty).

    Multi-harness (tasks 6.3/6.4/6.5): reads the harness from the manifest
    (default opencode for legacy manifests without the field — upgrade_
    reconcile), and when harness=claude downloads+extracts the claude state
    replica then un-mirrors it to local disk BEFORE the readiness marker is
    written (so the `claude` binary finds the restored transcripts on first
    launch). The OpenCode artifacts path stays byte-identical to Phase 1.

    add-pi-harness (task 4.5): harness=pi takes the identical path with
    pi.tar.gz and _restore_pi_state()."""
    try:
        manifest = _download_manifest(workspace)
    except ManifestReadError as exc:
        return {"attempted": True, "result": "error", "errors": [str(exc)]}
    if manifest is None:
        return {"attempted": True, "result": "no-manifest"}
    if manifest.get("published") is False:
        return {"attempted": True, "result": "no-manifest"}

    # Task 6.4/6.5: harness drives seeding branch + which replica to un-mirror.
    # A legacy manifest without `harness` reconciles to opencode (the image's
    # pre-multi-harness behavior); OpenCode artifacts path stays byte-identical.
    manifest_harness = manifest.get("harness")
    if manifest_harness and manifest_harness in SUPPORTED_HARNESSES:
        _set_harness(manifest_harness)
    else:
        _set_harness("opencode")  # legacy reconcile

    tmp_dir = CHECKPOINT_TMP_DIR / workspace / "restore"
    staging_root = (
        WORKSPACE_ROOT.parent / f".{WORKSPACE_ROOT.name}.restore-{uuid.uuid4().hex}"
        if _resolve_storage_backend() == "s3"
        else WORKSPACE_ROOT / f".sch-restore-{uuid.uuid4().hex}"
    )
    errors: list = []
    artifacts = manifest.get("artifacts") if _resolve_storage_backend() == "s3" else None

    def download_artifact(name: str, dest: Path, required: bool = True) -> bool:
        if artifacts is None:
            return _download_file(workspace, name, dest)
        key = artifacts.get(name)
        if not key:
            return not required
        return _download_key(key, dest)

    manifest_storage = manifest.get("storage")
    if manifest_storage and manifest_storage != _resolve_storage_backend():
        return {
            "attempted": True,
            "result": "error",
            "errors": [
                f"storage-mismatch:{manifest_storage}!={_resolve_storage_backend()}"
            ],
        }
    extract_root = staging_root
    restore_state = extract_root / "state"
    restore_db = restore_state / "data" / "opencode" / "opencode.db.backup"
    try:
        shutil.rmtree(staging_root, ignore_errors=True)
        staging_root.mkdir(parents=True)
        repo_tar = tmp_dir / "repo.tar.gz"
        if download_artifact("repo.tar.gz", repo_tar):
            if not _extract_archive(repo_tar, extract_root):
                errors.append("repo-extract")
        else:
            errors.append("repo-download")

        state_tar = tmp_dir / "state.tar.gz"
        if download_artifact("state.tar.gz", state_tar):
            if not _extract_archive(state_tar, extract_root):
                errors.append("state-extract")
        else:
            errors.append("state-download")

        if not download_artifact("opencode.db.backup", restore_db, required=False):
            errors.append("db-download")

        # Claude state artifact: only when harness=claude (task 6.3). The
        # The tarball mirrors the active state/claude replica; extract there,
        # then un-mirror to local disk ($HOME/.claude) so the
        # `claude` binary finds the restored JSONL transcripts.
        if _resolve_harness() == "claude":
            claude_tar = tmp_dir / "claude.tar.gz"
            if download_artifact("claude.tar.gz", claude_tar):
                if not _extract_archive(claude_tar, restore_state):
                    errors.append("claude-extract")
            else:
                errors.append("claude-download")

        # Pi state artifact (add-pi-harness, task 4.5): same shape as claude —
        # the tarball mirrors the active state/pi replica, so it is extracted
        # there and un-mirrored to local disk below, BEFORE the pi readiness
        # marker is written (spec: runtime-image, "Stato Pi su disco locale e
        # replica L2 sul mount").
        if _resolve_harness() == "pi":
            pi_tar = tmp_dir / "pi.tar.gz"
            if download_artifact("pi.tar.gz", pi_tar):
                if not _extract_archive(pi_tar, restore_state):
                    errors.append("pi-extract")
            else:
                errors.append("pi-download")

        if _resolve_storage_backend() == "s3" and not errors:
            if not (staging_root / "repo").is_dir() or not (restore_state / "config").is_dir():
                errors.append("staging-verify")
            else:
                try:
                    WORKSPACE_ROOT.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    errors.append("active-root-not-empty")
                if not errors:
                    os.replace(staging_root, WORKSPACE_ROOT)
        elif not errors:
            if not (staging_root / "repo").is_dir() or not restore_state.is_dir():
                errors.append("staging-verify")
            else:
                for destination in (REPO_DIR, STATE_DIR):
                    try:
                        destination.rmdir()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        errors.append("active-root-not-empty")
                        break
                if not errors:
                    SESSION_RESTORE_MARKER.write_text(json.dumps({
                        "workspace": workspace,
                        "staging": str(staging_root),
                        "started_utc": _utcnow(),
                    }))
                    os.replace(staging_root / "repo", REPO_DIR)
                    os.replace(staging_root / "state", STATE_DIR)
                    SESSION_RESTORE_MARKER.unlink()
        if _resolve_harness() == "claude" and not errors:
            claude_restore = _restore_claude_state()
            if claude_restore not in ("restored", "l1-present"):
                errors.append(f"claude-state:{claude_restore}")
        if _resolve_harness() == "pi" and not errors:
            pi_restore = _restore_pi_state()
            if pi_restore not in ("restored", "l1-present"):
                errors.append(f"pi-state:{pi_restore}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if staging_root.exists():
            shutil.rmtree(staging_root, ignore_errors=True)

    if errors:
        logger.error("L2 restore for workspace '%s' incomplete: %s", workspace, errors)
        return {"attempted": True, "result": "error", "errors": errors, "harness": _resolve_harness()}
    logger.info(
        "L2 restore for workspace '%s' downloaded+extracted (harness=%s)",
        workspace, _resolve_harness(),
    )
    return {"attempted": True, "result": "downloaded", "harness": _resolve_harness()}


def _bootstrap() -> None:
    """Full boot sequence, run in a background thread so /ping is served
    immediately while the (asynchronous) session-storage restore settles."""
    _remove_telegram_enabled_marker()
    try:
        BOOT_STATE["phase"] = "waiting-storage-backend"
        backend_deadline = time.monotonic() + MOUNT_SETTLE_TIMEOUT
        while _STORAGE_BACKEND["value"] is None and time.monotonic() < backend_deadline:
            time.sleep(0.1)
        if _STORAGE_BACKEND["value"] is None:
            _set_storage_backend("session")
            logger.info("no storage backend hint received; using legacy session backend")
        BOOT_STATE["storage"] = _resolve_storage_backend()
        BOOT_STATE["workspace_root"] = str(WORKSPACE_ROOT)
        BOOT_STATE["phase"] = "waiting-mount"
        BOOT_STATE["mount"] = _wait_for_mount_restore()
        logger.info("mount state: %s", BOOT_STATE["mount"])
        _recover_incomplete_session_restore()

        # Workspace identity (design D2): resolve now (payload hint from the
        # first invocation, or session-id fallback, both already applied by
        # invoke() by the time _wait_for_mount_restore() returns — the same
        # first invocation carries both the storage hint and the workspace
        # name). Persist to the marker so a shim restart within the same
        # microVM (or a future resume) can recover it without a fresh hint.
        ws_name = _resolve_workspace_name()
        BOOT_STATE["workspace"] = ws_name
        # Resolve harness EARLY (task 5.6 / 6.4): a fresh microVM reads it
        # back from the mount marker (written by the previous session from
        # the `sch` payload) so init-workspace.sh, L2 restore, and orphan
        # reconciliation all use the persisted harness without re-specification.
        # _resolve_harness falls back to DEFAULT_HARNESS (opencode) when no
        # payload and no marker — raw shells without `sch` keep Phase-0 behavior.
        ws_harness = _resolve_harness()
        BOOT_STATE["harness"] = ws_harness
        if ws_name:
            if CHECKPOINT_BUCKET:
                _assert_writer_claim(ws_name)
            # Orphan reconciliation (design D2 / task 6.7): a persisted
            # 'running' status can only mean the previous microVM died
            # mid-task. Never resurrect it. The orphan's harness field is
            # preserved in the interrupted rewrite for observability.
            _reconcile_orphan_task_status(ws_name)
        else:
            logger.info(
                "workspace identity unknown at boot (no payload hint, no marker, "
                "no session-id derivation) — L2 restore/checkpoint disabled this boot "
                "(spec: workspace-checkpointing, 'Session without a workspace name')"
            )

        # L2 restore (design D4): only ever attempted when the mount is
        # verified EMPTY (anti-clobber: L1 data always wins) and the
        # workspace identity is known and a checkpoint bucket is configured.
        restore_l2: dict = {"attempted": False, "result": "not-applicable"}
        if ws_name and CHECKPOINT_BUCKET and _mount_storage_empty():
            BOOT_STATE["phase"] = "restoring-l2"
            for attempt in range(1, INIT_VERIFY_RETRIES + 1):
                restore_l2 = _restore_l2(ws_name)
                if restore_l2.get("result") == "error":
                    raise RuntimeError(
                        "workspace restore failed: {}".format(
                            ", ".join(restore_l2.get("errors") or ["unknown error"])
                        )
                    )
                if restore_l2.get("result") != "downloaded":
                    break
                if _verify_l2_restore():
                    restore_l2["result"] = "restored"
                    restore_l2["verified"] = True
                    if attempt > 1:
                        logger.info("L2 restore verified on retry %d/%d", attempt, INIT_VERIFY_RETRIES)
                    break
                logger.warning(
                    "L2 restore verification failed (attempt %d/%d) for '%s'; retrying",
                    attempt, INIT_VERIFY_RETRIES, ws_name,
                )
                time.sleep(INIT_VERIFY_RETRY_WAIT)
            else:
                restore_l2["result"] = "restore-unverified"
                restore_l2["verified"] = False
                logger.error(
                    "L2 restore for '%s' never verified after %d attempts",
                    ws_name, INIT_VERIFY_RETRIES,
                )
                raise RuntimeError("workspace restore could not be verified")
        BOOT_STATE["restore_l2"] = restore_l2

        BOOT_STATE["phase"] = "restoring-db"
        # Whether or not L2 populated the mount, the local-disk DB restore
        # step is the same (it is a no-op if no backup file is present).
        restore_result = _restore_db()
        BOOT_STATE["restore_result"] = restore_result
        # WAL regression fix: the restore outcome used to be recorded and
        # ignored — a failed restore booted "ready" with an empty session DB
        # (silent session loss). An actual restore error now fails closed
        # (boot retry can genuinely fix a transient copy failure); a backup
        # promised by the marker but missing from the mount is unrecoverable
        # data (failing closed could never restore it) so it boots, but is
        # loudly surfaced instead of pretending all is well.
        if restore_result.startswith("error"):
            raise RuntimeError(f"opencode.db restore failed: {restore_result}")
        if restore_result == "backup-promised-but-missing":
            BOOT_STATE["db_restore_warning"] = restore_result
            logger.error(
                "marker promised an opencode.db backup but none was found on the "
                "mount — previous sessions are NOT recoverable this boot; "
                "continuing with an empty session DB"
            )

        BOOT_STATE["phase"] = "init-workspace"
        BOOT_STATE["init_result"] = _run_init_workspace()

        # Verify-after-write (belt-and-braces against the mount-attach race,
        # see FRESH_SETTLE_WAIT above): init-workspace.sh reporting exit 0
        # only means the writes succeeded against WHATEVER was mounted at
        # /mnt/workspace at that instant — if the real session-storage
        # volume attaches slightly later, those writes are shadowed and the
        # directory looks empty again. Confirm the seed (or, transitively,
        # the L2 restore above) actually stuck; if not, the mount likely
        # just settled — re-run the (idempotent) init script rather than
        # declaring the workspace ready on a lie.
        for attempt in range(1, INIT_VERIFY_RETRIES + 1):
            if _verify_workspace_seeded():
                if attempt > 1:
                    logger.info("workspace seed verified on retry %d/%d",
                                attempt, INIT_VERIFY_RETRIES)
                break
            logger.warning(
                "workspace seed verification failed (attempt %d/%d) — "
                "repo/config missing right after init-workspace.sh reported "
                "success; mount likely settled late, retrying",
                attempt, INIT_VERIFY_RETRIES,
            )
            time.sleep(INIT_VERIFY_RETRY_WAIT)
            BOOT_STATE["init_result"] = _run_init_workspace()
        else:
            logger.error(
                "workspace seed verification never succeeded after %d attempts; "
                "failing closed (see BOOT_STATE for diagnostics)",
                INIT_VERIFY_RETRIES,
            )
            raise RuntimeError("workspace seed could not be verified")
        BOOT_STATE["seed_verified"] = _verify_workspace_seeded()
        if _resolve_storage_backend() == "session" and SESSION_RESTORE_MARKER.exists():
            raise RuntimeError("late incomplete session restore promotion detected")

        # First initialization: write the marker used to detect resumes
        # (workspace identity + harness, if known, preserved by
        # _write_marker's read-modify-write semantics).
        _write_marker(
            initialized=True, workspace=ws_name, harness=_resolve_harness(),
            storage=_resolve_storage_backend(),
        )

        # Publish notifier capability before unblocking any harness. The marker
        # is local, atomic, and contains no Telegram credentials.
        if not _TELEGRAM["marker_cleanup_registered"]:
            atexit.register(_remove_telegram_enabled_marker)
            _TELEGRAM["marker_cleanup_registered"] = True
        _publish_telegram_enabled_marker()

        # Workspace usable: unblock the wrapper for the RESOLVED harness only
        # (design D3, tasks 3.4/3.5). The harness-wrapper.sh gates on the
        # per-harness marker; writing only the resolved harness's marker
        # means a stray invocation of the OTHER harness's binary waits and
        # degrades gracefully (warning + launch against seeded config) rather
        # than silently proceeding — matches D8's mutual-exclusivity intent
        # at the TUI level. Reached only after the seed (fresh init OR L2
        # restore, both covered by the verify loop above) is confirmed.
        ready_marker = _harness_ready_marker(_resolve_harness())
        if _resolve_storage_backend() == "session" and SESSION_RESTORE_MARKER.exists():
            raise RuntimeError("session restore promotion appeared before readiness")
        ready_marker.parent.mkdir(parents=True, exist_ok=True)
        ready_marker.touch(exist_ok=True)
        _WORKSPACE_READY.set()

        BOOT_STATE["phase"] = "ready"
        if _resolve_storage_backend() == "session":
            threading.Thread(
                target=_session_restore_guard_loop,
                name="sch-session-restore-guard",
                daemon=True,
            ).start()
        logger.info(
            "bootstrap complete (mount=%s, harness=%s, restore=%s, restore_l2=%s)",
            BOOT_STATE["mount"], BOOT_STATE.get("harness"),
            BOOT_STATE["restore_result"], BOOT_STATE["restore_l2"],
        )
    except Exception as exc:  # noqa: BLE001 — shim must stay alive for /ping
        BOOT_STATE["phase"] = "error"
        BOOT_STATE["error"] = str(exc)
        logger.error("bootstrap failed: %s", exc, exc_info=True)
    finally:
        if BOOT_STATE.get("phase") == "ready":
            t = threading.Thread(target=_checkpoint_loop, name="sch-checkpoint", daemon=True)
            t.start()
            atexit.register(_backup_db_once)  # best-effort final DB checkpoint only
            # Telegram notifications (add-telegram-notifications): opt-in —
            # _start_telegram_notifier is a no-op without the env config, and
            # the atexit flush drains only pending TERMINAL notifications
            # (task 2.3, same ordered-shutdown spot as the DB checkpoint).
            _start_telegram_notifier()
            atexit.register(_flush_telegram_notifier)
            # Interactive busy keep-alive (add-interactive-busy-keepalive):
            # independent of the Telegram opt-in — the watcher only touches
            # local marker files and the advisory /ping busy flag; the
            # busy-cap/busy-stale notifies inside degrade to no-ops when the
            # notifier is unconfigured.
            threading.Thread(
                target=_busy_keepalive_loop,
                name="sch-busy-keepalive",
                daemon=True,
            ).start()


def _verify_workspace_seeded() -> bool:
    """Read-after-write sanity check for init-workspace.sh's own output.

    Confirms the repo worktree and the seeded harness config are actually
    visible right now, not just that the script exited 0 (which can be true
    against a not-yet-final mount — see FRESH_SETTLE_WAIT/INIT_VERIFY_*).

    Multi-harness (task 3.4): the canary file is harness-specific —
    ``opencode.json`` for harness=opencode (byte-identical to Phase 1, task
    3.5) and the project-root ``.mcp.json`` (``REPO_DIR/.mcp.json`` —
    sch-context7-builtin follow-up fix, see ``CLAUDE_MCP_FILE``) for
    harness=claude. init-workspace.sh seeds only the branch matching
    SCH_HARNESS, so requiring both would loop forever on a harness=claude
    workspace.

    add-pi-harness (task 2.3): for harness=pi the canary is ``settings.json``
    (without it Pi would start on its own default provider `google`, which has
    no credentials in this microVM) plus the brief, the remote-auto role file
    the headless argv references and the SCH extension.
    """
    try:
        if not REPO_DIR.exists():
            return False
        if _resolve_harness() == "claude":
            return all(
                path.is_file()
                for path in (
                    CLAUDE_MCP_FILE,
                    CLAUDE_CONFIG_DIR_LOCAL / "CLAUDE.md",
                    CLAUDE_CONFIG_DIR_LOCAL / "agents" / "remote-interactive.md",
                    CLAUDE_CONFIG_DIR_LOCAL / "agents" / "remote-auto.md",
                )
            )
        if _resolve_harness() == "pi":
            return all(
                path.is_file()
                for path in (
                    PI_SETTINGS_FILE,
                    PI_CONFIG_DIR_LOCAL / "AGENTS.md",
                    PI_ROLE_REMOTE_AUTO,
                    PI_CONFIG_DIR_LOCAL / "roles" / "remote-interactive.md",
                    PI_CONFIG_DIR_LOCAL / "extensions" / "sch-pi.ts",
                )
            )
        return OPENCODE_CONFIG_FILE.is_file()
    except OSError:
        return False


def _run_init_workspace() -> dict:
    """Run the idempotent workspace bootstrap; never crash the shim."""
    if not INIT_SCRIPT.exists():
        logger.warning("init script missing: %s", INIT_SCRIPT)
        return {"status": "skipped", "reason": f"{INIT_SCRIPT} not found"}
    try:
        # add-user-provider-keys (design D6): the claude reconciliation
        # (settings.json env.CLAUDE_CODE_USE_BEDROCK + the pre-approved key
        # suffix) is recomputed from the keys ACTUALLY present in this session,
        # so the seeding script must see the staged set — including its absence,
        # which is what restores Bedrock on a workspace whose checkpointed
        # settings.json still says "use the API".
        proc = subprocess.run(
            ["/bin/bash", str(INIT_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
            env=_child_env_with_provider_keys(),
        )
        for line in (proc.stdout or "").splitlines():
            logger.info("init-workspace: %s", line)
        for line in (proc.stderr or "").splitlines():
            logger.warning("init-workspace(err): %s", line)
        return {"status": "ok" if proc.returncode == 0 else "error", "exit_code": proc.returncode}
    except Exception as exc:  # noqa: BLE001 — shim must stay alive for /ping
        logger.error("init-workspace failed: %s", exc, exc_info=True)
        return {"status": "error", "reason": str(exc)}


def _opencode_version() -> str:
    exe = shutil.which("opencode")
    if not exe:
        return "not-installed"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or out.stderr.strip() or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _claude_version() -> str:
    """Installed Claude Code version (multi-harness, design D10)."""
    exe = shutil.which("claude")
    if not exe:
        return "not-installed"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or out.stderr.strip() or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _pi_version() -> str:
    """Installed Pi version (add-pi-harness, design D10). Same shape as
    _claude_version: `pi --version` prints the bare version string."""
    exe = shutil.which("pi")
    if not exe:
        return "not-installed"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() or out.stderr.strip() or "unknown"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _workspace_info() -> dict:
    def _describe(path: Path) -> dict:
        info: dict = {"path": str(path), "exists": path.is_dir()}
        if info["exists"]:
            try:
                info["entries"] = sorted(p.name for p in path.iterdir())[:20]
            except OSError as exc:
                info["error"] = str(exc)
        return info

    repo = _describe(REPO_DIR)
    repo["is_git_worktree"] = (REPO_DIR / ".git").exists()
    return {
        "workspace_root": _describe(WORKSPACE_ROOT),
        "repo": repo,
        "state_data": _describe(STATE_DIR / "data"),
        "state_config": _describe(STATE_DIR / "config"),
        "state_claude": _describe(CLAUDE_STATE_REPLICA),
        "claude_local_root": _describe(CLAUDE_CONFIG_DIR_LOCAL),
        "state_pi": _describe(PI_STATE_REPLICA),
        "pi_local_root": _describe(PI_CONFIG_DIR_LOCAL),
    }


def _resolve_latest_opencode_session() -> str | None:
    """Return the latest OpenCode session associated with this worktree."""
    if not OPENCODE_DB_LOCAL.exists():
        return None
    try:
        conn = sqlite3.connect(str(OPENCODE_DB_LOCAL))
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(session)")}
            if "directory" in columns:
                rows = conn.execute(
                    "SELECT id FROM session WHERE directory = ? "
                    "ORDER BY time_updated DESC LIMIT 1", (str(REPO_DIR),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM session ORDER BY time_updated DESC LIMIT 1"
                ).fetchall()
            return rows[0][0] if rows else None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to resolve latest opencode session: %s", exc)
        return None


def _run_task_heartbeat(
    workspace: str, task_id: str, stop_event: threading.Event, harness: str,
    model: str | None = None,
) -> None:
    """Daemon heartbeat: PUT heartbeat_utc every ~30s while running (task 5.7:
    harness field preserved on every heartbeat write; model field present on
    every heartbeat when one was requested — add-task-model-flag task 2.2)."""
    task_started = time.time()
    while not stop_event.is_set():
        stop_event.wait(30)
        if stop_event.is_set():
            break
        with _TASK_LOCK:
            if _TASK_STATE.get("task_id") != task_id or _TASK_STATE.get("state") != "running":
                break
        try:
            status = _download_task_status(workspace)
        except TaskStatusReadError as exc:
            logger.warning("task heartbeat status read failed: %s", exc)
            continue
        if status.get("state") == "running" and status.get("task_id") == task_id:
            _check_task_stall(workspace, task_id, status, task_started)
            status["heartbeat_utc"] = _utcnow()
            status["harness"] = harness  # preserve harness field on heartbeat
            if model:
                status["model"] = model  # preserve model field on heartbeat
            _upload_task_status(workspace, status)


def _check_task_stall(
    workspace: str, task_id: str, status: dict, task_started: float,
) -> None:
    """Stall detector (add-telegram-notifications, task 2.2 / OQ-N1).

    Two independent stall signals, both conservative to avoid noise, and at
    most ONE notification per task (spec: 'The stall notification MUST be
    emitted at most once per task'):

    - persisted heartbeat_utc older than the threshold while the task is
      `running`: the heartbeat uploads have been failing for that long (the
      in-process loop always advances it when uploads succeed) — the literal
      'heartbeat not advancing' of the spec;
    - milestone silence: the harness hooks HAVE produced events for this
      workspace before (so the hook channel provably works) but nothing has
      arrived for longer than the threshold. A workspace whose hooks never
      emitted anything (hooks not seeded, older image) never triggers this
      branch — that degradation must stay silent (spec: 'Harness without seeded
      hooks').

    No-op entirely when the notifier is not configured."""
    notifier = _TELEGRAM["notifier"]
    if notifier is None or task_id in _TELEGRAM_STALLED_TASKS:
        return
    now = time.time()
    if now - task_started < TELEGRAM_STALL_SECONDS:
        return
    stalled_s = None
    heartbeat_ts = _parse_utc(status.get("heartbeat_utc"))
    if heartbeat_ts is not None and now - heartbeat_ts > TELEGRAM_STALL_SECONDS:
        stalled_s = int(now - heartbeat_ts)
    else:
        activity_age = notifier.last_activity_age(workspace)
        if activity_age is not None and activity_age > TELEGRAM_STALL_SECONDS:
            stalled_s = int(activity_age)
    if stalled_s is None:
        return
    _TELEGRAM_STALLED_TASKS.add(task_id)
    _telegram_notify("task-stall", {
        "task_id": task_id,
        "stalled_s": stalled_s,
    }, workspace)


# --- Interactive busy keep-alive watcher (add-interactive-busy-keepalive) -------

def _read_activity_records() -> dict[str, dict]:
    """Parse every activity marker under ACTIVITY_DIR (best-effort)."""
    records: dict[str, dict] = {}
    try:
        entries = list(ACTIVITY_DIR.glob("*.json"))
    except OSError:
        return records
    for entry in entries:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records[entry.stem] = data
    return records


def _gc_activity_records(records: dict[str, dict], now: float) -> None:
    """Unlink marker files old enough to be irrelevant (any state)."""
    for sid, record in records.items():
        ts = record.get("ts")
        if isinstance(ts, (int, float)) and now - ts > BUSY_GC_SECONDS:
            try:
                (ACTIVITY_DIR / f"{sid}.json").unlink(missing_ok=True)
            except OSError:
                pass


def _busy_release_locked(reason: str, now: float) -> int:
    """Release the advisory hold; returns how long it was held. Caller must
    hold _BUSY_LOCK. Advisory-only: a failed SDK call is logged, never raised
    (same posture as the headless task path, D1/R5)."""
    held_s = int(now - (_BUSY_STATE["since"] or now))
    try:
        app.complete_async_task(_BUSY_STATE["handle"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("interactive keep-alive complete_async_task failed: %s", exc)
    _BUSY_STATE["handle"] = None
    _BUSY_STATE["since"] = None
    _BUSY_STATE["last_release_reason"] = reason
    logger.info(
        "interactive keep-alive released (reason=%s, held=%ss)", reason, held_s,
    )
    return held_s


def _busy_keepalive_tick(now: float | None = None) -> dict:
    """One busy-watcher pass (separated from the loop for tests).

    State machine:
    - any session with a fresh "busy" marker  -> hold an advisory async task
      (HealthyBusy on /ping) unless the current episode already hit the cap;
    - hold older than BUSY_MAX_HOLD_SECONDS   -> release + busy-cap notify
      (once per episode: `capped` stays set until the episode ends);
    - no fresh-busy session                   -> release; if the release was
      caused by markers going stale (process died without session.idle)
      rather than a clean idle transition, emit busy-stale.
    """
    now = time.time() if now is None else now
    with _BUSY_LOCK:
        records = _read_activity_records()
        fresh_busy: list[str] = []
        stale_busy: list[str] = []
        for sid, record in records.items():
            ts = record.get("ts")
            if record.get("state") != "busy" or not isinstance(ts, (int, float)):
                continue
            (fresh_busy if now - ts <= BUSY_STALE_SECONDS else stale_busy).append(sid)
        fresh_busy.sort()

        if fresh_busy:
            if _BUSY_STATE["handle"] is None and not _BUSY_STATE["capped"]:
                try:
                    handle = app.add_async_task(
                        "interactive_activity", {"sessions": fresh_busy},
                    )
                    _BUSY_STATE["handle"] = handle
                    _BUSY_STATE["since"] = now
                    logger.info(
                        "interactive keep-alive engaged (sessions=%s)", fresh_busy,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "interactive keep-alive add_async_task failed: %s", exc,
                    )
            elif (
                _BUSY_STATE["handle"] is not None
                and now - (_BUSY_STATE["since"] or now) > BUSY_MAX_HOLD_SECONDS
            ):
                held_s = _busy_release_locked("cap", now)
                _BUSY_STATE["capped"] = True
                _telegram_notify("busy-cap", {
                    "held_s": held_s,
                    "sessions": fresh_busy,
                })
        else:
            if _BUSY_STATE["handle"] is not None:
                if stale_busy:
                    quiet_s = int(min(
                        now - record["ts"]
                        for sid, record in records.items() if sid in stale_busy
                    ))
                    _busy_release_locked("stale", now)
                    _telegram_notify("busy-stale", {
                        "quiet_s": quiet_s,
                        "sessions": sorted(stale_busy),
                    })
                else:
                    _busy_release_locked("idle", now)
            # Episode over (all sessions idle or stale): re-arm the cap.
            _BUSY_STATE["capped"] = False

        _BUSY_STATE["sessions"] = fresh_busy
        _gc_activity_records(records, now)
        return {
            "active": _BUSY_STATE["handle"] is not None,
            "sessions": fresh_busy,
            "capped": _BUSY_STATE["capped"],
        }


def _busy_keepalive_loop() -> None:
    """Daemon watcher: never exits, never raises (same posture as the other
    shim maintenance loops)."""
    while True:
        time.sleep(BUSY_POLL_SECONDS)
        try:
            _busy_keepalive_tick()
        except Exception as exc:  # noqa: BLE001
            logger.warning("interactive keep-alive tick failed: %s", exc)


def _task_output(stdout_data: str, stderr_data: str) -> tuple[str, bool]:
    """Return bounded terminal output, preferring the harness's stdout.

    Harnesses normally write their human-readable result to stdout. On an
    early failure stderr is more useful, so retain it when stdout is empty.
    The tail is preserved when truncating because streaming CLIs typically put
    their final answer at the end of their output.
    """
    output = (stdout_data or "").strip() or (stderr_data or "").strip()
    if len(output) <= TASK_OUTPUT_MAX_CHARS:
        return output, False
    return output[-TASK_OUTPUT_MAX_CHARS:], True


# --- Git-native workflow actions (add-git-native-workflow, design D2/D3/D4) ----
#
# `git-seed`: clone-from-bundle provisioning of the repo worktree + creation
# of the session's work branch, invoked by `sch run/task --branch` AFTER the
# seed bundle has been uploaded to BUNDLE_STAGING_DIR through the tunnel file
# channel and BEFORE the harness starts (spec git-native-workflow, "Seed del
# workspace remoto via git bundle sul tunnel").
#
# `git-snapshot`: mechanical commit-guarantee (design D4) + incremental
# delivery bundle, invoked by `sch fetch`. Without the TASK-26 opt-in no
# credentials are ever involved: both actions operate exclusively on the
# local worktree and the staging dir (spec git-native-workflow, "No git
# credentials in the remote workspace"). With a staged GITHUB_TOKEN the
# reconciliation below additionally wires the recorded `origin` and a
# tmpfs-backed credential helper — still without persisting any secret.


def _git_credential_helper() -> str:
    """The repo-local `credential.helper` value SCH manages (TASK-26).

    A `store` helper pointed at the tmpfs credentials file: git reads the
    token from there at use time, so the token value never lands in
    `.git/config` (which IS checkpointed) or in any process argv. The helper
    string itself carries no secret and is safe to log and persist.
    """
    return f"store --file {GIT_CREDENTIALS_FILE}"


_GITHUB_SSH_ORIGIN_RE = re.compile(
    r"^(?:ssh://git@github\.com(?::443)?/|git@github\.com:)"
    r"([^/\s]+)/([^/\s]+?)(\.git)?/?$",
    re.IGNORECASE,
)
_GITHUB_HTTPS_ORIGIN_RE = re.compile(
    r"^https://(?:[^/@\s]+@)?github\.com(?::443)?"
    r"/([^/\s]+)/([^/\s]+?)(\.git)?/?$",
    re.IGNORECASE,
)


def _sanitize_origin_url(raw) -> str:
    """Normalize an `origin` URL to a credential-free github.com https URL.

    Server-side mirror of the CLI-side rule (TASK-26): embedded userinfo is
    stripped, `git@github.com:`/`ssh://git@github.com/` forms are rewritten
    to https, anything else (non-GitHub hosts, non-https schemes, garbage)
    yields "". The payload is operator-controlled input, so it is validated
    here again regardless of what the client sent.
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text or len(text) > 2000 or any(c in text for c in " \t\r\n\0"):
        return ""
    match = _GITHUB_SSH_ORIGIN_RE.match(text) or _GITHUB_HTTPS_ORIGIN_RE.match(text)
    if not match:
        return ""
    owner, repo, suffix = match.group(1), match.group(2), match.group(3) or ""
    if owner in (".", "..") or repo in (".", ".."):
        return ""
    return f"https://github.com/{owner}/{repo}{suffix}"


def _token_url_safe(token: str) -> bool:
    """True when the token can sit in a credential-store URL line as-is."""
    return bool(token) and not any(c in token for c in " \t\r\n\0@:#?/\\")


def _write_git_credentials_file(token: str) -> bool:
    """Write the tmpfs git credential store (TASK-26), atomically, 0600.

    Total replacement like the provider-keys staging: this holds exactly one
    line, `https://x-access-token:<token>@github.com`, scoped to github.com
    only. Returns True on success. Never logs the value."""
    try:
        GIT_CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(GIT_CREDENTIALS_FILE.parent, 0o700)
        tmp_path = GIT_CREDENTIALS_FILE.with_name(GIT_CREDENTIALS_FILE.name + ".tmp")
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"https://x-access-token:{token}@github.com\n")
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, GIT_CREDENTIALS_FILE)
        return True
    except Exception as exc:  # noqa: BLE001 — git access must never fail an invoke
        logger.warning("could not stage git credentials: %s", exc)
        return False


def _repo_config_get(key: str) -> str | None:
    """Repo-local `git config --get`, or None when unset/failing."""
    proc = _git(["config", "--get", key], REPO_DIR)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _ensure_repo_credential_helper() -> None:
    """Point the repo at the tmpfs credential store, unless the operator set
    their own helper (which is never overwritten)."""
    if not (REPO_DIR / ".git").exists():
        return
    current = _repo_config_get("credential.helper")
    if current == _git_credential_helper():
        return
    if current:
        logger.info("github access: leaving operator credential.helper in place")
        return
    proc = _git(["config", "credential.helper", _git_credential_helper()], REPO_DIR)
    if proc.returncode != 0:
        logger.warning("github access: %s", _git_error(proc, "cannot set credential.helper"))


def _remove_own_repo_credential_helper() -> None:
    """Drop the SCH-managed helper; an operator helper is never touched."""
    if not (REPO_DIR / ".git").exists():
        return
    if _repo_config_get("credential.helper") != _git_credential_helper():
        return
    proc = _git(["config", "--unset", "credential.helper"], REPO_DIR)
    if proc.returncode != 0:
        logger.warning("github access: %s", _git_error(proc, "cannot unset credential.helper"))


def _ensure_sch_origin(origin_url: str) -> None:
    """Add the recorded `origin` when the repo has none (TASK-26).

    Never modifies an existing origin: a repo that already has one keeps it
    (operator-owned until proven SCH-managed). Origins added here carry the
    `remote.origin.schManaged` marker so withdrawal can remove exactly them.
    """
    if not origin_url or not (REPO_DIR / ".git").exists():
        return
    existing = _git(["remote", "get-url", "origin"], REPO_DIR)
    if existing.returncode == 0:
        return
    added = _git(["remote", "add", "origin", origin_url], REPO_DIR)
    if added.returncode != 0:
        logger.warning("github access: %s", _git_error(added, "cannot add origin"))
        return
    marked = _git(["config", "remote.origin.schManaged", "true"], REPO_DIR)
    if marked.returncode != 0:
        logger.warning("github access: %s", _git_error(marked, "cannot mark origin"))
    else:
        logger.info("github access: origin configured for this session")


def _remove_sch_origin() -> None:
    """Remove an SCH-managed `origin`; operator origins are never touched."""
    if not (REPO_DIR / ".git").exists():
        return
    if _repo_config_get("remote.origin.schManaged") != "true":
        return
    removed = _git(["remote", "remove", "origin"], REPO_DIR)
    if removed.returncode != 0:
        logger.warning("github access: %s", _git_error(removed, "cannot remove origin"))
    else:
        logger.info("github access: SCH-managed origin withdrawn")


def _reconcile_github_access() -> None:
    """Apply or withdraw the opt-in GitHub remote access (TASK-26).

    Runs on every invocation after staging, but only for git-native
    workspaces (a recorded branch): any other workspace leaves the repo's
    git config alone. Token present and URL-safe → tmpfs credential store +
    helper pointer + SCH-managed origin from the recorded `originUrl`
    (late opt-in works: the URL was bound at seed, the token may arrive in
    any later invocation). Token absent → the store file, our helper entry
    and our origin are removed (total replacement). Never raises and never
    logs values: git-config drift must degrade to the credential-less
    default, not fail the invocation.
    """
    try:
        state = _read_git_native_state()
        if not state.get("branch"):
            return
        if not REPO_DIR.is_dir() or not (REPO_DIR / ".git").exists():
            return
        staged = _read_staged_provider_keys()
        token = staged.get("SCH_GITHUB_TOKEN", "")
        if token and _token_url_safe(token):
            if not _write_git_credentials_file(token):
                return
            _ensure_repo_credential_helper()
            origin_url = _sanitize_origin_url(state.get("originUrl") or "")
            _ensure_sch_origin(origin_url)
        else:
            if token:
                logger.warning(
                    "github access: staged token is not URL-safe; "
                    "treating the session as credential-less"
                )
            with contextlib.suppress(OSError):
                GIT_CREDENTIALS_FILE.unlink()
            _remove_own_repo_credential_helper()
            _remove_sch_origin()
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.warning("github access reconciliation skipped: %s", exc)


def _git(args: list, cwd: Path, env_extra: dict = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=600, env=env,
    )


def _git_error(proc: subprocess.CompletedProcess, doing: str) -> str:
    detail = (proc.stderr or proc.stdout or "").strip()
    return f"{doing}: {detail}" if detail else doing


def _read_git_native_state() -> dict:
    """Parsed GIT_NATIVE_STATE_FILE content, or {} when absent/unreadable.

    The file lives in STATE_DIR so it rides the state.tar.gz L2 checkpoint:
    a workspace restored after a crash still knows its branch/baseSha and
    `sch fetch` keeps working (recovery scenario)."""
    try:
        data = json.loads(GIT_NATIVE_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_git_native_state(branch: str, base_sha: str, origin_url: str = "") -> None:
    GIT_NATIVE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = GIT_NATIVE_STATE_FILE.with_name(
        f".{GIT_NATIVE_STATE_FILE.name}.{os.getpid()}.tmp"
    )
    record = {
        "mode": "git-native", "branch": branch, "baseSha": base_sha,
        "seeded_utc": _utcnow(),
    }
    if origin_url:
        # Non-secret remote metadata (credential-free https URL, bound at
        # seed like the branch): lets a later invocation apply the opt-in
        # token even when none was staged at seed time (TASK-26).
        record["originUrl"] = origin_url
    temp.write_text(json.dumps(record) + "\n")
    os.replace(temp, GIT_NATIVE_STATE_FILE)


def _wait_workspace_ready_or_error(action: str, timeout_s: float | None = None) -> dict:
    """Shared readiness gate for the git-native actions (same discipline as
    the fs tunnel target): the repo worktree must exist before any git
    operation. Returns an error response dict, or None when ready.

    ``timeout_s`` defaults to the fs/git-native bound; callers with a longer
    budget (prepare-run with ``continue``, TASK-29) pass their own."""
    if timeout_s is None:
        timeout_s = FS_WORKSPACE_READY_TIMEOUT_S
    if not _WORKSPACE_READY.wait(timeout_s) or not REPO_DIR.is_dir():
        return {
            "status": "error", "action": action,
            "error": "workspace bootstrap did not complete (phase={})".format(
                BOOT_STATE.get("phase", "unknown")
            ),
        }
    return None


def _handle_git_seed(payload: dict) -> dict:
    """Seed the repo worktree from BUNDLE_STAGING_DIR/seed.bundle and create
    the work branch (spec runtime-image, "`git-seed` action in the shim").

    Failure contract: no partial mutations visible to the harness. The
    validation chain (branch format -> not-already-seeded -> bundle present
    -> `git bundle verify`) runs before any object enters the repo; the only
    fetch/checkout failure mode after that leaves the worktree untouched
    (git refuses checkouts that would clobber files and aborts atomically).
    """
    not_ready = _wait_workspace_ready_or_error("git-seed")
    if not_ready:
        return not_ready

    branch = (payload.get("branch") or "").strip()
    if not branch:
        return {"status": "error", "action": "git-seed", "error": "missing 'branch'"}
    check = _git(["check-ref-format", "--branch", branch], REPO_DIR)
    if check.returncode != 0:
        return {
            "status": "error", "action": "git-seed",
            "error": f"invalid branch name '{branch}'",
        }

    existing = _read_git_native_state()
    if existing.get("branch"):
        return {
            "status": "error", "action": "git-seed",
            "error": (
                "workspace already seeded (branch '{}' provisioned); "
                "double seed is refused".format(existing["branch"])
            ),
        }

    bundle = BUNDLE_STAGING_DIR / SEED_BUNDLE_NAME
    if not bundle.is_file():
        return {
            "status": "error", "action": "git-seed",
            "error": f"seed bundle not found in staging ({bundle})",
        }

    # init-workspace.sh already `git init`s an empty repo; be robust to a
    # bare directory anyway (idempotent, no-op on an existing repo).
    if not (REPO_DIR / ".git").exists():
        init = _git(["init"], REPO_DIR)
        if init.returncode != 0:
            return {"status": "error", "action": "git-seed",
                    "error": _git_error(init, "git init failed")}

    head = _git(["rev-parse", "--verify", "-q", "HEAD"], REPO_DIR)
    if head.returncode == 0:
        return {
            "status": "error", "action": "git-seed",
            "error": (
                "repo path already contains history; refusing to seed over "
                "an existing clone"
            ),
        }

    verify = _git(["bundle", "verify", str(bundle)], REPO_DIR)
    if verify.returncode != 0:
        return {"status": "error", "action": "git-seed",
                "error": _git_error(verify, "seed bundle failed verification")}

    fetch = _git(["fetch", str(bundle), "HEAD"], REPO_DIR)
    if fetch.returncode != 0:
        return {"status": "error", "action": "git-seed",
                "error": _git_error(fetch, "cannot fetch from seed bundle")}
    base = _git(["rev-parse", "FETCH_HEAD"], REPO_DIR)
    if base.returncode != 0:
        return {"status": "error", "action": "git-seed",
                "error": _git_error(base, "cannot resolve seeded HEAD")}
    base_sha = base.stdout.strip()

    checkout = _git(["checkout", "-b", branch, "FETCH_HEAD"], REPO_DIR)
    if checkout.returncode != 0:
        return {"status": "error", "action": "git-seed",
                "error": _git_error(checkout, "cannot checkout work branch")}

    # Opt-in GitHub origin (TASK-26): record the client's sanitized origin URL
    # ("" when the local repo has no usable GitHub origin — the default stays
    # credential-less with no origin). Server-side sanitized again: the
    # payload is operator input.
    origin_url = _sanitize_origin_url(payload.get("originUrl") or "")
    _write_git_native_state(branch, base_sha, origin_url)
    with contextlib.suppress(OSError):
        bundle.unlink()  # staging cleanup: never checkpointed, never re-read
    logger.info("git-seed: seeded branch '%s' at %s", branch, base_sha)
    # The per-invocation reconciliation ran before this seed existed, so apply
    # it now: a token staged in the seed invocation takes effect immediately.
    _reconcile_github_access()
    response = {"status": "ok", "action": "git-seed", "branch": branch, "baseSha": base_sha}
    if origin_url:
        response["originUrl"] = origin_url
    return response


def _handle_git_snapshot(payload: dict) -> dict:  # noqa: ARG001 — no inputs by contract (design D6)
    """Mechanical snapshot + incremental delivery bundle (spec runtime-image,
    "`git-snapshot` action in the shim"). Idempotent: a clean worktree never
    creates a commit; `no-work` is reported explicitly instead of producing
    an unimportable empty bundle."""
    not_ready = _wait_workspace_ready_or_error("git-snapshot")
    if not_ready:
        return not_ready

    state = _read_git_native_state()
    branch = (state.get("branch") or "").strip()
    base_sha = (state.get("baseSha") or "").strip()
    if not branch or not base_sha:
        return {
            "status": "error", "action": "git-snapshot",
            "error": "workspace is not seeded for git-native mode (no git-seed recorded)",
        }

    porcelain = _git(["status", "--porcelain"], REPO_DIR)
    if porcelain.returncode != 0:
        return {"status": "error", "action": "git-snapshot",
                "error": _git_error(porcelain, "git status failed")}
    dirty = bool(porcelain.stdout.strip())

    snapshot_committed = False
    if dirty:
        # Commit-guarantee (design D4): service commit distinguishable from
        # the agent's own commits by the conventional author and message.
        ws = _resolve_workspace_name() or "unknown"
        identity = {
            "GIT_AUTHOR_NAME": f"sch-session {ws}",
            "GIT_AUTHOR_EMAIL": "sch-session@local",
            "GIT_COMMITTER_NAME": f"sch-session {ws}",
            "GIT_COMMITTER_EMAIL": "sch-session@local",
        }
        add = _git(["add", "-A"], REPO_DIR, env_extra=identity)
        if add.returncode != 0:
            return {"status": "error", "action": "git-snapshot",
                    "error": _git_error(add, "git add failed")}
        commit = _git(
            ["commit", "-m", f"wip: session snapshot ({_utcnow()})"],
            REPO_DIR, env_extra=identity,
        )
        if commit.returncode != 0:
            return {"status": "error", "action": "git-snapshot",
                    "error": _git_error(commit, "snapshot commit failed")}
        snapshot_committed = True

    head = _git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], REPO_DIR)
    if head.returncode != 0:
        return {
            "status": "error", "action": "git-snapshot",
            "error": f"work branch '{branch}' not found in the repo",
        }
    head_sha = head.stdout.strip()

    if head_sha == base_sha:
        # Explicit "nothing to deliver" (clean worktree, no commits past the
        # base): the client exits cleanly without attempting an import.
        return {
            "status": "no-work", "action": "git-snapshot",
            "branch": branch, "headSha": head_sha,
            "snapshotCommitted": False, "baseSha": base_sha,
        }

    BUNDLE_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    bundle = BUNDLE_STAGING_DIR / DELIVERY_BUNDLE_NAME
    create = _git(
        ["bundle", "create", str(bundle), f"{base_sha}..refs/heads/{branch}"],
        REPO_DIR,
    )
    if create.returncode != 0:
        return {"status": "error", "action": "git-snapshot",
                "error": _git_error(create, "cannot create delivery bundle")}

    logger.info(
        "git-snapshot: branch=%s head=%s committed=%s bundle=%s (%d bytes)",
        branch, head_sha, snapshot_committed, bundle, bundle.stat().st_size,
    )
    return {
        "status": "ok", "action": "git-snapshot",
        "branch": branch, "headSha": head_sha, "baseSha": base_sha,
        "snapshotCommitted": snapshot_committed,
        "bundleRef": DELIVERY_BUNDLE_NAME,
    }


def _repo_empty_or_unseeded() -> bool:
    if not REPO_DIR.is_dir() or not (REPO_DIR / ".git").exists():
        return True
    try:
        return _git(["rev-parse", "--verify", "-q", "HEAD"], REPO_DIR).returncode != 0
    except Exception:  # noqa: BLE001
        return True


def _handle_session_import(payload: dict) -> dict:  # noqa: ARG001
    action = "session-import"
    version = _opencode_version()
    not_ready = _wait_workspace_ready_or_error(action)
    if not_ready:
        not_ready["opencodeVersion"] = version
        return not_ready
    if _resolve_harness() != "opencode":
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": "session handoff requires a workspace bound to harness=opencode"}
    staged = BUNDLE_STAGING_DIR / HANDOFF_FILE_NAME
    if not staged.is_file():
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": f"handoff file not found in staging ({staged})"}
    try:
        exported = json.loads(staged.read_text(encoding="utf-8"))
        session_id = str((exported.get("info") or {}).get("id") or "").strip()
    except (OSError, ValueError, AttributeError) as exc:
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": f"invalid handoff export: {exc}"}
    if not session_id:
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": "invalid handoff export: missing info.id"}
    reimported = False
    if OPENCODE_DB_LOCAL.exists():
        try:
            with sqlite3.connect(str(OPENCODE_DB_LOCAL)) as conn:
                reimported = conn.execute(
                    "SELECT 1 FROM session WHERE id = ?", (session_id,)
                ).fetchone() is not None
        except sqlite3.Error:
            pass
    exe = shutil.which("opencode")
    if not exe:
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": "opencode is not installed in the runtime image"}
    # Same child-environment discipline as the task/serve spawns: this runs the
    # real `opencode` binary through the dispatcher, so it gets the staged key
    # set (import itself needs no provider, but no shim-spawned harness process
    # is left with a stale key set).
    env = _child_env_with_provider_keys()
    env["SCH_HARNESS"] = "opencode"
    try:
        proc = subprocess.run(
            [exe, "import", str(staged)], cwd=str(REPO_DIR), env=env,
            capture_output=True, text=True, timeout=600,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": f"opencode import failed: {exc}"}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown error").strip()[-4000:]
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": f"opencode import failed: {detail}"}
    try:
        with sqlite3.connect(str(OPENCODE_DB_LOCAL)) as conn:
            row = conn.execute(
                "SELECT time_updated FROM session WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("imported session is absent from opencode.db")
            if _resolve_latest_opencode_session() != session_id:
                maximum = conn.execute("SELECT COALESCE(MAX(time_updated), 0) FROM session").fetchone()[0]
                conn.execute(
                    "UPDATE session SET time_updated = ? WHERE id = ?",
                    (max(int(time.time() * 1000), int(maximum) + 1), session_id),
                )
                conn.commit()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": f"cannot verify imported session recency: {exc}"}
    if _resolve_latest_opencode_session() != session_id:
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": "imported session did not become the latest session"}
    durable = _backup_db_durable()
    if durable.get("status") != "ok":
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": f"durable opencode.db backup failed: {durable.get('db_backup')}"}
    try:
        staged.unlink()
    except OSError as exc:
        return {"status": "error", "action": action, "opencodeVersion": version,
                "error": f"import succeeded but staged cleanup failed: {exc}"}
    return {"status": "ok", "action": action, "sessionID": session_id,
            "opencodeVersion": version, "repoEmpty": _repo_empty_or_unseeded(),
            "reimported": reimported}


def _handle_task_action(payload: dict) -> dict:
    """Non-blocking submit handler for action == 'task' (spec: headless-task-execution)."""
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return {"status": "error", "message": "missing or empty 'prompt'"}

    # Optional per-invocation model (spec: headless-task-execution, "Campo
    # modello opzionale sull'azione task"). Second line of defense after the
    # client-side check: a present-but-malformed value is rejected BEFORE any
    # mutation — no _TASK_STATE update, no S3 status upload, no worker thread
    # — so the task slot stays free for a corrected resubmit.
    model_present = "model" in payload
    model = payload.get("model")
    if model_present and (
        not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model)
    ):
        return {
            "status": "error",
            "message": "invalid model: expected non-empty [A-Za-z0-9._:/-]+",
        }
    model = model if model_present else None

    timeout_s = payload.get("timeout_s")
    try:
        timeout_s = int(timeout_s) if timeout_s is not None else SCH_TASK_TIMEOUT_S
    except (TypeError, ValueError):
        timeout_s = SCH_TASK_TIMEOUT_S
    timeout_s = max(_TASK_TIMEOUT_MIN, min(_TASK_TIMEOUT_MAX, timeout_s))

    continue_session = bool(payload.get("continue"))
    session_id_hint = (payload.get("session_id_hint") or "").strip() or None
    workspace = _resolve_workspace_name()
    if not workspace:
        return {"status": "error", "message": "workspace identity not yet resolved"}

    # Multi-harness (task 5.1): accept harness in the payload; reject (no
    # thread started) when it differs from the persisted marker. The marker
    # is the source of truth (written by `sch` and by the L2 manifest
    # restore), so a divergent payload means the client disagrees with the
    # workspace's bound harness — refuse to launch either binary (spec:
    # harness-selection, "Azione task con harness divergente rifiutata").
    payload_harness = (payload.get("harness") or "").strip().lower() or None
    marker_harness = _resolve_harness()
    if payload_harness and payload_harness != marker_harness:
        return {
            "status": "error",
            "message": (
                f"harness mismatch: payload='{payload_harness}' but workspace "
                f"'{workspace}' is bound to harness='{marker_harness}' (mutual "
                "exclusivity per-workspace; use a new workspace name or the "
                "documented reset path to switch)"
            ),
        }
    harness = marker_harness

    with _TASK_LOCK:
        if _TASK_STATE.get("state") == "running":
            return {
                "status": "busy",
                "message": "a task is already running for this workspace",
                "task_id": _TASK_STATE.get("task_id"),
            }

        task_id = uuid.uuid4().hex
        started = _utcnow()
        _TASK_STATE.update({
            "task_id": task_id,
            "state": "running",
            "prompt": prompt,
            "started_utc": started,
            "finished_utc": None,
            "heartbeat_utc": started,
            "exit_code": None,
            "harness": harness,
            "model": model,
            "harness_session_id": session_id_hint if continue_session else None,
            # Back-compat: keep the opencode-specific field name populated for
            # harness=opencode (existing `sch status` reads may still
            # reference it). For harness=claude it stays None.
            "opencode_session_id": session_id_hint if (continue_session and harness == "opencode") else None,
            "error": None,
            "thread": None,
            "async_task_handle": None,
        })

    # Persist initial running status before spawning the thread (task 5.7:
    # harness field present at submit, every heartbeat, and on every terminal
    # state). Sibling object is overwrite-only, independent of the manifest.
    initial_status = {
        "task_id": task_id,
        "state": "running",
        "prompt": prompt,
        "harness": harness,
        "started_utc": started,
        "heartbeat_utc": started,
        "harness_session_id": session_id_hint if continue_session else None,
    }
    if continue_session:
        # Continuation was requested; the worker resolves the session after
        # the workspace is ready and records continue_resolved (TASK-27).
        # Present-iff-requested, like the model field below.
        initial_status["continue_requested"] = True
    if model:
        # Observability field (spec: headless-task-execution, "Task model
        # observability"): present only when a model was requested.
        initial_status["model"] = model
    else:
        # dict.update never removes keys: drop any model left over from a
        # previous model-bearing task so "no model requested" always renders
        # as an absent field.
        _TASK_STATUS.pop("model", None)
    # Same leftover discipline for the terminal-notification fields (TASK-28):
    # they describe the previous task's delivery, never this running one.
    for field in NOTIFICATION_FIELDS:
        _TASK_STATUS.pop(field, None)
    _TASK_STATUS.update(initial_status)
    if not _upload_task_status(workspace, initial_status):
        with _TASK_LOCK:
            _TASK_STATE.update({"state": "none", "task_id": None, "thread": None})
        return {
            "status": "error",
            "message": "cannot persist initial task status; task was not started",
        }

    # Telegram (add-telegram-notifications, task 2.1): submit accepted — the
    # notify call is a non-blocking enqueue, the S3 write above never waited
    # for it, and it is a no-op when the feature is unconfigured.
    _telegram_notify("task-submitted", {
        "task_id": task_id,
        "harness": harness,
        "model": model,
        "prompt": prompt,
    }, workspace)

    thread = threading.Thread(
        target=_run_task,
        name=f"sch-task-{task_id}",
        daemon=True,
        args=(task_id, prompt, timeout_s, continue_session, session_id_hint, workspace, harness, model),
    )
    with _TASK_LOCK:
        _TASK_STATE["thread"] = thread
    thread.start()

    response = {"status": "accepted", "task_id": task_id, "harness": harness}
    if model:
        # Echo the accepted model (design D4 of add-task-model-flag): the
        # client uses the missing echo to detect a runtime image that
        # predates the feature. Key present only when a model was requested.
        response["model"] = model
    if continue_session:
        # Echo the accepted continuation request (TASK-27, same contract as
        # the prepare-run action and the model echo above): the client uses
        # the missing echo to detect a runtime image that predates the
        # feature. Whether a prior session was actually found is recorded
        # later in the persisted status (continue_resolved).
        response["continue"] = True
    if _INTERACTIVE_ACTIVE:
        response["warning"] = (
            "interactive TUI active on this workspace — dual writer on harness state risks corruption"
        )
    return response


def _serve_is_alive() -> bool:
    proc = _SERVE_STATE.get("proc")
    return proc is not None and proc.poll() is None


def _serve_supervisor_loop() -> None:
    """Background supervisor for the shared `opencode web` backend.

    Mirrors the Popen conventions of _run_task (process-group start,
    inherited+overridden env so harness-wrapper.sh dispatches to opencode
    even though SCH_HARNESS may not otherwise be set for the shim's own
    process env) but is long-lived instead of communicate()-bounded: polls
    liveness and (re)starts on crash, gated on the same workspace-readiness
    signal _run_task waits for (READY_MARKER + REPO_DIR), so a not-yet-seeded
    workspace never gets a server started against an empty/default config.
    Never started eagerly at boot — only lazily, on the first `serve-ensure`
    action — so workspaces that never use `sch attach` or `sch web` pay no cost.
    """
    while True:
        with _SERVE_LOCK:
            proc = _SERVE_STATE.get("proc")
            alive = proc is not None and proc.poll() is None
            ready = READY_MARKER.exists() and REPO_DIR.exists()
            if not alive and ready:
                if proc is not None:
                    logger.warning(
                        "opencode web exited (code=%s); restarting (restart #%d)",
                        proc.returncode, _SERVE_STATE["restart_count"] + 1,
                    )
                    _SERVE_STATE["restart_count"] += 1
                opencode_exe = shutil.which("opencode") or "opencode"
                argv = [
                    opencode_exe, "web",
                    "--hostname", "127.0.0.1",
                    "--port", str(OPENCODE_SERVE_PORT),
                ]
                try:
                    # add-user-provider-keys (design D7): the supervisor is a
                    # child of the shim, so it gets the staged key set in its
                    # spawn environment (the dispatcher then maps the canonical
                    # names). A key rotated after this process started applies
                    # from its next restart — accepted, documented (design D4).
                    proc_env = _child_env_with_provider_keys()
                    proc_env["SCH_HARNESS"] = "opencode"
                    new_proc = subprocess.Popen(
                        argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        cwd=str(REPO_DIR),
                        start_new_session=True,
                        env=proc_env,
                    )
                    _SERVE_STATE["proc"] = new_proc
                    _SERVE_STATE["port"] = OPENCODE_SERVE_PORT
                    _SERVE_STATE["started_utc"] = _utcnow()
                    logger.info(
                        "opencode web started: pid=%d port=%d", new_proc.pid, OPENCODE_SERVE_PORT,
                    )
                except Exception as exc:  # noqa: BLE001 — supervisor must never die
                    logger.error("failed to start opencode web: %s", exc, exc_info=True)
        time.sleep(3)


def _ensure_serve_supervisor_started() -> None:
    with _SERVE_LOCK:
        if not _SERVE_STATE["supervisor_started"]:
            _SERVE_STATE["supervisor_started"] = True
            threading.Thread(
                target=_serve_supervisor_loop, name="sch-serve-supervisor", daemon=True,
            ).start()


def _build_headless_argv(
    harness: str, session_id: str | None, prompt: str, model: str | None = None,
    variant: str | None = None,
) -> list:
    """Per-harness headless argv builder (design D6, tasks 5.2/5.3; model
    mapping: design D3 of add-task-model-flag).

    - opencode: `opencode run --session <id>? --model <id>? --variant <v>? --agent remote-auto? --auto <prompt>`
    - claude:   `claude -p --resume <id>? --model <id>? --agent remote-auto? --dangerously-skip-permissions <prompt>`
    - pi:       `pi -p --session <path>? --provider amazon-bedrock --model <id>?
                 --append-system-prompt <remote-auto role file>? <prompt>`

    The auto-approval flag AND the agent selection are argv-only (TUI path
    stays flag-free, design D6 + sch-remote-agents). stdin < /dev/null is
    applied by the caller (Popen(stdin=DEVNULL)) for ALL harnesses (D7
    default-on; OQ-MCP2 stays open — to be lifted empirically for claude only
    if `claude -p` is confirmed not to hang with a TTY). The optional model
    is inserted as a discrete `--model <id>` argv pair (no shell
    interpolation); when empty the argv is byte-for-byte unchanged. The
    optional variant (opencode only: `--variant`, the provider-specific
    reasoning effort such as `high`) follows the same discrete-pair rule and
    is ignored for the other harnesses, which have no such flag on their
    headless argv.

    add-pi-harness (design D3/D4): the pi argv carries NO auto-approval flag —
    Pi has no permission prompt by design, so the unattended-execution
    requirement is satisfied by construction (spec: headless-task-execution).
    Pi also has no agent files, so the remote-auto contract rides
    `--append-system-prompt` pointed at the seeded role file. The SCH extension
    never intercepts tool calls, in headless or interactive mode.
    """
    if harness == "pi":
        pi_exe = shutil.which("pi") or "pi"
        argv = [pi_exe, "-p"]
        if session_id:
            argv += ["--session", session_id]
        if model:
            # Pi selects a model as a provider/model PAIR. The id stays opaque
            # (spec: run-model-selection): an id Pi does not know produces Pi's
            # own error, never an `sch`-side validation failure.
            argv += ["--provider", PI_BEDROCK_PROVIDER, "--model", model]
        # `--append-system-prompt` accepts text OR a file path (pi --help:
        # "Append text or file contents to the system prompt"), so the seeded
        # role file is passed by path — no shell interpolation, no content
        # inlining. A missing file (operator deleted it and the re-seed has not
        # run yet) simply omits the role: the task still runs, degraded to Pi's
        # default system prompt, rather than failing.
        if _TASK_AGENT and PI_ROLE_REMOTE_AUTO.is_file():
            argv += ["--append-system-prompt", str(PI_ROLE_REMOTE_AUTO)]
        argv += [prompt]
        return argv
    if harness == "claude":
        argv = ["claude", "-p"]
        if session_id:
            argv += ["--resume", session_id]
        if model:
            argv += ["--model", model]
        if _TASK_AGENT:
            argv += ["--agent", _TASK_AGENT]
        argv += ["--dangerously-skip-permissions", prompt]
        return argv
    # opencode (default)
    opencode_exe = shutil.which("opencode") or "opencode"
    argv = [opencode_exe, "run"]
    if session_id:
        argv += ["--session", session_id]
    if model:
        argv += ["--model", model]
    if variant and MODEL_ID_RE.fullmatch(variant):
        # Reasoning-effort variant (e.g. `high`): discrete pair like --model.
        # A malformed value is dropped rather than failing the task — the turn
        # then runs with the model's default effort.
        argv += ["--variant", variant]
    if _TASK_AGENT:
        argv += ["--agent", _TASK_AGENT]  # default "remote-auto" (sch-remote-agents)
    argv += _TASK_AUTO_APPROVE_FLAGS  # default ["--auto"]
    argv += [prompt]
    return argv


def _resolve_latest_harness_session(harness: str) -> str | None:
    """Per-harness 'latest session' resolver (design D5, task 5.4/5.5)."""
    if harness == "claude":
        return _resolve_latest_claude_session()
    if harness == "pi":
        return _resolve_latest_pi_session()
    return _resolve_latest_opencode_session()


def _headless_harness_env(harness: str) -> dict[str, str]:
    env = _child_env_with_provider_keys()
    env["SCH_HARNESS"] = harness
    env["SCH_EXECUTION_MODE"] = "headless"
    for name in (
        "SCH_TELEGRAM_BOT_TOKEN",
        "SCH_TELEGRAM_CHAT_ID",
        "SCH_TELEGRAM_COMMANDS_TABLE",
        "SCH_TELEGRAM_ROUTING_TABLE",
        "SCH_TELEGRAM_NOTIFICATIONS_CONFIGURED",
    ):
        env.pop(name, None)
    return env


def _resolve_continue_session(
    continue_session: bool, session_id_hint: str | None, harness: str
) -> tuple[str | None, bool | None]:
    """Resolve the harness session a --continue task should resume.

    Returns (session_id, resolved) where resolved is None when no
    continuation was requested, True when a session was found (an explicit
    handoff hint wins without a store lookup), and False when continuation
    was requested but nothing was found. The False case still degrades to a
    fresh session (spec R2) — the caller persists the miss so it is visible
    instead of silent.
    """
    if not continue_session:
        return None, None
    if session_id_hint:
        return session_id_hint, True
    found = _resolve_latest_harness_session(harness)
    return found, found is not None


def _persist_continue_outcome(
    workspace: str,
    task_id: str,
    harness_session_id: str | None,
    continue_resolved: bool | None,
) -> None:
    """Record the post-ready continuation outcome in-memory and on S3.

    The running status submitted at accept time carries continue_requested
    without knowing the outcome yet; this fills in harness_session_id and
    continue_resolved so `sch status` shows whether the task really resumed
    a prior session. Best-effort like the heartbeat (a lost race with a
    concurrent heartbeat beat only delays the fields by one beat).
    """
    with _TASK_LOCK:
        if _TASK_STATE.get("task_id") != task_id or _TASK_STATE.get("state") != "running":
            return
        _TASK_STATE["harness_session_id"] = harness_session_id
        _TASK_STATUS["harness_session_id"] = harness_session_id
        _TASK_STATUS["continue_resolved"] = continue_resolved
        body = dict(_TASK_STATUS)
    if not _upload_task_status(workspace, body):
        logger.warning("continue outcome upload failed for task %s", task_id)


def _run_task(
    task_id: str,
    prompt: str,
    timeout_s: int,
    continue_session: bool,
    session_id_hint: str | None,
    workspace: str,
    harness: str,
    model: str | None = None,
) -> None:
    """Background worker for a headless task (design D1, D5, D6, D7, D8)."""
    started = _utcnow()
    started_ts = time.time()
    harness_session_id = None

    # 1. Advertise HealthyBusy via the SDK (advisory-only: the thread is the
    # source of truth; if the SDK call fails we proceed anyway — D1/R5).
    async_handle = None
    try:
        async_handle = app.add_async_task("headless_task", {"task_id": task_id, "harness": harness})
        with _TASK_LOCK:
            if _TASK_STATE.get("task_id") == task_id:
                _TASK_STATE["async_task_handle"] = async_handle
    except Exception as exc:  # noqa: BLE001
        logger.warning("add_async_task advisory call failed: %s", exc)

    # 2. Continuation is resolved AFTER the workspace-ready wait below
    # (TASK-27): on a cold boot the session store (opencode.db for opencode,
    # JSONL/dirs for claude/pi) is restored asynchronously, so resolving here
    # would miss pre-stop sessions and silently degrade to a fresh session.
    # harness_session_id stays None until the post-ready resolution.
    harness_session_id = None
    continue_resolved: bool | None = None

    # 3. Start heartbeat thread.
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_run_task_heartbeat,
        args=(workspace, task_id, heartbeat_stop, harness, model),
        name=f"sch-task-hb-{task_id}",
        daemon=True,
    )
    heartbeat_thread.start()

    # 4. Wait for the workspace to be ready before launching the harness.
    ready_marker = _harness_ready_marker(harness)
    ready_deadline = time.monotonic() + 240
    while time.monotonic() < ready_deadline:
        if REPO_DIR.exists() and ready_marker.exists():
            break
        time.sleep(1)
    else:
        logger.error("task %s timed out waiting for workspace ready (harness=%s)", task_id, harness)
        error = "workspace did not become ready before task start"
        state = "failed"
        _finish_task(
            task_id=task_id,
            prompt=prompt,
            started=started,
            started_ts=started_ts,
            harness=harness,
            harness_session_id=harness_session_id,
            state=state,
            exit_code=None,
            error=error,
            output="",
            output_truncated=False,
            workspace=workspace,
            async_handle=async_handle,
            heartbeat_stop=heartbeat_stop,
            heartbeat_thread=heartbeat_thread,
            model=model,
            continue_requested=continue_session,
            continue_resolved=continue_resolved,
        )
        return

    # 4b. Post-ready continuation resolution (TASK-27, design D5): the
    # session-store restore has landed by now (the wait above), so a
    # pre-stop session is found instead of silently degrading to fresh.
    if continue_session:
        harness_session_id, continue_resolved = _resolve_continue_session(
            continue_session, session_id_hint, harness
        )
        if harness_session_id:
            logger.info(
                "task %s continuing %s session %s", task_id, harness, harness_session_id,
            )
        else:
            logger.info(
                "task %s requested continue but no %s session found; starting new",
                task_id, harness,
            )
        _persist_continue_outcome(
            workspace, task_id, harness_session_id, continue_resolved
        )

    # 5. Build the per-harness headless argv (design D6, task 5.2). The argv
    # passes through the dispatcher (harness-wrapper.sh shadows both
    # /usr/local/bin/opencode and /usr/local/bin/claude), so the ENV bridge
    # is applied even on the headless path (spec: runtime-image,
    # "Esecuzione headless riusa il wrapper dispatcher").
    effective_model = model
    effective_variant = None
    if harness == "opencode" and not effective_model:
        # No explicit --model: forward the resumed session's own stored
        # model+variant so a headless --continue keeps the TUI selection
        # (model and reasoning effort, e.g. `high`). Without this the
        # --agent remote-auto switch resolves the turn to the agent's
        # configured model and default effort, discarding the selection and
        # clobbering the session row. An explicit --model always wins (its
        # variant is the model's default: a stored variant belongs to the
        # previous model). (None, None) degrades to the harness default.
        effective_model, effective_variant = _opencode_continue_model(harness_session_id)
        if effective_model:
            logger.info(
                "task %s continuing opencode session %s with model %s%s",
                task_id, harness_session_id, effective_model,
                " variant {}".format(effective_variant) if effective_variant else "",
            )
    argv = _build_headless_argv(
        harness, harness_session_id, prompt, effective_model, effective_variant,
    )

    proc = None
    exit_code = None
    error = None
    state = "failed"
    stdout_data = ""
    stderr_data = ""
    try:
        logger.info("task %s running (harness=%s): %s", task_id, harness, " ".join(argv))
        # Run the harness in a new session/process-group so we can reliably
        # kill it and any child processes (npm/node/MCP servers) on timeout.
        # subprocess.run(timeout=...) only sends SIGTERM to the direct child
        # and may leave orphaned children keeping the task alive.
        # SCH_HARNESS is set in the subprocess env so harness-wrapper.sh
        # dispatches correctly even though $0 basename would also work.
        proc_env = _headless_harness_env(harness)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,  # D7: stdin < /dev/null for every harness
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_DIR),
            start_new_session=True,
            env=proc_env,
        )
        try:
            stdout_data, stderr_data = proc.communicate(timeout=timeout_s)
            exit_code = proc.returncode
            if exit_code == 0:
                state = "succeeded"
            else:
                state = "failed"
                stderr_tail = (stderr_data or "")[-4000:]
                error = stderr_tail or f"exit code {exit_code}"
        except subprocess.TimeoutExpired:
            # Kill the entire process group (negative PID) so children die too.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                stdout_data, stderr_data = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            state = "timed-out"
            exit_code = proc.returncode
            stderr_tail = (stderr_data or "")[-4000:]
            error = stderr_tail or "application timeout expired"
            logger.warning("task %s timed out after %ss (harness=%s)", task_id, timeout_s, harness)
    except Exception as exc:  # noqa: BLE001
        state = "failed"
        error = str(exc)
        logger.error("task %s runner failed: %s", task_id, exc, exc_info=True)

    output, output_truncated = _task_output(stdout_data, stderr_data)
    _finish_task(
        task_id=task_id,
        prompt=prompt,
        started=started,
        started_ts=started_ts,
        harness=harness,
        harness_session_id=harness_session_id,
        state=state,
        exit_code=exit_code,
        error=error,
        output=output,
        output_truncated=output_truncated,
        workspace=workspace,
        async_handle=async_handle,
        heartbeat_stop=heartbeat_stop,
        heartbeat_thread=heartbeat_thread,
        model=model,
        continue_requested=continue_session,
        continue_resolved=continue_resolved,
    )


def _finish_task(
    *,
    task_id: str,
    prompt: str,
    started: str,
    started_ts: float,
    harness: str,
    harness_session_id: str | None,
    state: str,
    exit_code: int | None,
    error: str | None,
    output: str,
    output_truncated: bool,
    workspace: str,
    async_handle,
    heartbeat_stop: threading.Event,
    heartbeat_thread: threading.Thread,
    model: str | None = None,
    continue_requested: bool = False,
    continue_resolved: bool | None = None,
) -> None:
    """Common terminal-state cleanup for _run_task (tasks 5.7, 6.6)."""
    finished = _utcnow()
    duration_s = int(time.time() - started_ts)

    # Stop heartbeat.
    heartbeat_stop.set()
    heartbeat_thread.join(timeout=5)

    # Forced L2 checkpoint before completing async task (design D8, task 6.6:
    # fires for BOTH harnesses; if it fails, the task status sibling is still
    # persisted with the real outcome plus a checkpoint-failed warning).
    checkpoint_warning = None
    checkpoint_status = "failed"
    try:
        checkpoint_result = _do_checkpoint(force=True)
        db_checkpoint_ok = checkpoint_result.get("db_backup") in ("ok", "no-local-db")
        if (
            checkpoint_result.get("status") == "ok"
            and checkpoint_result.get("manifest_written")
            and db_checkpoint_ok
        ):
            checkpoint_status = "confirmed"
        else:
            checkpoint_warning = f"checkpoint status: {checkpoint_result.get('status')}"
    except Exception as exc:  # noqa: BLE001
        checkpoint_warning = f"checkpoint exception: {exc}"
        logger.warning("forced checkpoint at task end failed: %s", exc)

    # Complete async task so /ping returns Healthy (R5: log and continue on error).
    if async_handle is not None:
        try:
            app.complete_async_task(async_handle)
        except Exception as exc:  # noqa: BLE001
            logger.warning("complete_async_task failed for task %s: %s", task_id, exc)

    # Persist terminal status and update in-memory slot (task 5.7: harness
    # field present on every terminal state). For harness=opencode keep the
    # legacy `opencode_session_id` field populated for back-compat with
    # existing `sch status` readers; the spec's canonical field is
    # `harness_session_id` (used by the new bin/sch status display).
    terminal_status = {
        "task_id": task_id,
        "state": state,
        "prompt": prompt,
        "harness": harness,
        "started_utc": started,
        "finished_utc": finished,
        "duration_s": duration_s,
        "heartbeat_utc": finished,
        "exit_code": exit_code,
        "harness_session_id": harness_session_id,
        "opencode_session_id": harness_session_id if harness == "opencode" else None,
        "error": error,
        "output": output,
        "checkpoint_status": checkpoint_status,
    }
    if model:
        # Terminal record keeps the model for post-hoc provenance (spec:
        # headless-task-execution, "Task model observability").
        terminal_status["model"] = model
    if continue_requested:
        # Continuation provenance (TASK-27): present iff requested, like the
        # model field. continue_resolved is present iff the worker resolved
        # the request (absent on paths that never reach resolution, e.g. a
        # workspace that never became ready).
        terminal_status["continue_requested"] = True
        if continue_resolved is not None:
            terminal_status["continue_resolved"] = continue_resolved
    if output_truncated:
        terminal_status["output_truncated"] = True
    if checkpoint_warning:
        terminal_status["checkpoint_warning"] = checkpoint_warning
    if telegram_notifier.enabled():
        # TASK-28: promise a terminal notification. The notifier's success
        # hook flips it to "delivered"; the external watchdog re-sends any
        # record still pending after its grace period (microVM died before
        # the flush, Telegram unreachable from here, ...). Present iff the
        # channel is configured, so the disabled runtime stays byte-identical.
        terminal_status[NOTIFICATION_STATUS_FIELD] = NOTIFICATION_PENDING

    with _TASK_LOCK:
        _TASK_STATE.update(terminal_status)
        _TASK_STATE["thread"] = None
        _TASK_STATE["async_task_handle"] = None

    _TASK_STATUS.update(terminal_status)
    if not _upload_task_status(workspace, terminal_status):
        logger.error("failed to upload terminal task status for %s", task_id)

    # Telegram (add-telegram-notifications, task 2.1): terminal notification,
    # emitted even when the terminal checkpoint or the status upload failed
    # (spec: 'The terminal-status notification MUST be emitted even when
    # the terminal checkpoint fails') — checkpoint_status carries the truth.
    _telegram_notify("task-terminal", {
        "task_id": task_id,
        "state": state,
        "checkpoint_status": checkpoint_status,
        "duration_s": duration_s,
        "exit_code": exit_code,
        "error": error,
    }, workspace)

    logger.info(
        "task %s finished: state=%s harness=%s duration=%ss",
        task_id, state, harness, duration_s,
    )


def _task_info_field() -> dict:
    """Build the 'task' field for the info action (task 5.8: harness field
    present both when running and in the idle/terminal surface)."""
    with _TASK_LOCK:
        state = _TASK_STATE.get("state")
        if state == "running":
            running = {
                "task_id": _TASK_STATE.get("task_id"),
                "state": "running",
                "prompt": _TASK_STATE.get("prompt"),
                "harness": _TASK_STATE.get("harness"),
                "started_utc": _TASK_STATE.get("started_utc"),
                "heartbeat_utc": _TASK_STATE.get("heartbeat_utc"),
            }
            if _TASK_STATE.get("model"):
                # Live observability of the requested model (spec:
                # headless-task-execution, "Info live durante il task");
                # absent when no model was requested.
                running["model"] = _TASK_STATE.get("model")
            return running
        # Idle: surface the last terminal record for observability (task 5.8).
        return {
            "state": state if state and state != "idle" else (_TASK_STATUS.get("state") or "none"),
            "harness": _TASK_STATE.get("harness") or _TASK_STATUS.get("harness"),
            "finished_utc": _TASK_STATE.get("finished_utc") or _TASK_STATUS.get("finished_utc"),
            "exit_code": _TASK_STATE.get("exit_code"),
            "duration_s": _TASK_STATE.get("duration_s"),
        }


@contextlib.contextmanager
def _suppress_close_errors():
    """Best-effort cleanup helper for the websocket tunnel handler below:
    swallow errors from an already-broken connection during shutdown. A
    plain sync context manager works fine wrapping `await` statements — the
    `with` block itself is synchronous, only its body runs inside the
    enclosing `async def`."""
    try:
        yield
    except Exception:  # noqa: BLE001
        pass


# --- Remote UI tunnel: server-side session registry (design D2/D3) -------------
# Unlike the retired canale-shell approach (where the remote PTY/process
# survived a physical WebSocket disconnect on its own), a
# `@app.websocket` handler invocation IS the connection: when the socket
# drops, the coroutine exits. To still deliver "transparent reconnection...
# without loss" (spec remote-ui-tunnel), the target (TCP connection or
# child process) and the FramedPeer's offset/retain-buffer state live in a
# `_TunnelSession` keyed by a client-generated `tunnel_id`, independent of
# any single websocket attachment. A physical reconnect with the same
# `tunnel_id` re-attaches to the SAME session (same target, same peer state)
# instead of restarting it. Sessions with no attached websocket for
# `TUNNEL_ORPHAN_TIMEOUT_S` are torn down by the sweep loop below (mirrors
# the orphan timeout of the retired PTY helper, design D4).
_TUNNEL_SESSIONS_LOCK = asyncio.Lock()
_TUNNEL_SESSIONS: dict = {}
# A filesystem sync session is a single writer for the workspace. The lease is
# independent from CommandShell/task activity, which may still mutate the
# worktree while one client replica is connected.
_FS_WORKSPACE_LEASES = FsSyncLeaseRegistry()
_TUNNEL_PENDING_OUT_CAP = 4 * 1024 * 1024  # 4MB; orphan timeout is the real backstop
# Send-side flow-control window: max un-acked bytes the reader_loop will get
# ahead of the client before pausing. MUST stay below framing RETAIN_BYTES
# (256KB) so every un-acked byte remains resendable after a reconnect.
_TUNNEL_SEND_WINDOW = 128 * 1024  # 128KB (< RETAIN_BYTES 256KB)
# WebSocket frame-rate pacing: AgentCore closes a connection that exceeds 250
# frames/sec (bedrock-agentcore-limits.html, non-adjustable). We pace every
# outgoing WebSocket message (one frame each) to stay comfortably under it —
# exceeding it was the cause of the 1006 reconnect storm on large transfers
# (live verification). A small burst is allowed so tiny responses aren't
# delayed; sustained rate is capped.
_TUNNEL_SEND_RATE = 200.0   # frames/sec (80% of the 250 hard limit)
_TUNNEL_SEND_BURST = 20.0   # burst tokens


def _iter_ws_frames(data: bytes):
    """Yield individual framing lines (each newline-terminated) from `data`.

    Each line MUST be sent as its own WebSocket message: several concatenated
    framing lines (produced by a single large `reader.read()` or a buffered
    reconnect gap) can exceed AgentCore's 64KB per-WebSocket-frame limit, and
    an oversized frame is silently dropped end-to-end — which stalled every
    response larger than ~48KB (e.g. `opencode serve`'s /doc, or the TUI's
    initial state load, causing `sch attach` to hang on a blank screen).
    Found in live verification (design.md D3 "Post-live-verification fix"). A
    single framing line is <64KB by construction (MAX_CHUNK base64 ~= 60KB)."""
    start = 0
    n = len(data)
    while start < n:
        nl = data.find(b"\n", start)
        if nl == -1:
            yield data[start:]  # defensive: framing lines are always \n-terminated
            break
        yield data[start:nl + 1]
        start = nl + 1


class _TunnelSession:
    """Server-side state for one logical `sch attach`/`sch acp` session,
    surviving across physical WebSocket reconnects."""

    def __init__(self, tunnel_id, mode, reader, writer, proc=None, stderr_log=None, fs_workspace=None):
        self.tunnel_id = tunnel_id
        self.mode = mode
        self.reader = reader
        self.writer = writer
        self.proc = proc
        self.stderr_log = stderr_log
        self.fs_workspace = fs_workspace
        self.peer = FramedPeer(on_data=self._on_data, on_close=self._on_close)
        self.current_ws = None
        self.pending_out = bytearray()
        self.last_detached = time.monotonic()
        self.reader_task = None
        self.closed = False
        self._terminate_task = None
        self._rl_tokens = _TUNNEL_SEND_BURST
        self._rl_last = time.monotonic()

    async def _rate_limit(self) -> None:
        """Token-bucket pacing so we never exceed the 250 frames/sec-per-
        connection WebSocket limit (which AgentCore enforces by closing the
        connection -> 1006 reconnect storm). Call once per outgoing frame."""
        now = time.monotonic()
        self._rl_tokens = min(
            _TUNNEL_SEND_BURST, self._rl_tokens + (now - self._rl_last) * _TUNNEL_SEND_RATE
        )
        self._rl_last = now
        if self._rl_tokens < 1.0:
            await asyncio.sleep((1.0 - self._rl_tokens) / _TUNNEL_SEND_RATE)
            self._rl_tokens = 0.0
        else:
            self._rl_tokens -= 1.0

    def _on_data(self, chunk: bytes) -> None:
        try:
            self.writer.write(chunk)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tunnel[%s]: write to target failed: %s", self.tunnel_id, exc)

    def _on_close(self) -> None:
        raise CloseRequested()

    async def send_or_buffer(self, data: bytes) -> None:
        """Send framed bytes to the currently-attached websocket, or buffer
        them (bounded) if none is attached — the client's own gap-detection
        (framing.js/tunnel_framing's offset check) covers re-sync on the
        next attach, this buffer just avoids needing a resend round-trip for
        the common short-gap case."""
        if self.current_ws is not None:
            try:
                for frame in _iter_ws_frames(data):
                    await self._rate_limit()
                    await self.current_ws.send_text(frame.decode("ascii"))
                return
            except Exception:  # noqa: BLE001
                self.current_ws = None
                self.last_detached = time.monotonic()
        self.pending_out += data
        overflow = len(self.pending_out) - _TUNNEL_PENDING_OUT_CAP
        if overflow > 0:
            del self.pending_out[:overflow]

    async def reader_loop(self) -> None:
        """Runs for the lifetime of the session, independent of websocket
        attachment: keeps reading from the target and framing/buffering
        output even during a gap between reconnects."""
        try:
            while True:
                # Send-side flow control (design D3, live-verification fix #3):
                # do NOT read ahead beyond what the client has acked + the
                # retain window. Without this, a fast source (opencode serve
                # streaming a large response) races out_offset far past the
                # client's inOffset while the WebSocket is momentarily down or
                # slow; the 256KB retain buffer then trims un-acked bytes, so a
                # post-reconnect resend can no longer be satisfied -> permanent
                # offset desync -> a 1006 close/reconnect STORM that stalled any
                # transfer over ~450KB (the `sch attach` TUI's initial state
                # load) on a blank screen. Bounding unacked <= the window keeps
                # every resendable byte inside the retain buffer, and paces the
                # send to the client's ack rate (no giant reconnect-flush burst
                # that would just re-trip the frame-rate limit).
                while (not self.closed) and len(self.peer.out_retain.buf) >= _TUNNEL_SEND_WINDOW:
                    await asyncio.sleep(0.02)
                if self.closed:
                    break
                chunk = await self.reader.read(65536)
                if not chunk:
                    break
                await self.send_or_buffer(self.peer.send_data(chunk))
        except Exception as exc:  # noqa: BLE001
            logger.info("tunnel[%s]: target reader ended: %s", self.tunnel_id, exc)
        finally:
            # Task 6.5 (spec remote-ui-tunnel): propagate the remote
            # process's real exit code when available, so `sch acp`
            # terminates with it instead of always 0/1. Sent as a plain
            # JSON text message (distinguishable from framing.js lines,
            # which never start with '{') BEFORE the framed close, so the
            # client can parse it before tearing the stream down.
            if self.proc is not None:
                try:
                    exit_code = await asyncio.wait_for(self.proc.wait(), timeout=5)
                except Exception:  # noqa: BLE001
                    exit_code = None
                await self.send_or_buffer(
                    json.dumps({"type": "exit", "code": exit_code}).encode("ascii") + b"\n"
                )
            await self.send_or_buffer(self.peer.send_close())
            await self.terminate()

    async def terminate(self) -> None:
        if self._terminate_task is None:
            self._terminate_task = asyncio.create_task(self._terminate())
        await asyncio.shield(self._terminate_task)

    async def _terminate(self) -> None:
        self.closed = True
        try:
            if self.proc is not None:
                try:
                    self.proc.terminate()
                    await asyncio.wait_for(self.proc.wait(), timeout=5)
                except Exception:  # noqa: BLE001
                    with contextlib.suppress(Exception):
                        self.proc.kill()
                with contextlib.suppress(Exception):
                    if self.stderr_log is not None:
                        self.stderr_log.close()
            else:
                with contextlib.suppress(Exception):
                    self.writer.close()
        finally:
            async with _TUNNEL_SESSIONS_LOCK:
                _TUNNEL_SESSIONS.pop(self.tunnel_id, None)
                if self.fs_workspace:
                    _FS_WORKSPACE_LEASES.release(self.fs_workspace, self.tunnel_id)
            logger.info("tunnel[%s]: session terminated (mode=%s)", self.tunnel_id, self.mode)


async def _tunnel_orphan_sweep_loop() -> None:
    """Background sweep (design D4): terminate sessions with no attached
    websocket for longer than TUNNEL_ORPHAN_TIMEOUT_S. Started once, lazily,
    on the first tunnel connection (mirrors _ensure_serve_supervisor_started)."""
    while True:
        await asyncio.sleep(30)
        async with _TUNNEL_SESSIONS_LOCK:
            stale = [
                s
                for s in _TUNNEL_SESSIONS.values()
                if s.current_ws is None
                and time.monotonic() - s.last_detached >= TUNNEL_ORPHAN_TIMEOUT_S
            ]
        for s in stale:
            logger.warning("tunnel[%s]: orphan timeout reached; terminating", s.tunnel_id)
            await s.terminate()


_TUNNEL_SWEEP_STARTED = {"value": False}


def _ensure_tunnel_sweep_started() -> None:
    if not _TUNNEL_SWEEP_STARTED["value"]:
        _TUNNEL_SWEEP_STARTED["value"] = True
        asyncio.create_task(_tunnel_orphan_sweep_loop())


async def _open_tunnel_target(mode: str, first: dict):
    """Create the target for a brand-new tunnel session. Returns
    (reader, writer, proc_or_none, stderr_log_or_none)."""
    if mode in ("exec", "fs"):
        ready = await asyncio.to_thread(
            _WORKSPACE_READY.wait, FS_WORKSPACE_READY_TIMEOUT_S
        )
        if not ready or not REPO_DIR.is_dir():
            phase = BOOT_STATE.get("phase", "unknown")
            raise RuntimeError(
                "workspace bootstrap did not complete before tunnel target "
                f"(phase={phase})"
            )
    if mode == "tcp":
        port = first.get("port")
        if not isinstance(port, int) or not (0 < port < 65536):
            raise ValueError("mode='tcp' requires an integer 'port' (1-65535)")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return reader, writer, None, None
    if mode == "fs":
        # sch-acp-editor-integration (design D4): dedicated file-sync
        # channel for `sch acp` file-locality. Same subprocess plumbing as
        # mode='exec', but the argv is FIXED server-side (never
        # client-controlled) and rooted at the workspace worktree. The
        # worker speaks the fs-sync JSON-line protocol documented in
        # its module docstring (peer: tunnel/mirror.js).
        argv = [
            sys.executable,
            str(Path(__file__).parent / "fs_sync_worker.py"),
            "--root",
            str(REPO_DIR),
        ]
    else:
        argv = first.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError("mode='exec' requires a non-empty list of non-empty strings 'argv'")
    stderr_log = open(f"/tmp/sch-tunnel-ws-{uuid.uuid4()}.stderr.log", "wb")  # noqa: SIM115
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(REPO_DIR),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_log,
        # add-user-provider-keys (design D7): mode='exec' targets are ACP
        # harness agents (`opencode acp`, `claude-agent-acp`) spawned by this
        # shim, so they follow the same child-environment discipline as the
        # headless/serve paths — including popping a key the user removed.
        env=_child_env_with_provider_keys(),
    )
    return proc.stdout, proc.stdin, proc, stderr_log


@app.websocket
async def tunnel_websocket_handler(websocket, context):  # noqa: ARG001 — context unused, matches SDK handler signature
    """Remote end of the sch-remote-ui-tunnel byte bridge (`sch attach` /
    `sch acp`, design D2/D3, capability remote-ui-tunnel).

    Runs over `InvokeAgentRuntimeWithWebSocketStream` (design.md "Nota di
    revisione", post-D9 pivot from the interactive shell channel — no shared
    concurrency budget with `sch shell`/`sch open`/`sch run`). Replaces the
    retired PTY-based helper + `prepare-tunnel` run-once marker entirely: no
    login shell, no PTY, no marker file. The first WebSocket message
    declares the target and a client-generated `tunnel_id`; this handler (or
    a prior invocation still tracked in `_TUNNEL_SESSIONS`) pumps bytes via
    asyncio, using the same offset/ack reliability framing used before the
    pivot (`tunnel_framing.FramedPeer`) — now carried as discrete WebSocket
    text messages instead of lines on a raw-mode PTY.

    First message (JSON): {"tunnel_id": "<uuid>", "mode": "tcp", "port": <int>}
    (sch attach -> opencode serve),
    {"tunnel_id": "<uuid>", "mode": "exec", "argv": [...]} (sch acp), or
    {"tunnel_id": "<uuid>", "mode": "fs", "workspace": "<identity>"} (sch acp file-locality sync,
    sch-acp-editor-integration design D4 — spawns the fixed
    fs_sync_worker.py rooted at the worktree; a client on an image without
    this mode gets the standard {"type":"error"} rejection and degrades to
    chat-style).
    `tunnel_id` MUST be stable across reconnects of the same logical session
    (design D3's `ResilientStream`) so this handler can reattach to the
    existing `_TunnelSession` instead of restarting the target/process.

    NOT exercised against a live AgentCore runtime as of writing (no
    live-runtime access in this implementation session) — see design.md
    OQ-WS-LIVE.
    """
    from starlette.websockets import WebSocketDisconnect

    await websocket.accept()
    _ensure_tunnel_sweep_started()

    try:
        first = await websocket.receive_json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("tunnel: invalid or missing initial message: %s", exc)
        with _suppress_close_errors():
            await websocket.close(code=1002)
        return

    if not isinstance(first, dict):
        with _suppress_close_errors():
            await websocket.close(code=1002)
        return

    if not _set_storage_backend(first.get("storage")):
        with _suppress_close_errors():
            await websocket.send_json({"type": "error", "message": "storage backend mismatch"})
            await websocket.close(code=1008)
        return
    session_epoch = first.get("session_epoch", 0)
    if not _set_session_epoch(session_epoch):
        with _suppress_close_errors():
            await websocket.send_json({"type": "error", "message": "invalid or mismatched session_epoch"})
            await websocket.close(code=1008)
        return
    _set_workspace_name(first.get("workspace"))

    tunnel_id = (first.get("tunnel_id") or "").strip()
    if not tunnel_id:
        with _suppress_close_errors():
            await websocket.send_json({"type": "error", "message": "'tunnel_id' is required"})
            await websocket.close(code=1002)
        return

    async with _TUNNEL_SESSIONS_LOCK:
        session = _TUNNEL_SESSIONS.get(tunnel_id)

    if session is None:
        mode = (first.get("mode") or "").strip().lower()
        if mode not in ("tcp", "exec", "fs"):
            with _suppress_close_errors():
                await websocket.send_json({"type": "error", "message": "'mode' must be 'tcp', 'exec' or 'fs'"})
                await websocket.close(code=1002)
            return
        if mode == "tcp" and _resolve_harness() != "opencode":
            with _suppress_close_errors():
                await websocket.send_json({
                    "type": "error",
                    "message": (
                        f"workspace '{_resolve_workspace_name()}' is bound to harness="
                        f"'{_resolve_harness()}'; mode='tcp' requires opencode"
                    ),
                })
                await websocket.close(code=1008)
            return
        fs_workspace = None
        if mode == "fs":
            # `sch task --sync` can be the first request that starts a fresh
            # microVM, before any action payload has established this value.
            _set_workspace_name(first.get("workspace"))
            if not _set_storage_backend(first.get("storage")):
                with _suppress_close_errors():
                    await websocket.send_json({
                        "type": "error",
                        "message": "mode='fs' storage backend mismatch",
                    })
                    await websocket.close(code=1008)
                return
            fs_workspace = _resolve_workspace_name()
            if not fs_workspace:
                with _suppress_close_errors():
                    await websocket.send_json({"type": "error", "message": "mode='fs' requires a resolved workspace identity"})
                    await websocket.close(code=1008)
                return
            async with _TUNNEL_SESSIONS_LOCK:
                if not _FS_WORKSPACE_LEASES.acquire(fs_workspace, tunnel_id):
                    with _suppress_close_errors():
                        await websocket.send_json({
                            "type": "error",
                            "message": f"workspace '{fs_workspace}' already has an active fs-sync lease",
                            "workspace": fs_workspace,
                        })
                        await websocket.close(code=1008)
                    return
        try:
            reader, writer, proc, stderr_log = await _open_tunnel_target(mode, first)
        except Exception as exc:  # noqa: BLE001
            if fs_workspace:
                async with _TUNNEL_SESSIONS_LOCK:
                    _FS_WORKSPACE_LEASES.release(fs_workspace, tunnel_id)
            logger.warning("tunnel[%s]: failed to open target (mode=%s): %s", tunnel_id, mode, exc)
            with _suppress_close_errors():
                await websocket.send_json({"type": "error", "message": str(exc)})
                await websocket.close(code=1011)
            return
        session = _TunnelSession(tunnel_id, mode, reader, writer, proc, stderr_log, fs_workspace)
        async with _TUNNEL_SESSIONS_LOCK:
            _TUNNEL_SESSIONS[tunnel_id] = session
        session.reader_task = asyncio.create_task(session.reader_loop())
        logger.info("tunnel[%s]: new session opened (mode=%s)", tunnel_id, mode)
    else:
        logger.info("tunnel[%s]: reattaching existing session (mode=%s)", tunnel_id, session.mode)

    session.current_ws = websocket
    if session.pending_out:
        with _suppress_close_errors():
            for frame in _iter_ws_frames(bytes(session.pending_out)):
                await session._rate_limit()
                await websocket.send_text(frame.decode("ascii"))
        session.pending_out.clear()

    async def pump_ws_to_target() -> str:
        """Returns 'closed' (application-level C frame received — the
        session should be torn down) or 'disconnected' (network-level; the
        session is kept alive, bounded by the orphan sweep, for a future
        reconnect with the same tunnel_id)."""
        try:
            while True:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=TUNNEL_ORPHAN_TIMEOUT_S)
                try:
                    reply = session.peer.feed(raw.encode("ascii", errors="ignore"))
                except CloseRequested:
                    return "closed"
                if reply:
                    await session.send_or_buffer(reply)
        except (WebSocketDisconnect, TimeoutError):
            return "disconnected"
        except Exception as exc:  # noqa: BLE001
            logger.warning("tunnel[%s]: ws pump error: %s", tunnel_id, exc)
            return "disconnected"

    async def heartbeat_loop() -> None:
        try:
            while session.current_ws is websocket:
                await asyncio.sleep(5)
                hb = session.peer.maybe_heartbeat()
                if hb:
                    await session.send_or_buffer(hb)
        except Exception:  # noqa: BLE001
            pass

    ws_task = asyncio.create_task(pump_ws_to_target())
    hb_task = asyncio.create_task(heartbeat_loop())
    try:
        done, pending = await asyncio.wait([ws_task, hb_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        outcome = "disconnected"
        if ws_task in done:
            with contextlib.suppress(Exception):
                outcome = ws_task.result()
    finally:
        if session.current_ws is websocket:
            session.current_ws = None
            session.last_detached = time.monotonic()
        if outcome == "closed":
            await session.terminate()
            with _suppress_close_errors():
                await websocket.send_json({"type": "closed", "tunnel_id": tunnel_id})
        with _suppress_close_errors():
            await websocket.close()
        logger.info(
            "tunnel[%s]: websocket detached (%s)",
            tunnel_id, "session terminated" if session.closed else "session kept alive for reconnect",
        )


@app.entrypoint
def invoke(payload, context=None):
    """Handle ``noop``/``info``/``checkpoint`` invocations (sync, single JSON
    response)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"action": payload}
    if not isinstance(payload, dict):
        payload = {}

    action = (payload.get("action") or payload.get("prompt") or "info").strip().lower()
    logger.info("invocation action=%s", action)

    storage_backend = (payload.get("storage_backend") or "").strip().lower()
    session_epoch = payload.get("session_epoch", 0)
    if not _set_session_epoch(session_epoch):
        return {"status": "rejected", "action": action, "error": "invalid or mismatched session_epoch"}
    # Storage hint from the caller (sch knows whether a workspace is new).
    storage_hint = (payload.get("storage") or "").strip().lower()
    if storage_hint in ("fresh", "resumed") and _STORAGE_HINT["value"] is None:
        _STORAGE_HINT["value"] = storage_hint
        logger.info("storage hint received: %s", storage_hint)
    selected_backend = storage_backend or (
        "session" if storage_hint in ("fresh", "resumed") else ""
    )

    # Workspace identity (design D2): explicit payload hint takes priority;
    # else derive from the runtimeSessionId prefix (sch-<ws>-<uuid>) when the
    # invocation context exposes it.
    workspace_hint = (payload.get("workspace") or "").strip()
    if workspace_hint:
        _set_workspace_name(workspace_hint)
    elif _WORKSPACE_NAME["value"] is None and context is not None:
        derived = _derive_workspace_from_session_id(getattr(context, "session_id", None))
        if derived:
            _set_workspace_name(derived)
            logger.info("workspace identity derived from session id fallback: %s", derived)

    # Multi-harness (design D1/D9, task 5.6): propagate the workspace's
    # harness from the `sch` payload into the in-memory state and the mount
    # marker. A fresh microVM (idle timeout, MaxLifetime, version update)
    # reads it back from the marker / L2 manifest without re-specification.
    # The marker write happens below in the per-action handlers (so a noop
    # warm-up persists it before the first task/shell).
    harness_hint = (payload.get("harness") or "").strip().lower() or None
    if harness_hint:
        _set_harness(harness_hint)
    if context is not None:
        sid = getattr(context, "session_id", None)
        if sid:
            _SESSION_ID["value"] = sid

    # Per-user provider keys (add-user-provider-keys, design D1/D3): every
    # payload built by `sch` carries the caller's full key set, so staging here
    # — before the storage backend is published (the barrier the bootstrap
    # thread waits on) — guarantees the keys are on tmpfs before init-workspace
    # runs its claude reconciliation and before any harness process is spawned.
    # Only NAMES are logged, never values.
    staged_keys = _stage_provider_keys(payload)
    if staged_keys:
        logger.info("provider keys staged for this session: %s", ", ".join(staged_keys))

    # Opt-in GitHub remote access (TASK-26): reconcile the repo's origin and
    # credential helper with the staged set on every invocation. No-op unless
    # the workspace is git-native; never fatal and never logs values — drift
    # degrades to the credential-less default.
    _reconcile_github_access()

    # Publish the backend last: the bootstrap thread uses this as the barrier
    # that workspace, harness, session id and epoch are all initialized.
    if selected_backend and not _set_storage_backend(selected_backend):
        return {
            "status": "rejected",
            "action": action,
            "error": (
                f"storage backend mismatch: active='{_resolve_storage_backend()}' "
                f"requested='{selected_backend}'"
            ),
        }
    if not storage_backend and selected_backend == "session":
        logger.info("legacy payload detected; selected session storage backend")

    if _resolve_workspace_name() and CHECKPOINT_BUCKET:
        try:
            _assert_writer_claim(_resolve_workspace_name())
        except WriterFenceError as exc:
            return {
                "status": "rejected", "action": action,
                "error": str(exc),
            }

    if action == "checkpoint":
        # Synchronous, forced checkpoint (db -> mount, artefacts -> S3,
        # manifest always updated) — used by `sch stop` right before
        # StopRuntimeSession so no data from the last interval is lost.
        result = _do_checkpoint(force=True)
        db_ok = result.get("db_backup") in ("ok", "no-local-db")
        l2_ok = result.get("status") == "ok" and result.get("manifest_written") is True
        return {
            "status": "ok" if (db_ok and l2_ok) else "error",
            "action": "checkpoint",
            "result": result,
        }

    if action == "mark-interactive":
        global _INTERACTIVE_ACTIVE  # noqa: PLW0603
        _INTERACTIVE_ACTIVE = bool(payload.get("active"))
        return {
            "status": "ok",
            "action": "mark-interactive",
            "active": _INTERACTIVE_ACTIVE,
        }

    if action == "command-shell-presence":
        try:
            result = _COMMAND_SHELL_PRESENCE.update(
                payload.get("shellId"),
                payload.get("attachmentId"),
                payload.get("state"),
                payload.get("ttl_s"),
            )
        except ValueError as exc:
            return {
                "status": "rejected",
                "action": "command-shell-presence",
                "error": str(exc),
            }
        return {
            "status": "ok",
            "action": "command-shell-presence",
            "shellId": payload["shellId"],
            "attachmentId": payload["attachmentId"],
            **result,
        }

    if action == "prepare-run":
        # `sch run <ws>`: arm the run-once autostart marker consumed by
        # /etc/profile.d/sch-run.sh in the next interactive login shell
        # (agentcore exec --it cannot pass a startup command to the PTY).
        # Same second-line-of-defense as the task action: a payload harness
        # divergent from the workspace's bound harness is rejected here even
        # though bin/sch already rejects it client-side.
        payload_harness = (payload.get("harness") or "").strip().lower() or None
        marker_harness = _resolve_harness()
        if payload_harness and payload_harness != marker_harness:
            return {
                "status": "rejected",
                "action": "prepare-run",
                "error": (
                    f"harness mismatch: payload='{payload_harness}' but workspace "
                    f"'{_resolve_workspace_name()}' is bound to harness='{marker_harness}'"
                ),
            }
        model_present = "model" in payload
        model = payload.get("model")
        if model_present and (
            not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model)
        ):
            return {
                "status": "rejected",
                "action": "prepare-run",
                "error": "invalid model: expected non-empty [A-Za-z0-9._:/-]+",
            }
        marker = {
            "harness": marker_harness,
            "workspace": _resolve_workspace_name(),
        }
        response = {"status": "ok", "action": "prepare-run", "harness": marker_harness}
        if model_present:
            marker["model"] = model
            response["model"] = model
        if bool(payload.get("continue")):
            # `sch run --continue`: resume the harness's latest session in the
            # TUI. The id is resolved HERE (same resolvers as `task --continue`)
            # so the shell script stays dumb argv plumbing; a miss degrades to
            # a fresh TUI instead of failing (headless-task-execution R2).
            #
            # The lookup runs only once the workspace is ready (TASK-29,
            # decision-8): on a cold boot right after `sch stop` the session
            # store (opencode.db, Claude JSONL, Pi session files) is restored
            # asynchronously, and resolving before that landed would arm a
            # fresh TUI while the pre-stop session sits in S3. The wait is
            # bounded like the task worker's; the operator is waiting for the
            # TUI anyway (the dispatcher would gate on the same readiness),
            # so the pause only moves earlier. A workspace that is still not
            # ready after the bound is an explicit error rather than a fresh
            # TUI: the operator asked to resume, and "fresh" here is exactly
            # the silent degrade this gate exists to prevent.
            not_ready = _wait_workspace_ready_or_error(
                "prepare-run", timeout_s=PREPARE_RUN_READY_TIMEOUT_S
            )
            if not_ready:
                not_ready["error"] = (
                    "cannot resolve the session to resume: {}; the workspace is "
                    "still restoring after {}s - retry `sch run --continue` once "
                    "it is ready".format(not_ready["error"], PREPARE_RUN_READY_TIMEOUT_S)
                )
                logger.warning(
                    "prepare-run: continue requested but workspace not ready after %ss; "
                    "refusing to arm a fresh TUI",
                    PREPARE_RUN_READY_TIMEOUT_S,
                )
                return not_ready
            session_id = _resolve_latest_harness_session(marker_harness)
            marker["continue"] = True
            response["continue"] = True
            if session_id:
                marker["session_id"] = session_id
                response["session_id"] = session_id
            else:
                logger.info(
                    "prepare-run: continue requested but no %s session found; arming fresh",
                    marker_harness,
                )
        # The autostart discards markers older than its TTL (300 s); stamp the
        # epoch after the readiness wait above so a slow cold boot never eats
        # into the window the login shell has to consume the marker.
        marker["epoch"] = time.time()
        try:
            RUN_ONCE_MARKER.write_text(json.dumps(marker))
        except OSError as exc:
            logger.warning("prepare-run: cannot write run-once marker: %s", exc)
            return {"status": "error", "action": "prepare-run", "error": str(exc)}
        logger.info(
            "prepare-run: armed run-once autostart (harness=%s, model=%s, continue=%s)",
            marker_harness,
            model or "default",
            bool(marker.get("continue")),
        )
        return response

    if action == "serve-ensure":
        # Ensure the shared API + web UI backend is running for this workspace.
        # Fast, non-blocking status probe + lazy
        # supervisor bootstrap — the wait-for-workspace-ready discipline
        # lives in the supervisor loop (see _serve_supervisor_loop), not in
        # this synchronous handler, so a cold workspace does not hang the
        # invocation for minutes. A short bounded poll below gives an
        # immediate "ok" for the common warm-workspace case.
        if _resolve_harness() != "opencode":
            return {
                "status": "rejected",
                "action": "serve-ensure",
                "error": (
                    f"workspace '{_resolve_workspace_name()}' is bound to harness="
                    f"'{_resolve_harness()}'; opencode serve requires harness=opencode "
                    "(no client/server split exists for claude or pi)"
                ),
            }
        _ensure_serve_supervisor_started()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not _serve_is_alive():
            time.sleep(0.5)
        return {
            "status": "ok" if _serve_is_alive() else "starting",
            "action": "serve-ensure",
            "port": _SERVE_STATE.get("port") or OPENCODE_SERVE_PORT,
            "opencode_version": _opencode_version(),
            "workspace_ready": READY_MARKER.exists() and REPO_DIR.exists(),
            "capabilities": {"web": True},
        }

    if action == "task":
        return _handle_task_action(payload)

    if action == "git-seed":
        return _handle_git_seed(payload)

    if action == "git-snapshot":
        return _handle_git_snapshot(payload)

    if action == "session-import":
        return _handle_session_import(payload)

    if action not in ("noop", "info", "ping", "warmup"):
        return {
            "status": "error",
            "message": (
                f"unknown action '{action}' (supported: noop, info, checkpoint, task, "
                "mark-interactive, command-shell-presence, prepare-run, serve-ensure, "
                "git-seed, git-snapshot, session-import)"
            ),
        }

    response = {
        "status": "ok",
        "action": action,
        "image_version": os.environ.get("SCH_IMAGE_VERSION", "unknown"),
        "opencode_version_pinned": os.environ.get("OPENCODE_VERSION", "unknown"),
        "claude_code_version_pinned": os.environ.get("CLAUDE_CODE_VERSION", "unknown"),
        "pi_version_pinned": os.environ.get("PI_VERSION", "unknown"),
        "harness": _resolve_harness(),
        "storage": _resolve_storage_backend(),
        "boot": dict(BOOT_STATE),
    }
    if action in ("info",):
        response["opencode_version_installed"] = _opencode_version()
        response["claude_version_installed"] = _claude_version()
        response["pi_version_installed"] = _pi_version()
        response["workspace"] = _workspace_info()
        response["db"] = {
            "local": str(OPENCODE_DB_LOCAL),
            "local_exists": OPENCODE_DB_LOCAL.exists(),
            "backup": str(DB_BACKUP_PATH),
            "backup_exists": DB_BACKUP_PATH.exists(),
            "backup_interval_s": DB_BACKUP_INTERVAL,
        }
        response["claude"] = {
            "config_dir_local": str(CLAUDE_CONFIG_DIR_LOCAL),
            "ready_marker_exists": CLAUDE_READY_MARKER.exists(),
            "mcp_file_path": str(CLAUDE_MCP_FILE),
            "mcp_file_exists": CLAUDE_MCP_FILE.is_file(),
            "state_replica": str(CLAUDE_STATE_REPLICA),
            "state_replica_exists": CLAUDE_STATE_REPLICA.exists(),
        }
        # add-pi-harness (task 4.6): the pi counterpart — readiness, the seed
        # canary, the role prompt the headless argv points at, and the L2 replica.
        response["pi"] = {
            "config_dir_local": str(PI_CONFIG_DIR_LOCAL),
            "ready_marker_exists": PI_READY_MARKER.exists(),
            "settings_path": str(PI_SETTINGS_FILE),
            "settings_exists": PI_SETTINGS_FILE.is_file(),
            "role_remote_auto_exists": PI_ROLE_REMOTE_AUTO.is_file(),
            "extension_exists": (PI_CONFIG_DIR_LOCAL / "extensions" / "sch-pi.ts").is_file(),
            "state_replica": str(PI_STATE_REPLICA),
            "state_replica_exists": PI_STATE_REPLICA.exists(),
        }
        response["checkpoint"] = {
            "enabled": _checkpoint_enabled(),
            "bucket": CHECKPOINT_BUCKET or None,
            "interval_s": CHECKPOINT_INTERVAL,
            "workspace": _resolve_workspace_name(),
            "harness": _resolve_harness(),
            "storage": _resolve_storage_backend(),
            "workspace_root": str(WORKSPACE_ROOT),
            "last_result": CHECKPOINT_STATE.get("last_result"),
            "last_attempt_utc": CHECKPOINT_STATE.get("last_attempt_utc"),
            "last_success_utc": CHECKPOINT_STATE.get("last_success_utc"),
        }
        response["task"] = _task_info_field()
        # add-user-provider-keys: NAMES only, never values (spec:
        # "Segretezza delle chiavi" — diagnostics may name variables and, at
        # most, short suffixes). Useful to confirm end-to-end that the keys the
        # user configured actually reached this session.
        response["provider_keys"] = {
            "staged": sorted(_read_staged_provider_keys()),
            "staging_file": str(PROVIDER_KEYS_FILE),
        }
        response["task_status"] = dict(_TASK_STATUS)
        response["interactive_busy"] = {
            "active": _BUSY_STATE["handle"] is not None,
            "since_epoch": _BUSY_STATE["since"],
            "sessions": list(_BUSY_STATE["sessions"]),
            "capped": _BUSY_STATE["capped"],
            "last_release_reason": _BUSY_STATE["last_release_reason"],
            "stale_after_s": BUSY_STALE_SECONDS,
            "max_hold_s": BUSY_MAX_HOLD_SECONDS,
        }
        presence = _COMMAND_SHELL_PRESENCE.snapshot()
        response["command_shell_presence"] = {
            "state": presence["state"],
            "lease_count": len(presence["leases"]),
            "expires_at": presence["expires_at"],
        }
        response["tunnel"] = {
            "serve_supervisor_started": _SERVE_STATE.get("supervisor_started"),
            "serve_alive": _serve_is_alive(),
            "serve_port": _SERVE_STATE.get("port"),
            "serve_started_utc": _SERVE_STATE.get("started_utc"),
            "serve_restart_count": _SERVE_STATE.get("restart_count"),
        }
    return response


# Start the boot sequence in the background: /ping must be served immediately
# while the asynchronous session-storage restore settles.
_remove_telegram_enabled_marker()
threading.Thread(
    target=_command_shell_presence_expiry_loop,
    name="sch-command-shell-presence",
    daemon=True,
).start()
threading.Thread(target=_bootstrap, name="sch-bootstrap", daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("AGENT_PORT", 8080))
    logger.info(
        "SCH shim starting on :%s (image=%s, opencode=%s, claude_code=%s, checkpoint_bucket=%s, checkpoint_interval=%s)",
        port,
        os.environ.get("SCH_IMAGE_VERSION", "unknown"),
        os.environ.get("OPENCODE_VERSION", "unknown"),
        os.environ.get("CLAUDE_CODE_VERSION", "unknown"),
        CHECKPOINT_BUCKET or "unset",
        CHECKPOINT_INTERVAL,
    )
    app.run(host="0.0.0.0", port=port)
