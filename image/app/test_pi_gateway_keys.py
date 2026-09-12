"""Container-free tests for the pi gateway provider keys
(extend-pi-gateway-keys, TASK-18).

Three concerns:

* :class:`GeneratorMappingTests` — the build-time models.dev -> pi
  models.json mapping (image/scripts/gen-pi-gateway-models.py), run offline
  against a local fixture: env-var references only, models.dev fields mapped
  onto pi's schema, unknown modalities dropped, defaults filled.
* :class:`PiGatewayReconciliationTests` — init-workspace.sh merges the
  SCH-owned gateway blocks into pi's models.json only for keys staged in the
  session, withdraws registrations a previous session made, preserves
  operator-owned blocks, and writes no file at all for a keyless session.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
GEN = ROOT / "scripts" / "gen-pi-gateway-models.py"
SCRIPT = ROOT / "scripts" / "init-workspace.sh"

# A minimal models.dev catalog slice: one model per interesting mapping case.
FIXTURE = {
    "opencode": {
        "name": "OpenCode Zen",
        "api": "https://opencode.ai/zen/v1",
        "models": {
            "zen-large": {
                "name": "Zen Large",
                "reasoning": True,
                "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                "limit": {"context": 1000000, "output": 128000},
                "cost": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 6.25},
            },
            "zen-free": {
                # No name, no modalities, no limits, no cost: pi defaults.
                "reasoning": False,
            },
        },
    },
    "kilo": {
        "name": "Kilo Gateway",
        "api": "https://api.kilo.ai/api/gateway",
        "models": {
            "kilo/hy": {"tool_call": True, "limit": {"context": 262144}},
        },
    },
}


def _fixture_file(tmp: Path) -> Path:
    path = tmp / "catalog.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return path


def _generate(source: Path) -> dict:
    result = subprocess.run(
        ["python3", str(GEN), "--source", str(source)],
        text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def _run_generator(source: str, output: Path | None = None):
    argv = ["python3", str(GEN), "--source", source]
    if output is not None:
        argv += ["--output", str(output)]
    return subprocess.run(argv, text=True, capture_output=True)


def _load_generator_module():
    spec = importlib.util.spec_from_file_location("sch_gen_pi_gateway", GEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GeneratorMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_provider_blocks_use_env_references(self):
        doc = _generate(_fixture_file(Path(self.tmp.name)))
        self.assertEqual(set(doc["providers"]), {"opencode", "kilo"})
        self.assertEqual(
            doc["providers"]["opencode"]["apiKey"], "$OPENCODE_API_KEY")
        self.assertEqual(doc["providers"]["kilo"]["apiKey"], "$KILO_API_KEY")
        self.assertEqual(
            doc["providers"]["opencode"]["baseUrl"], "https://opencode.ai/zen/v1")
        for provider in doc["providers"].values():
            self.assertEqual(provider["api"], "openai-completions")

    def test_model_fields_are_mapped(self):
        doc = _generate(_fixture_file(Path(self.tmp.name)))
        large = doc["providers"]["opencode"]["models"][1]  # sorted: zen-free, zen-large
        self.assertEqual(large["id"], "zen-large")
        self.assertEqual(large["name"], "Zen Large")
        self.assertTrue(large["reasoning"])
        # "pdf" has no pi representation and is dropped, not forwarded.
        self.assertEqual(large["input"], ["text", "image"])
        self.assertEqual(large["contextWindow"], 1000000)
        self.assertEqual(large["maxTokens"], 128000)
        self.assertEqual(
            large["cost"],
            {"input": 5.0, "output": 25.0, "cacheRead": 0.5, "cacheWrite": 6.25},
        )

    def test_missing_fields_fall_back_to_pi_defaults(self):
        doc = _generate(_fixture_file(Path(self.tmp.name)))
        free = doc["providers"]["opencode"]["models"][0]
        self.assertEqual(free["id"], "zen-free")
        self.assertNotIn("name", free)
        self.assertFalse(free["reasoning"])
        self.assertEqual(free["input"], ["text"])
        self.assertNotIn("contextWindow", free)
        self.assertNotIn("maxTokens", free)
        self.assertEqual(free["cost"]["input"], 0.0)

    def test_output_is_deterministic(self):
        source = _fixture_file(Path(self.tmp.name))
        self.assertEqual(_generate(source), _generate(source))

    def test_unknown_catalog_provider_fails_loudly(self):
        broken = Path(self.tmp.name) / "broken.json"
        broken.write_text(json.dumps({"opencode": FIXTURE["opencode"]}), encoding="utf-8")
        with self.assertRaises(subprocess.CalledProcessError):
            _generate(broken)


class GeneratorFailurePolicyTests(unittest.TestCase):
    """Exit codes are the contract with the image build: an unreachable source
    degrades (the build continues without the catalog), an unusable catalog
    fails the build."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_explicit_user_agent_is_sent(self):
        # models.dev answers HTTP 403 to Python-urllib's default UA — the real
        # cause of the first failed image build of this capability.
        module = _load_generator_module()
        request = module.build_request("https://models.dev/api.json")
        self.assertTrue(request.get_header("User-agent"))
        self.assertNotIn("urllib", request.get_header("User-agent").lower())

    def test_unreachable_source_exits_2_and_writes_nothing(self):
        output = Path(self.tmp.name) / "out.json"
        result = _run_generator(str(Path(self.tmp.name) / "absent.json"), output)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(output.exists())
        self.assertIn("source unreachable", result.stderr)

    def test_unusable_catalog_exits_4(self):
        broken = Path(self.tmp.name) / "broken.json"
        broken.write_text(json.dumps({"opencode": {"api": "https://x", "models": {}}}))
        result = _run_generator(str(broken))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("catalog unusable", result.stderr)

    def test_non_json_source_exits_4(self):
        garbage = Path(self.tmp.name) / "garbage.json"
        garbage.write_text("not json at all")
        self.assertEqual(_run_generator(str(garbage)).returncode, 4)

    def test_verify_rejects_a_document_without_env_references(self):
        module = _load_generator_module()
        document = module.build_providers(FIXTURE)
        module.verify(document)  # the generated shape is valid
        document["providers"]["opencode"]["apiKey"] = "sk-literal-value"
        with self.assertRaises(module.CatalogUnusable):
            module.verify(document)

    def test_degrade_flag_turns_an_unreachable_source_into_exit_zero(self):
        output = Path(self.tmp.name) / "out.json"
        result = subprocess.run(
            ["python3", str(GEN), "--source", str(Path(self.tmp.name) / "absent.json"),
             "--output", str(output), "--degrade-on-unreachable"],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(output.exists())
        self.assertIn("continuing without a gateway catalog", result.stderr)

    def test_degrade_flag_still_fails_on_an_unusable_catalog(self):
        broken = Path(self.tmp.name) / "broken.json"
        broken.write_text(json.dumps({"opencode": {"api": "https://x", "models": {}}}))
        result = subprocess.run(
            ["python3", str(GEN), "--source", str(broken), "--degrade-on-unreachable"],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 4, result.stderr)

    def test_degrade_removes_a_stale_catalog_from_a_previous_run(self):
        output = Path(self.tmp.name) / "out.json"
        output.write_text('{"providers": {"opencode": {"stale": true}}}')
        subprocess.run(
            ["python3", str(GEN), "--source", str(Path(self.tmp.name) / "absent.json"),
             "--output", str(output), "--degrade-on-unreachable"],
            text=True, capture_output=True, check=True,
        )
        self.assertFalse(output.exists())


class DockerfileBuildStepTests(unittest.TestCase):
    """Structural guard on the build step.

    `RUN` does no Dockerfile variable substitution, so a `\\$var` written for
    the shell arrives as a literal `$var` and every comparison using it fails
    silently — that mistake failed a real image build. The step must therefore
    contain no `$` at all and delegate the failure policy to the generator.
    """

    def _run_body(self):
        lines = (ROOT / "Dockerfile").read_text().splitlines()
        start = next(
            n for n, line in enumerate(lines)
            if line.startswith("RUN python3.11 /tmp/gen-pi-gateway-models.py")
        )
        body = [lines[start][len("RUN"):]]
        while body[-1].rstrip().endswith("\\"):
            body[-1] = body[-1].rstrip()[:-1]
            start += 1
            body.append(lines[start])
        return " ".join(part.strip() for part in body)

    def test_step_contains_no_dollar_sign(self):
        self.assertNotIn("$", self._run_body())

    def test_step_delegates_the_degradation_policy_to_the_generator(self):
        self.assertIn("--degrade-on-unreachable", self._run_body())


class PiGatewayReconciliationTests(unittest.TestCase):
    KEY_OC = "oc-key-value-should-never-land-in-a-file"
    KEY_KILO = "kilo-key-value-should-never-land-in-a-file"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.pi_dir = root / "pi-agent"
        self.template_dir = root / "pi-templates"
        self.template_dir.mkdir(parents=True)
        # Minimal but faithful template dir: the settings template is the real
        # one; the gateway catalog is a small fixture this test controls.
        shutil_copy = (ROOT / "pi-templates" / "settings.json").read_text()
        (self.template_dir / "settings.json").write_text(shutil_copy)
        self.gateway_file = self.template_dir / "gateway-models.json"
        self.gateway_file.write_text(json.dumps({
            "providers": {
                "opencode": {
                    "name": "OpenCode Zen",
                    "baseUrl": "https://opencode.ai/zen/v1",
                    "api": "openai-completions",
                    "apiKey": "$OPENCODE_API_KEY",
                    "models": [{"id": "zen-large", "contextWindow": 1000000}],
                },
                "kilo": {
                    "name": "Kilo Gateway",
                    "baseUrl": "https://api.kilo.ai/api/gateway",
                    "api": "openai-completions",
                    "apiKey": "$KILO_API_KEY",
                    "models": [{"id": "kilo/hy", "contextWindow": 262144}],
                },
            }
        }, indent=2))
        self.models_file = self.pi_dir / "models.json"
        self.sidecar = self.pi_dir / "models.json.sch-gateways"

    def run_init(self, *keys):
        env = os.environ.copy()
        env.update({
            "SCH_HARNESS": "pi",
            "SCH_WORKSPACE_ROOT": str(self.workspace),
            "XDG_DATA_HOME": str(self.workspace / "state" / "data"),
            "XDG_CONFIG_HOME": str(self.workspace / "state" / "config"),
            "PI_CODING_AGENT_DIR": str(self.pi_dir),
            "SCH_PI_TEMPLATE_DIR": str(self.template_dir),
            "AWS_REGION": "eu-west-1",
            "SCH_OPENCODE_API_KEY": self.KEY_OC if "opencode" in keys else "",
            "SCH_KILO_API_KEY": self.KEY_KILO if "kilo" in keys else "",
        })
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, check=True
        )

    def test_staged_keys_are_merged_with_env_references_only(self):
        self.run_init("opencode", "kilo")
        doc = json.loads(self.models_file.read_text())
        self.assertEqual(
            doc["providers"]["opencode"],
            json.loads(self.gateway_file.read_text())["providers"]["opencode"],
        )
        self.assertEqual(doc["providers"]["kilo"]["apiKey"], "$KILO_API_KEY")
        # Values never enter the file, only "$ENV_VAR" references.
        self.assertNotIn(self.KEY_OC, self.models_file.read_text())
        self.assertNotIn(self.KEY_KILO, self.models_file.read_text())
        self.assertEqual(
            self.sidecar.read_text().split(), ["kilo", "opencode"])

    def test_merge_is_idempotent(self):
        self.run_init("opencode", "kilo")
        first = self.models_file.read_text()
        self.run_init("opencode", "kilo")
        self.assertEqual(self.models_file.read_text(), first)

    def test_partially_staged_session_merges_only_what_is_staged(self):
        self.run_init("opencode")
        doc = json.loads(self.models_file.read_text())
        self.assertIn("opencode", doc["providers"])
        self.assertNotIn("kilo", doc["providers"])

    def test_removed_key_withdraws_the_previous_registration(self):
        self.run_init("opencode", "kilo")
        self.run_init("kilo")
        doc = json.loads(self.models_file.read_text())
        self.assertNotIn("opencode", doc["providers"])
        self.assertIn("kilo", doc["providers"])
        self.assertEqual(self.sidecar.read_text().split(), ["kilo"])

    def test_keyless_session_leaves_no_trace(self):
        self.run_init()
        self.assertFalse(self.models_file.exists())
        self.assertFalse(self.sidecar.exists())

    def test_withdrawing_the_last_key_removes_both_files(self):
        self.run_init("opencode")
        self.run_init()
        self.assertFalse(self.models_file.exists())
        self.assertFalse(self.sidecar.exists())

    def test_operator_block_under_a_gateway_id_is_never_overwritten(self):
        self.pi_dir.mkdir(parents=True)
        operator_block = {
            "baseUrl": "https://operator-proxy.example.com/v1",
            "api": "openai-completions",
            "apiKey": "$OPERATOR_KEY",
            "models": [{"id": "zen-large"}],
        }
        self.models_file.write_text(json.dumps({
            "providers": {"opencode": operator_block},
        }, indent=2))
        self.run_init("opencode", "kilo")
        doc = json.loads(self.models_file.read_text())
        self.assertEqual(doc["providers"]["opencode"], operator_block)
        self.assertIn("kilo", doc["providers"])
        # Only what SCH owns is tracked; the operator block is not ours.
        self.assertEqual(self.sidecar.read_text().split(), ["kilo"])

    def test_operator_custom_providers_survive_reconciliation(self):
        self.pi_dir.mkdir(parents=True)
        self.models_file.write_text(json.dumps({
            "providers": {"my-llm": {
                "baseUrl": "http://localhost:8080/v1",
                "api": "openai-completions",
                "apiKey": "ollama",
                "models": [{"id": "llama3.1:8b"}],
            }},
        }, indent=2))
        self.run_init("opencode", "kilo")
        doc = json.loads(self.models_file.read_text())
        self.assertIn("my-llm", doc["providers"])
        self.assertIn("opencode", doc["providers"])

    def test_unparseable_models_file_is_left_untouched(self):
        self.pi_dir.mkdir(parents=True)
        original = b"{ definitely not json\n"
        self.models_file.write_bytes(original)
        self.run_init("opencode", "kilo")
        self.assertEqual(self.models_file.read_bytes(), original)

    def test_missing_catalog_with_a_staged_key_is_reported(self):
        # The image was built while models.dev was unreachable: the build
        # degrades, so the runtime must SAY the provider cannot be offered
        # instead of leaving the user hunting for it in /model.
        self.gateway_file.unlink()
        result = self.run_init("opencode")
        self.assertIn("gateway key staged", result.stdout)
        self.assertIn("WARNING", result.stdout)
        self.assertFalse(self.models_file.exists())
        # NAMES only: no key value in any log line.
        self.assertNotIn(self.KEY_OC, result.stdout + result.stderr)

    def test_missing_catalog_without_keys_is_silent(self):
        self.gateway_file.unlink()
        result = self.run_init()
        self.assertNotIn("gateway", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
