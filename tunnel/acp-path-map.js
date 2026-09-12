// acp-path-map.js — per-field ACP v1 path translation for the `sch acp`
// bridge (sch-acp-editor-integration, design D2; capability
// acp-file-locality, requirement "Path translation in ACP messages").
//
// The agent lives in the microVM and only ever sees paths under
// REMOTE root (/mnt/workspace/repo); the editor lives on the laptop and
// only ever sees paths under the LOCAL mirror root. This module rewrites
// the path-bearing fields defined by the ACP v1 protocol
// (agentclientprotocol.com/protocol/v1) in both directions:
//
//   editor -> agent (toAgent):  <mirror>  ->  /mnt/workspace/repo
//     - session/new     params.cwd
//     - session/load    params.cwd
//     - session/prompt  params.prompt[]: content blocks of type
//                       `resource_link` (.uri) and `resource`
//                       (.resource.uri)
//
//   agent -> editor (toEditor): /mnt/workspace/repo -> <mirror>
//     - session/update  params.update: `tool_call` / `tool_call_update`
//                       (locations[].path, content[] of type `diff` (.path)
//                       and of type `content` (nested block uri)), and the
//                       single content block of `agent_message_chunk` /
//                       `agent_thought_chunk` / `user_message_chunk`
//     - session/request_permission  params.toolCall (same tool-call shape)
//     - fs/read_text_file, fs/write_text_file  params.path (mapped for
//       completeness/table symmetry — the bridge REJECTS these requests
//       per design D3, see acp.js)
//     - terminal/create params.cwd (idem — terminal capability is
//       neutralized per design D3)
//
// Deliberately NOT a blind string replace over the whole message: only the
// fields above are rewritten. Everything else passes through unchanged;
// unmapped string fields that still LOOK like a path under the other
// side's root are reported as warnings for stderr (spec scenario "Unknown
// field with suspicious remote path") without altering the message.
//
// macOS canonicalization pitfall (found in the OQ-ADAPTER-FS probe, see
// design.md): agents emit CANONICAL paths (realpath) in locations/diffs —
// on macOS /var/... becomes /private/var/.... The map therefore accepts
// BOTH the as-given and the canonical form of each root on input, and
// rewrites to a single stable form on output (the as-given local root
// toward the editor, the remote root toward the agent).

import { realpathSync } from 'node:fs';
import { normalize } from 'node:path';

const REMOTE_ROOT_DEFAULT = '/mnt/workspace/repo';

function stripTrailingSlash(p) {
  return p.length > 1 && p.endsWith('/') ? p.slice(0, -1) : p;
}

/** Translate `path` if it sits under any of `fromRoots`; returns null when
 * it does not (caller decides passthrough). Boundary-aware: /root matches
 * "/root" and "/root/...", never "/rootX". */
function translatePath(path, fromRoots, toRoot) {
  if (typeof path !== 'string' || path.length === 0) return null;
  // Temp-directory paths can contain duplicate separators (for example when
  // TMPDIR already ends in `/`). Normalize only for matching so the emitted
  // destination remains the stable root chosen by the map.
  const candidate = normalize(path);
  for (const root of fromRoots) {
    const normalizedRoot = normalize(root);
    if (candidate === normalizedRoot) return toRoot;
    if (candidate.startsWith(`${normalizedRoot}/`)) {
      return toRoot + candidate.slice(normalizedRoot.length);
    }
  }
  return null;
}

/** file:// URIs carry the same paths (Zed sends resource_link uris like
 * file:///Users/me/proj/src/a.ts). Translate the embedded path, preserving
 * the scheme; bare absolute paths in uri fields are handled too. */
function translateUri(uri, fromRoots, toRoot) {
  if (typeof uri !== 'string' || uri.length === 0) return null;
  if (uri.startsWith('file://')) {
    const path = uri.slice('file://'.length);
    const mapped = translatePath(path, fromRoots, toRoot);
    return mapped === null ? null : `file://${mapped}`;
  }
  return translatePath(uri, fromRoots, toRoot);
}

export class AcpPathMap {
  /**
   * @param {object} opts
   * @param {string} opts.localRoot  absolute path of the local mirror dir
   * @param {string} [opts.remoteRoot] defaults to /mnt/workspace/repo
   */
  constructor({ localRoot, remoteRoot = REMOTE_ROOT_DEFAULT }) {
    if (!localRoot) throw new Error('AcpPathMap requires localRoot');
    const local = stripTrailingSlash(localRoot);
    let canonical = local;
    try {
      canonical = stripTrailingSlash(realpathSync(local));
    } catch {
      /* mirror not created yet — canonical == as-given */
    }
    this.localRoot = local;
    this.localRoots = canonical === local ? [local] : [local, canonical];
    this.remoteRoot = stripTrailingSlash(remoteRoot);
    this.remoteRoots = [this.remoteRoot];
  }

  _toAgentPath(p) {
    return translatePath(p, this.localRoots, this.remoteRoot);
  }

  _toEditorPath(p) {
    return translatePath(p, this.remoteRoots, this.localRoot);
  }

  /** Rewrite one prompt/message content block in place. Returns true if
   * anything changed. Block shapes per ACP v1: {type:"resource_link", uri}
   * and {type:"resource", resource:{uri, text|blob}}. */
  _rewriteContentBlock(block, fromRoots, toRoot) {
    if (!block || typeof block !== 'object') return false;
    let changed = false;
    if (block.type === 'resource_link' && typeof block.uri === 'string') {
      const mapped = translateUri(block.uri, fromRoots, toRoot);
      if (mapped !== null) {
        block.uri = mapped;
        changed = true;
      }
    } else if (block.type === 'resource' && block.resource && typeof block.resource === 'object') {
      const mapped = translateUri(block.resource.uri, fromRoots, toRoot);
      if (mapped !== null) {
        block.resource.uri = mapped;
        changed = true;
      }
    }
    return changed;
  }

  /** Rewrite the path-bearing fields of a tool_call / tool_call_update /
   * request_permission.toolCall object (agent -> editor direction). */
  _rewriteToolCallLike(tc) {
    if (!tc || typeof tc !== 'object') return;
    if (Array.isArray(tc.locations)) {
      for (const loc of tc.locations) {
        if (loc && typeof loc === 'object') {
          const mapped = this._toEditorPath(loc.path);
          if (mapped !== null) loc.path = mapped;
        }
      }
    }
    if (Array.isArray(tc.content)) {
      for (const item of tc.content) {
        if (!item || typeof item !== 'object') continue;
        if (item.type === 'diff') {
          const mapped = this._toEditorPath(item.path);
          if (mapped !== null) item.path = mapped;
        } else if (item.type === 'content') {
          this._rewriteContentBlock(item.content, this.remoteRoots, this.localRoot);
        }
      }
    }
  }

  /** editor -> agent. Mutates and returns `msg`. */
  toAgent(msg) {
    if (!msg || typeof msg !== 'object' || !msg.method || !msg.params) return msg;
    const p = msg.params;
    switch (msg.method) {
      case 'session/new':
      case 'session/load': {
        const mapped = this._toAgentPath(p.cwd);
        if (mapped !== null) p.cwd = mapped;
        break;
      }
      case 'session/prompt': {
        if (Array.isArray(p.prompt)) {
          for (const block of p.prompt) this._rewriteContentBlock(block, this.localRoots, this.remoteRoot);
        }
        break;
      }
      default:
        break;
    }
    return msg;
  }

  /** agent -> editor. Mutates and returns `msg`. */
  toEditor(msg) {
    if (!msg || typeof msg !== 'object' || !msg.method) return msg;
    const p = msg.params;
    if (!p || typeof p !== 'object') return msg;
    switch (msg.method) {
      case 'session/update': {
        const u = p.update;
        if (!u || typeof u !== 'object') break;
        if (u.sessionUpdate === 'tool_call' || u.sessionUpdate === 'tool_call_update') {
          this._rewriteToolCallLike(u);
        } else if (
          u.sessionUpdate === 'agent_message_chunk'
          || u.sessionUpdate === 'agent_thought_chunk'
          || u.sessionUpdate === 'user_message_chunk'
        ) {
          this._rewriteContentBlock(u.content, this.remoteRoots, this.localRoot);
        }
        break;
      }
      case 'session/request_permission': {
        this._rewriteToolCallLike(p.toolCall);
        break;
      }
      case 'fs/read_text_file':
      case 'fs/write_text_file': {
        const mapped = this._toEditorPath(p.path);
        if (mapped !== null) p.path = mapped;
        break;
      }
      case 'terminal/create': {
        const mapped = this._toEditorPath(p.cwd);
        if (mapped !== null) p.cwd = mapped;
        break;
      }
      default:
        break;
    }
    return msg;
  }

  /**
   * Post-rewrite sweep for suspicious survivors (spec acp-file-locality,
   * scenario "Unknown field with suspicious remote path"): any string
   * value anywhere in the message that still starts with the root that
   * should have been left behind in this direction. Pure diagnostics —
   * never mutates. Returns [{field, value}] (value truncated for logging).
   */
  findSuspicious(msg, direction) {
    const roots = direction === 'toEditor' ? this.remoteRoots : this.localRoots;
    const out = [];
    const walk = (node, fieldPath) => {
      if (typeof node === 'string') {
        for (const root of roots) {
          if (node === root || node.startsWith(`${root}/`) || node.startsWith(`file://${root}`)) {
            out.push({ field: fieldPath, value: node.length > 200 ? `${node.slice(0, 200)}...` : node });
            return;
          }
        }
        return;
      }
      if (Array.isArray(node)) {
        node.forEach((v, i) => walk(v, `${fieldPath}[${i}]`));
        return;
      }
      if (node && typeof node === 'object') {
        for (const [k, v] of Object.entries(node)) walk(v, fieldPath ? `${fieldPath}.${k}` : k);
      }
    };
    walk(msg, '');
    return out;
  }
}

export { REMOTE_ROOT_DEFAULT, translatePath, translateUri };
