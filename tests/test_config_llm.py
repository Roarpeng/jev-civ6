# -*- coding: utf-8 -*-
"""Config loader + LLM layer (mock provider) tests."""
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import config as cfgmod  # noqa: E402
from server.config import Config, LLMConfig  # noqa: E402
from server.llm import _normalize_answers, judge  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = cfgmod.load(path="Z:/definitely/missing.toml")
        self.assertEqual(cfg.llm.provider, "typesafe")
        self.assertEqual(cfg.server.port, 8080)
        self.assertEqual(cfg.autopilot.fail_limit, 3)

    def test_toml_overrides(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "cfg.toml"
            p.write_text(
                '[llm]\nprovider = "openai"\nmodel = "gpt-4o-mini"\n'
                'base_url = "https://example.test/v1"\n'
                '[server]\nport = 9999\n'
                '[autopilot]\nturn_wait_timeout_s = 123.0\n',
                encoding="utf-8")
            cfg = cfgmod.load(path=p)
            self.assertEqual(cfg.llm.provider, "openai")
            self.assertEqual(cfg.llm.model, "gpt-4o-mini")
            self.assertEqual(cfg.server.port, 9999)
            self.assertEqual(cfg.autopilot.turn_wait_timeout_s, 123.0)

    def test_env_overrides(self):
        os.environ["JEVCIV6_LLM_PROVIDER"] = "mock"
        os.environ["JEVCIV6_PORT"] = "8123"
        try:
            cfg = cfgmod.load(path="Z:/missing.toml")
            self.assertEqual(cfg.llm.provider, "mock")
            self.assertEqual(cfg.server.port, 8123)
        finally:
            del os.environ["JEVCIV6_LLM_PROVIDER"]
            del os.environ["JEVCIV6_PORT"]

    def test_api_key_resolution(self):
        cfg = Config()
        cfg.llm.api_key_env = "MY_TEST_KEY"
        os.environ["MY_TEST_KEY"] = "secret123"
        try:
            self.assertEqual(cfgmod.llm_api_key(cfg), "secret123")
        finally:
            del os.environ["MY_TEST_KEY"]


class MockLLMTests(unittest.TestCase):
    def test_mock_provider_full_roundtrip(self):
        cfg = Config(llm=LLMConfig(provider="mock"))
        qs = {"q1": {"type": "choice", "criteria": {"a": "x", "b": "y"}},
              "q2": {"type": "noul", "criteria": {"true": "", "false": ""}}}
        resp, lat = judge({"turn": 1}, qs, cfg=cfg)
        self.assertEqual(resp["answers"]["q1"]["choice"], "a")
        self.assertEqual(resp["answers"]["q2"]["noul"], 0.5)
        self.assertIn("usage", resp)

    def test_normalize_choice_fallback_uses_top_probability(self):
        qs = {"q1": {"type": "choice", "criteria": {"a": "x", "b": "y"}}}
        raw = {"answers": {"q1": {"choice": "zzz",
                                  "probabilities": {"a": 0.2, "b": 0.8}}}}
        out = _normalize_answers(qs, raw)
        self.assertEqual(out["answers"]["q1"]["choice"], "b")

    def test_normalize_all_invalid_raises(self):
        qs = {"q1": {"type": "choice", "criteria": {"a": "x"}}}
        raw = {"answers": {"q1": {"choice": "zzz"}}}
        with self.assertRaises(ValueError):
            _normalize_answers(qs, raw)

    def test_normalize_noul_clamped(self):
        qs = {"q1": {"type": "noul", "criteria": {"true": "", "false": ""}}}
        out = _normalize_answers(qs, {"answers": {"q1": {"noul": 1.7}}})
        self.assertEqual(out["answers"]["q1"]["noul"], 1.0)


if __name__ == "__main__":
    unittest.main()
