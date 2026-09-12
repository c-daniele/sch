#!/usr/bin/env bash
# bin/verify-docs.sh — Documentation verification suite
# Checks link and anchor resolution, spec template conformance, frontmatter,
# account IDs and personal paths, and language rules.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

python3 - << 'EOF'
import glob
import os
import re
import subprocess
import sys
import urllib.parse

errors = []
warnings = []


def tracked_files(suffixes=None):
    """Every file git tracks (a stray local file is not a documentation defect).

    Falls back to a filesystem walk when not inside a git work tree
    (e.g. the exported public snapshot, which ships without .git).
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout
        files = [p for p in out.decode("utf-8").split("\0") if p]
        if suffixes:
            files = [p for p in files if p.endswith(suffixes)]
        return sorted(files)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    files = []
    for root, dirs, filenames in os.walk("."):
        if ".git" in dirs:
            dirs.remove(".git")
        for fn in filenames:
            p = os.path.join(root, fn)
            if p.startswith("./"):
                p = p[2:]
            files.append(p)
    if suffixes:
        files = [p for p in files if p.endswith(suffixes)]
    return sorted(files)


ROOT_DOCS = ["README.md", "MANIFESTO.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md",
             "SUPPORT.md", "CHANGELOG.md", "CODE_OF_CONDUCT.md"]

print("== 1. Verifying relative link resolution ==")
link_sources = ROOT_DOCS + [".backlog/masterplan/MASTERPLAN.md"] + glob.glob("docs/**/*.md", recursive=True)

# Backlog runtime dirs that are legitimately absent in a fresh clone when the
# board, journal, or brainstorming area is empty: git does not track empty
# directories, so a checkout never creates them until the tooling does.
ALLOWED_MISSING_DIRS = {
    ".backlog/tasks",
    ".backlog/docs",
    ".backlog/docs/journal",
    ".backlog/brainstorming",
}

link_pattern = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

for src in sorted(link_sources):
    if not os.path.isfile(src):
        continue
    with open(src, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Strip code blocks
    content_no_code = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
    
    src_dir = os.path.dirname(src)
    for match in link_pattern.finditer(content_no_code):
        text, target = match.groups()
        target = target.strip()
        
        # Skip external URLs, mailto, anchor-only
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        
        # Strip anchor from target
        target_path = target.split("#")[0]
        if not target_path:
            continue
        
        # Unquote URL-encoded chars (e.g. %20)
        target_path = urllib.parse.unquote(target_path)
        resolved = os.path.normpath(os.path.join(src_dir, target_path))

        if not os.path.exists(resolved):
            if resolved in ALLOWED_MISSING_DIRS:
                continue
            errors.append(f"Broken relative link in {src}: [{text}]({target}) -> {resolved}")

print(f"Checked {len(link_sources)} files for relative links.")

print("\n== 1b. Verifying heading anchors (#fragment) in guides and root documents ==")
# GitHub's slug: strip inline code markers, lowercase, drop punctuation except
# '-', '_' and spaces, spaces -> '-', duplicate headings get -1, -2, ...
def heading_anchors(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    seen, anchors = {}, set()
    for line in text.splitlines():
        m = re.match(r'^#{1,6}\s+(.*)$', line)
        if not m:
            continue
        h = re.sub(r'`([^`]*)`', r'\1', m.group(1)).strip().lower()
        h = re.sub(r'[^\w\- ]', '', h).replace(' ', '-')
        n = seen.get(h, 0)
        seen[h] = n + 1
        anchors.add(h if n == 0 else f"{h}-{n}")
    return anchors

anchor_cache = {}
anchor_link_pattern = re.compile(r'\[([^\]]+)\]\(([^)#\s]*)#([^)\s]+)\)')
checked_anchors = 0
for src in sorted(link_sources):
    if not os.path.isfile(src):
        continue
    with open(src, "r", encoding="utf-8") as f:
        content_no_code = re.sub(r'```.*?```', '', f.read(), flags=re.DOTALL)
    src_dir = os.path.dirname(src)
    for match in anchor_link_pattern.finditer(content_no_code):
        text, target_path, fragment = match.groups()
        if target_path.startswith(("http://", "https://", "mailto:")):
            continue
        target = src if not target_path else os.path.normpath(os.path.join(src_dir, urllib.parse.unquote(target_path)))
        if not os.path.isfile(target) or not target.endswith(".md"):
            continue  # missing files are reported by check 1; non-markdown targets have no headings
        if target not in anchor_cache:
            anchor_cache[target] = heading_anchors(target)
        checked_anchors += 1
        if fragment.lower() not in anchor_cache[target]:
            errors.append(f"Broken anchor in {src}: [{text}]({target_path}#{fragment}) -> no heading '#{fragment}' in {target}")

print(f"Checked {checked_anchors} anchored links.")

print("\n== 2. Verifying specification structure & Status lines ==")
specs = sorted(glob.glob("docs/specs/**/*.md", recursive=True))
valid_status_prefixes = ("Implemented", "Partially verified", "Proposed")
required_sections = ["Purpose", "Scope", "Requirements", "Behavior", "Invariants", "Cross-references"]

for spec in specs:
    if spec.endswith("README.md"):
        continue
    with open(spec, "r", encoding="utf-8") as f:
        content = f.read()
    
    lines = content.splitlines()
    status_match = None
    for line in lines[:10]:
        if "Status:" in line:
            status_match = line
            break
    
    if not status_match:
        errors.append(f"Spec {spec} is missing a 'Status:' line near the top.")
    else:
        # Check valid status prefix
        status_text = status_match.split("Status:")[1].strip()
        if not any(status_text.startswith(prefix) for prefix in valid_status_prefixes):
            errors.append(f"Spec {spec} has invalid Status '{status_text}'. Must start with one of: {valid_status_prefixes}")
    
    # Check required sections
    for sec in required_sections:
        if not re.search(rf"^##\s+{sec}", content, re.MULTILINE):
            errors.append(f"Spec {spec} is missing required section '## {sec}'")

print(f"Checked {len(specs)} specs for structure.")

print("\n== 3. Verifying Backlog docs and decisions frontmatter ==")
backlog_files = glob.glob(".backlog/docs/**/*.md", recursive=True) + glob.glob(".backlog/decisions/*.md")

for bf in sorted(backlog_files):
    if not os.path.isfile(bf):
        continue
    with open(bf, "r", encoding="utf-8") as f:
        content = f.read()
    
    lines = content.splitlines()
    if len(lines) < 4 or lines[0] != "---":
        errors.append(f"Backlog file {bf} is missing YAML frontmatter '---'")
        continue
    
    # Find closing ---
    closing_idx = -1
    for i in range(1, min(len(lines), 30)):
        if lines[i] == "---":
            closing_idx = i
            break
    if closing_idx == -1:
        errors.append(f"Backlog file {bf} has unclosed YAML frontmatter")
        continue
    
    fm = "\n".join(lines[1:closing_idx])
    if "id:" not in fm:
        errors.append(f"Backlog file {bf} frontmatter is missing 'id:'")
    if "title:" not in fm:
        errors.append(f"Backlog file {bf} frontmatter is missing 'title:'")

print(f"Checked {len(backlog_files)} Backlog doc/decision files for CLI frontmatter.")

print("\n== 4. Checking for AWS account IDs (12-digit numbers) ==")
# Every tracked markdown file: root documents, docs/, and the whole .backlog/
# tree (tasks and brainstorming included -- live-verification notes are where
# real account IDs have leaked before).
doc_files = tracked_files((".md",))

# 12-digit numbers not part of a longer hex/hash/version string or timestamp (e.g., 20260828132324 is 14 digits)
# UUID tails (…-111111111111) are excluded by treating '-' as a hex neighbour.
account_id_pattern = re.compile(r'(?<![0-9a-fA-F-])([0-9]{12})(?![0-9a-fA-F-])')
# Placeholders accepted in examples: the AWS documentation sample IDs and the
# literal placeholders used throughout the docs.
allowed_id_markers = ("123456789012", "111122223333", "444455556666", "<account-id>",
                      "<account_id>", "<account>", "<ACCOUNT_ID>")

for df in sorted(doc_files):
    if not os.path.isfile(df):
        continue
    with open(df, "r", encoding="utf-8") as f:
        content = f.read()
    
    for line_no, line in enumerate(content.splitlines(), start=1):
        if any(marker in line for marker in allowed_id_markers):
            continue
        for m in account_id_pattern.finditer(line):
            val = m.group(1)
            errors.append(f"Possible 12-digit AWS account ID '{val}' in {df}:{line_no}")

print(f"Checked {len(doc_files)} doc files for account IDs.")

print("\n== 4b. Checking tracked source files for AWS account IDs and personal paths ==")
# Non-markdown tracked text files (code, scripts, templates, configs). Same
# pattern; test fixtures use the AWS documentation sample IDs.
source_suffixes = (".py", ".sh", ".js", ".yaml", ".yml", ".json", ".toml", ".ps1", ".txt", ".example", "Dockerfile")
source_files = [p for p in tracked_files(source_suffixes) if "/node_modules/" not in p and not p.endswith("package-lock.json")]
# Generic user names are fine (`sch` is the in-image user); anything else under
# /Users or /home is somebody's laptop.
personal_path_pattern = re.compile(r'/Users/(?!(?:me|you|op|user|username|<)\b)[A-Za-z0-9._-]+|/home/(?!(?:sch|me|you|op|user|username|ec2-user|ubuntu|<)\b)[A-Za-z0-9._-]+')
for sf in source_files:
    try:
        with open(sf, "r", encoding="utf-8") as f:
            content = f.read()
    except (UnicodeDecodeError, OSError):
        continue
    for line_no, line in enumerate(content.splitlines(), start=1):
        if not any(marker in line for marker in allowed_id_markers):
            for m in account_id_pattern.finditer(line):
                # `touch -t YYYYMMDDhhmm` stamps are 12 digits too; they always follow `-t`.
                if re.search(r'-t\s+' + re.escape(m.group(1)), line):
                    continue
                errors.append(f"Possible 12-digit AWS account ID '{m.group(1)}' in {sf}:{line_no}")
        for m in personal_path_pattern.finditer(line):
            errors.append(f"Possible personal home path '{m.group(0)}' in {sf}:{line_no}")

print(f"Checked {len(source_files)} tracked source files for account IDs and personal paths.")

print("\n== 5. Language check (Italian word heuristic) ==")
# Words unambiguous in Italian (excluding 'a', 'in', 'per', 'no', 'on', 'me' which are English)
italian_pattern = re.compile(
    r'\b(della|dello|delle|degli|dei|dal|dallo|dalla|dai|dagli|dalle|'
    r'nel|nello|nella|nei|negli|nelle|sul|sullo|sulla|sui|sugli|sulle|'
    r'questo|questa|questi|queste|quello|quella|quelli|quelle|'
    r'sono|siamo|siete|essere|avere|perch[eé]|quando|anche|abbiamo|'
    r'fatto|problema|lezione|disinstallare)\b',
    re.IGNORECASE
)

for df in sorted(doc_files):
    if not os.path.isfile(df):
        continue
    with open(df, "r", encoding="utf-8") as f:
        content = f.read()
    
    for line_no, line in enumerate(content.splitlines(), start=1):
        for m in italian_pattern.finditer(line):
            errors.append(f"Italian word heuristic '{m.group(1)}' matched in {df}:{line_no}")

print(f"Checked {len(doc_files)} doc files for Italian language.")

print("\n" + "=" * 50)
if errors:
    print(f"FAILED with {len(errors)} error(s):")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("ALL DOCUMENTATION CHECKS PASSED.")
    sys.exit(0)
EOF
