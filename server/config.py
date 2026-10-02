# -*- coding: utf-8 -*-
"""jevciv6 configuration: jevciv6.toml (project root) + environment overrides.

Precedence (highest first):
  1. environment variables (JEVCIV6_* / provider-standard key vars)
  2. jevciv6.toml in the project root (or $JEVCIV6_CONFIG)
  3. built-in defaults (identical to the original hard-coded behavior)

Stdlib only. The TOML file is optional: missing file = defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib as _toml  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    _toml = None

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LLMConfig:
    """Which judgment backend to use and how to reach it."""

    provider: str = "typesafe"   # typesafe | openai | anthropic | mock
    model: str = ""              # empty -> provider default (typesafe: "jev-latest")
    base_url: str = ""           # openai: full base incl. /v1; "" -> https://api.openai.com/v1
    api_key: str = ""            # inline key; prefer api_key_env
    api_key_env: str = ""        # name of the env var that holds the key
    timeout_s: float = 60.0
    max_retries: int = 3
    temperature: float | None = None  # openai/anthropic sampling; None = provider default
    extra_headers: dict = field(default_factory=dict)


@dataclass
class BridgeConfig:
    """How to run and reach the civ6-mcp FireTuner bridge."""

    python: str = ""             # interpreter for the bridge; "" = same as the server
    module: str = "civ_mcp"
    url: str = "http://127.0.0.1:8000"
    game_host: str = "127.0.0.1"   # FireTuner host; containers use host.docker.internal
    game_port: int = 4318
    ready_timeout_s: float = 90.0
    src_path: str = ""           # PYTHONPATH entry; "" -> ./civ6-mcp/src when it exists


@dataclass
class AutopilotConfig:
    """Play-loop timing and safety valves."""

    takeover_on_start: bool = True    # kill competing python controllers before spawn (Windows)
    sentinel_interval_s: float = 1.0
    sentinel_cooldown_s: float = 6.0
    fail_limit: int = 3
    end_turn_http_timeout_s: float = 60.0   # http timeout for one end_turn call
    narration_grace_s: float = 0.5          # wait for bridge narration AFTER the turn
                                           # already advanced (fast end_turn returns
                                           # promptly, so this is only a fallback)
    turn_wait_timeout_s: float = 900.0    # max seconds to wait for one turn to advance
    stall_limit_s: float = 1800.0          # no-progress watchdog per turn
    auto_decline_deals: bool = True       # decline incoming trade deals to unblock
    timeout_blocker_after: int = 4
    autosave_load: str = ""   # e.g. 'AutoSave_0229' — loaded automatically when AUTO starts at the main menu; empty = wait for the operator        # N consecutive end_turn no-advance returns
                                           # (each ≈ fast poll cap) before proactively
                                           # dismissing popup + skipping unit moves;
                                           # ~86s at the 20s fast cap, past the point
                                           # where a normal AI turn would be done


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    autopilot: AutopilotConfig = field(default_factory=AutopilotConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    source: str = "defaults"     # where this config came from (for diagnostics)

    def engine_summary(self) -> str:
        llm = self.llm
        model = llm.model or ("jev-latest" if llm.provider == "typesafe" else "?")
        return f"{llm.provider}:{model}"


def _apply_toml(cfg: Config, data: dict) -> None:
    llm = data.get("llm") or {}
    for key in ("provider", "model", "base_url", "api_key", "api_key_env"):
        if key in llm and llm[key] is not None:
            setattr(cfg.llm, key, str(llm[key]))
    if "timeout_s" in llm:
        cfg.llm.timeout_s = float(llm["timeout_s"])
    if "max_retries" in llm:
        cfg.llm.max_retries = int(llm["max_retries"])
    if "temperature" in llm and llm["temperature"] is not None:
        cfg.llm.temperature = float(llm["temperature"])
    if isinstance(llm.get("extra_headers"), dict):
        cfg.llm.extra_headers = {str(k): str(v) for k, v in llm["extra_headers"].items()}

    br = data.get("bridge") or {}
    for key in ("python", "module", "url", "src_path"):
        if key in br and br[key] is not None:
            setattr(cfg.bridge, key, str(br[key]))
    for key in ("game_port",):
        if key in br:
            setattr(cfg.bridge, key, int(br[key]))
    if "ready_timeout_s" in br:
        cfg.bridge.ready_timeout_s = float(br["ready_timeout_s"])

    ap = data.get("autopilot") or {}
    for key in ("sentinel_interval_s", "sentinel_cooldown_s", "end_turn_http_timeout_s",
                "narration_grace_s", "turn_wait_timeout_s", "stall_limit_s"):
        if key in ap:
            setattr(cfg.autopilot, key, float(ap[key]))
    for key in ("fail_limit",):
        if key in ap:
            setattr(cfg.autopilot, key, int(ap[key]))
    for key in ("takeover_on_start", "auto_decline_deals"):
        if key in ap:
            setattr(cfg.autopilot, key, bool(ap[key]))
    if "timeout_blocker_after" in ap:
        cfg.autopilot.timeout_blocker_after = int(ap["timeout_blocker_after"])
    if "autosave_load" in ap:
        cfg.autopilot.autosave_load = str(ap["autosave_load"])

    sv = data.get("server") or {}
    if "host" in sv and sv["host"]:
        cfg.server.host = str(sv["host"])
    if "port" in sv:
        cfg.server.port = int(sv["port"])


def _apply_env(cfg: Config) -> Config:
    env = os.environ
    if env.get("JEVCIV6_LLM_PROVIDER"):
        cfg.llm.provider = env["JEVCIV6_LLM_PROVIDER"]
    if env.get("JEVCIV6_LLM_MODEL"):
        cfg.llm.model = env["JEVCIV6_LLM_MODEL"]
    if env.get("JEVCIV6_LLM_BASE_URL"):
        cfg.llm.base_url = env["JEVCIV6_LLM_BASE_URL"]
    if env.get("JEVCIV6_LLM_API_KEY"):
        cfg.llm.api_key = env["JEVCIV6_LLM_API_KEY"]
    if env.get("JEVCIV6_BRIDGE_URL"):
        cfg.bridge.url = env["JEVCIV6_BRIDGE_URL"]
    if env.get("JEVCIV6_GAME_HOST"):
        cfg.bridge.game_host = env["JEVCIV6_GAME_HOST"]
    if env.get("JEVCIV6_GAME_PORT"):
        cfg.bridge.game_port = int(env["JEVCIV6_GAME_PORT"])
    if env.get("JEVCIV6_PYTHON"):
        cfg.bridge.python = env["JEVCIV6_PYTHON"]
    if env.get("JEVCIV6_TAKEOVER") is not None:
        cfg.autopilot.takeover_on_start = env["JEVCIV6_TAKEOVER"] not in ("0", "false", "off")
    if env.get("JEVCIV6_PORT"):
        cfg.server.port = int(env["JEVCIV6_PORT"])
    return cfg


def load(path: str | Path | None = None) -> Config:
    """Load configuration from TOML + env; safe to call anytime."""
    cfg = Config()
    if path is not None:
        cfg_path: Path | None = Path(path)
    elif os.environ.get("JEVCIV6_CONFIG"):
        cfg_path = Path(os.environ["JEVCIV6_CONFIG"])
    else:
        cfg_path = ROOT / "jevciv6.toml"
    if cfg_path is not None and cfg_path.exists():
        if _toml is None:
            raise RuntimeError(
                f"config file {cfg_path} found but tomllib unavailable (need Python >= 3.11)"
            )
        data = _toml.loads(cfg_path.read_text(encoding="utf-8"))
        _apply_toml(cfg, data)
        cfg.source = str(cfg_path)
    return _apply_env(cfg)


def llm_api_key(cfg: Config) -> str:
    """Resolve the API key for the configured provider (never logged)."""
    llm = cfg.llm
    if llm.api_key:
        return llm.api_key
    names: list[str] = []
    if llm.api_key_env:
        names.append(llm.api_key_env)
    if llm.provider == "typesafe":
        names += ["TYPESAFE_API_KEY", "JEVCIV6_LLM_API_KEY"]
    elif llm.provider == "openai":
        names += ["OPENAI_API_KEY", "JEVCIV6_LLM_API_KEY"]
    elif llm.provider == "anthropic":
        names += ["ANTHROPIC_API_KEY", "JEVCIV6_LLM_API_KEY"]
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""   # typesafe provider keeps its own Windows-registry fallback


def bridge_src_path(cfg: Config) -> Path | None:
    """PYTHONPATH entry so `python -m civ_mcp` resolves without pip-installing."""
    if cfg.bridge.src_path:
        p = Path(cfg.bridge.src_path)
        return p if p.is_absolute() else (ROOT / p)
    candidate = ROOT / "civ6-mcp" / "src"
    return candidate if candidate.exists() else None
