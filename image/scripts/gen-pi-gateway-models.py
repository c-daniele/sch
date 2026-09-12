#!/usr/bin/env python3
"""Build-time generator for the Pi gateway models.json (TASK-18).

Generates the SCH-owned Pi provider entries for the two API-key gateways
(OpenCode Zen, Kilo Gateway) from the models.dev catalog, so `pi` can offer
them in /model exactly like opencode does — without any runtime outbound
call (the microVM must never make an unsolicited outbound call at harness
startup; the catalog is baked at image build and refreshed by rebuilds).

Output shape follows pi's ~/.pi/agent/models.json schema (packages/
coding-agent/docs/models.md): providers keyed by id, each with `baseUrl`,
`api: "openai-completions"`, `apiKey` as an ENV VAR REFERENCE — the key value
never enters this file, the dispatcher exports the canonical variable and pi
interpolates it at request time — and an explicit `models` list mapped from
models.dev.

Stdlib only. Exit codes are part of the contract with the image build:

* ``0`` — catalog generated and self-verified.
* ``2`` — the catalog SOURCE was unreachable (network, HTTP error, unreadable
  file). The build treats this as a degradation, not a failure: a third-party
  outage must not block the deploy of everything else in the image. The image
  then ships without the gateway catalog, and init-workspace.sh logs an
  explicit warning if a gateway key is staged for a session.
* ``4`` — the catalog was READ but is unusable (missing provider, no baseUrl,
  no models, failed self-verification). That is a real regression signal —
  a silently empty gateway catalog is exactly the failure class this
  capability removes — so the build fails.

models.dev rejects the default ``Python-urllib`` User-Agent with HTTP 403
(measured), which is why the request carries an explicit one.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MODELS_DEV_URL = "https://models.dev/api.json"
# An explicit UA is REQUIRED: models.dev answers 403 to Python-urllib's default.
USER_AGENT = "sch-image-build (+https://models.dev)"
FETCH_ATTEMPTS = 3
FETCH_TIMEOUT = 60

EXIT_SOURCE_UNREACHABLE = 2
EXIT_CATALOG_UNUSABLE = 4

# models.dev provider id -> (canonical pi provider id, api key env var, label)
GATEWAYS = {
    "opencode": ("opencode", "OPENCODE_API_KEY", "OpenCode Zen"),
    "kilo": ("kilo", "KILO_API_KEY", "Kilo Gateway"),
}

# Pi's `input` accepts exactly these; models.dev modalities may also carry
# "pdf" etc., which pi has no representation for.
PI_INPUT_TYPES = ("text", "image")


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def convert_model(model_id, entry):
    """One models.dev model entry -> one pi models.json model entry."""
    out = {"id": model_id}
    if entry.get("name"):
        out["name"] = entry["name"]
    out["reasoning"] = bool(entry.get("reasoning", False))
    raw_input = ((entry.get("modalities") or {}).get("input")) or ["text"]
    out["input"] = [t for t in raw_input if t in PI_INPUT_TYPES] or ["text"]
    limit = entry.get("limit") or {}
    if limit.get("context"):
        out["contextWindow"] = int(limit["context"])
    if limit.get("output"):
        out["maxTokens"] = int(limit["output"])
    cost = entry.get("cost") or {}
    out["cost"] = {
        "input": _num(cost.get("input")),
        "output": _num(cost.get("output")),
        "cacheRead": _num(cost.get("cache_read")),
        "cacheWrite": _num(cost.get("cache_write")),
    }
    return out


def build_request(url):
    """The catalog request, always carrying an explicit User-Agent."""
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


class SourceUnreachable(Exception):
    """The catalog could not be read at all (network, HTTP, filesystem)."""


class CatalogUnusable(Exception):
    """The catalog was read but does not carry what pi needs."""


def load_catalog(source):
    if "://" not in source or source.startswith("file://"):
        path = Path(source[7:] if source.startswith("file://") else source)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SourceUnreachable("cannot read {}: {}".format(path, exc))
    else:
        text = _fetch(source)
    try:
        catalog = json.loads(text)
    except ValueError as exc:
        raise CatalogUnusable("catalog is not valid JSON: {}".format(exc))
    if not isinstance(catalog, dict):
        raise CatalogUnusable("catalog is not a JSON object")
    return catalog


def _fetch(url):
    """Fetch with a few retries: a transient blip must not fail an image build
    on its first attempt (a persistent failure still degrades cleanly)."""
    last = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(
                build_request(url), timeout=FETCH_TIMEOUT
            ) as response:
                return response.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            if attempt + 1 < FETCH_ATTEMPTS:
                time.sleep(2 ** attempt)
    raise SourceUnreachable("cannot fetch {}: {}".format(url, last))


def build_providers(catalog):
    providers = {}
    for source_id, (pi_id, env_name, label) in GATEWAYS.items():
        entry = catalog.get(source_id)
        if not isinstance(entry, dict):
            raise CatalogUnusable(
                "catalog has no '{}' provider".format(source_id))
        # models.dev spells the base URL `api` (it also carries `baseUrl`
        # spellings in some providers); accept both, require an http(s) URL.
        base_url = entry.get("baseUrl") or entry.get("baseURL") or entry.get("api")
        if not (isinstance(base_url, str) and base_url.startswith("http")):
            raise CatalogUnusable(
                "provider '{}' has no usable baseUrl".format(source_id))
        models = entry.get("models")
        if not isinstance(models, dict) or not models:
            raise CatalogUnusable(
                "provider '{}' has no models".format(source_id))
        providers[pi_id] = {
            "name": entry.get("name") or label,
            "baseUrl": base_url,
            "api": "openai-completions",
            "apiKey": "${}".format(env_name),
            "models": [
                convert_model(model_id, models[model_id])
                for model_id in sorted(models)
            ],
        }
    return {"providers": providers}


def verify(document):
    """Self-check of the generated document.

    Lives here, not in a Dockerfile shell one-liner: the checks compare
    against literal ``$ENV_VAR`` strings, and a `RUN` command cannot carry
    those safely (the shell expands them — the first version of this build
    step asserted against an empty string because of exactly that).
    """
    providers = document.get("providers")
    if not isinstance(providers, dict):
        raise CatalogUnusable("generated document has no providers object")
    expected = {pi_id for pi_id, _, _ in GATEWAYS.values()}
    if set(providers) != expected:
        raise CatalogUnusable(
            "generated providers {} != expected {}".format(
                sorted(providers), sorted(expected)))
    for pi_id, env_name, _ in GATEWAYS.values():
        block = providers[pi_id]
        if block.get("apiKey") != "${}".format(env_name):
            raise CatalogUnusable(
                "provider '{}' does not reference ${}".format(pi_id, env_name))
        if block.get("api") != "openai-completions":
            raise CatalogUnusable(
                "provider '{}' has the wrong api type".format(pi_id))
        if not block.get("models") or not all(m.get("id") for m in block["models"]):
            raise CatalogUnusable(
                "provider '{}' has empty or unidentified models".format(pi_id))
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source", default=MODELS_DEV_URL,
        help="models.dev catalog URL or local JSON file (tests)")
    parser.add_argument(
        "--output", default="-",
        help="output path (default stdout)")
    parser.add_argument(
        "--degrade-on-unreachable", action="store_true",
        help=(
            "exit 0 (writing nothing) when the catalog source is unreachable. "
            "Used by the image build so a models.dev outage cannot fail it — "
            "keeping the decision here, in Python, instead of in Dockerfile "
            "shell branching, which cannot reference shell variables safely."))
    args = parser.parse_args(argv)
    try:
        document = build_providers(load_catalog(args.source))
        verify(document)
    except SourceUnreachable as exc:
        # Degradation, not failure: the caller (image build) continues without
        # the gateway catalog and the runtime reports its absence.
        print("gen-pi-gateway-models: source unreachable: {}".format(exc),
              file=sys.stderr)
        if args.output != "-":
            Path(args.output).unlink(missing_ok=True)
        if args.degrade_on_unreachable:
            print("gen-pi-gateway-models: continuing without a gateway catalog",
                  file=sys.stderr)
            return 0
        return EXIT_SOURCE_UNREACHABLE
    except CatalogUnusable as exc:
        print("gen-pi-gateway-models: catalog unusable: {}".format(exc),
              file=sys.stderr)
        return EXIT_CATALOG_UNUSABLE
    text = json.dumps(document, indent=2, sort_keys=False) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text, encoding="utf-8")
        counts = ", ".join(
            "{}={}".format(pid, len(block["models"]))
            for pid, block in sorted(document["providers"].items()))
        print("gen-pi-gateway-models: wrote {} ({})".format(args.output, counts),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
