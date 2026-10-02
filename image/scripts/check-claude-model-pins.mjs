#!/usr/bin/env node
// check-claude-model-pins.mjs: fail when an image Claude model pin would make
// the pinned Claude Code show its "Newer <tier> model available" dialog.
//
// Problem (TASK-26): on a fresh workspace Claude Code compares the Opus, Sonnet
// and Haiku alias pins (ANTHROPIC_DEFAULT_<TIER>_MODEL) with the Bedrock alias
// targets of the model catalog baked into its binary. When a pin is older it
// asks to switch, and "Yes" restarts Claude Code, which ends an AgentCore
// session. A Claude Code bump can move those targets, so this check runs at
// image build time against the effective environment.
//
// Rule (established empirically on 2.1.285): the target of a tier is
// catalog.aliases.<tier>.per_provider.bedrock, falling back to
// catalog.aliases.<tier>.default. A pin older than the target fails. Newer
// models of the tier (catalog.latest_per_family) do not trigger the dialog and
// are only reported as notes. Fable and ANTHROPIC_CUSTOM_MODEL_OPTION are not
// part of the dialog and are not checked.
//
// Usage: check-claude-model-pins.mjs [--binary PATH]
//   Pins are read from the environment. PATH defaults to the npm-global
//   @anthropic-ai/claude-code binary. Exit 0 = ok, 1 = stale or unknown pin,
//   2 = usage error or catalog not found.

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import vm from "node:vm";

const TIERS = ["opus", "sonnet", "haiku"];
const ANCHOR = '"//":"Hand-maintained baked-in model catalog';

function fail(code, message) {
  process.stderr.write(`check-claude-model-pins: ${message}\n`);
  process.exit(code);
}

function defaultBinary() {
  const root = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
  return join(root, "@anthropic-ai", "claude-code", "bin", "claude.exe");
}

// Return the end index (inclusive) of the object literal opening at `start`.
function matchBrace(text, start) {
  let depth = 0;
  let quote = null;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (quote) {
      if (c === "\\") i++;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") quote = c;
    else if (c === "{") depth++;
    else if (c === "}" && --depth === 0) return i;
  }
  return -1;
}

export function extractCatalog(buffer) {
  const text = buffer.toString("latin1");
  const anchor = text.indexOf(ANCHOR);
  if (anchor < 0) return null;
  const start = text.lastIndexOf("{", anchor);
  if (start < 0 || text.slice(start + 1, anchor).trim() !== "") return null;
  const end = matchBrace(text, start);
  if (end < 0) return null;
  // latin1 keeps byte offsets; re-decode the slice as UTF-8 for the labels.
  const literal = buffer.subarray(start, end + 1).toString("utf8");
  const catalog = vm.runInNewContext(`(${literal})`, Object.create(null), { timeout: 1000 });
  if (!Array.isArray(catalog?.models) || typeof catalog?.aliases !== "object") return null;
  return catalog;
}

// "claude-opus-5-5" -> [5, 5]; "claude-3-5-haiku" -> [3, 5].
function version(id) {
  return (id.match(/\d+/g) || []).map(Number);
}

function compare(a, b) {
  const va = version(a);
  const vb = version(b);
  for (let i = 0; i < Math.max(va.length, vb.length); i++) {
    const d = (va[i] ?? 0) - (vb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

// "eu.anthropic.claude-opus-5-5" and "us.anthropic.claude-opus-5-5" both map
// to "anthropic.claude-opus-5-5": the region prefix selects a profile, not a model.
function stripRegion(id) {
  const m = /^(?:[a-z]{2,6}\.)?(anthropic\..+)$/.exec(id || "");
  return m ? m[1] : null;
}

export function checkPins(catalog, env) {
  const byId = new Map(catalog.models.map((m) => [m.id, m]));
  const results = [];
  for (const tier of TIERS) {
    const name = `ANTHROPIC_DEFAULT_${tier.toUpperCase()}_MODEL`;
    const pin = env[name];
    const alias = catalog.aliases[tier] || {};
    const targetId = alias.per_provider?.bedrock ?? alias.default;
    const target = byId.get(targetId);
    if (!pin) {
      results.push({ tier, ok: false, message: `${name} is not set` });
      continue;
    }
    if (!target) {
      results.push({ tier, ok: false, message: `catalog has no Bedrock alias target for ${tier}` });
      continue;
    }
    const key = stripRegion(pin);
    const model = catalog.models.find((m) => key && stripRegion(m.provider_ids?.bedrock) === key);
    if (!model) {
      results.push({ tier, ok: false, message: `${name}=${pin} is not a Bedrock model in the catalog; cannot verify it` });
      continue;
    }
    if (model.family !== tier) {
      results.push({ tier, ok: false, message: `${name}=${pin} is a ${model.family} model, not ${tier}` });
      continue;
    }
    if (compare(model.id, target.id) < 0) {
      results.push({
        tier, ok: false,
        message: `${name}=${pin} (${model.display_name}) is older than the Bedrock alias target ` +
          `${target.display_name} (${target.provider_ids?.bedrock}): a fresh workspace would show ` +
          `"Newer ${tier[0].toUpperCase()}${tier.slice(1)} model available" and restart Claude Code; ` +
          `pin ${target.display_name} or newer`,
      });
      continue;
    }
    const note = [];
    const latest = byId.get(catalog.latest_per_family?.[tier]);
    if (latest && compare(model.id, latest.id) < 0) {
      note.push(`newer ${latest.display_name} is known (no dialog: above the Bedrock alias target)`);
    }
    results.push({
      tier, ok: true,
      message: `${name}=${pin} (${model.display_name}) >= Bedrock alias target ${target.display_name}` +
        (note.length ? `; note: ${note.join("; ")}` : ""),
    });
  }
  return results;
}

function main(argv) {
  let binary = null;
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--binary" && i + 1 < argv.length) binary = argv[++i];
    else fail(2, `unknown argument: ${argv[i]}`);
  }
  binary = binary || defaultBinary();
  let buffer;
  try {
    buffer = readFileSync(binary);
  } catch (err) {
    fail(2, `cannot read ${binary}: ${err.message}`);
  }
  let catalog;
  try {
    catalog = extractCatalog(buffer);
  } catch (err) {
    fail(2, `model catalog in ${binary} does not evaluate: ${err.message}`);
  }
  if (!catalog) fail(2, `no model catalog found in ${binary} (anchor ${ANCHOR}); update this check for the new Claude Code layout`);
  const results = checkPins(catalog, process.env);
  for (const r of results) process.stdout.write(`${r.ok ? "ok" : "FAIL"} ${r.tier}: ${r.message}\n`);
  process.exit(results.every((r) => r.ok) ? 0 : 1);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main(process.argv.slice(2));
