#!/usr/bin/env pwsh
# sch.ps1 — Serverless Coding Harness client (Windows shim).
#
# This file intentionally contains no application logic: it locates a
# Python interpreter (python3, then python, then the `py` launcher) and
# delegates the entire invocation, with arguments and exit code propagated
# unchanged, to the cross-platform implementation in cli/sch/ (see
# docs/specs/access-surfaces/cli-cross-platform.md).

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$MainPy = Join-Path $RepoRoot "cli/sch/__main__.py"

function Test-InterpreterAvailable([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (Test-InterpreterAvailable "python3") {
    & python3 $MainPy @args
    exit $LASTEXITCODE
}
if (Test-InterpreterAvailable "python") {
    & python $MainPy @args
    exit $LASTEXITCODE
}
if (Test-InterpreterAvailable "py") {
    & py -3 $MainPy @args
    exit $LASTEXITCODE
}

[Console]::Error.WriteLine("sch: no Python interpreter found (tried python3, python, py -3) - install Python >= 3.8")
exit 1
