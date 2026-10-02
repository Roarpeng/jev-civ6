"""War-room server: journal API, TypeSafe gateway, live-state proxy, and UI.

Run:  python -m uvicorn server.app:app --host 127.0.0.1 --port 8080
  or: python server/app.py
"""
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .decision_gate import evaluate as gate_evaluate
from .journal import Journal
from .llm import judge as llm_judge
from .config import load as load_config
from .autopilot import AutoPilot

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Jev x Civ6 war-room", version="0.1.0")
journal = Journal()


class JevRequest(BaseModel):
    state: object = Field(..., description="State object/string sent to the judge")
    questions: dict = Field(..., description="Question id -> Question spec")
    model: str | None = None  # None -> use the configured provider default
    turn: int | None = None
    meta: dict = {}


class JevRecord(BaseModel):
    request: dict
    response: dict
    latency_ms: int | None = None
    turn: int | None = None
    meta: dict = {}


class ActionEvent(BaseModel):
    tool: str
    args: dict = {}
    result: object = None
    turn: int | None = None
    meta: dict = {}


class StateEvent(BaseModel):
    turn: int | None = None
    snapshot: object


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/jev")
def jev(req: JevRequest):
    """Gateway: perform the TypeSafe call, journal it, return the answers."""
    try:
        resp, latency_ms = llm_judge(req.state, req.questions, req.model or None)
        error = None
    except Exception as e:  # noqa: BLE001 — surface service errors to the log
        resp, latency_ms, error = None, None, str(e)
    event = {
        "request": {"state": req.state, "questions": req.questions,
                    "model": req.model or load_config().engine_summary()},
        "response": resp,
        "error": error,
        "latency_ms": latency_ms,
        "meta": req.meta,
    }
    ev = journal.add("jev", event, turn=req.turn)
    if error:
        raise HTTPException(status_code=502, detail={"error": error, "event": ev})
    return {"id": ev["id"], "ts": ev["ts"], "answers": resp.get("answers"), "response": resp}


@app.post("/api/jev/record")
def jev_record(req: JevRecord):
    """Journal an already-performed TypeSafe call (used by the CLI judge)."""
    event = {
        "request": req.request,
        "response": req.response,
        "error": None,
        "latency_ms": req.latency_ms,
        "meta": req.meta,
    }
    ev = journal.add("jev", event, turn=req.turn)
    return {"id": ev["id"], "ts": ev["ts"]}


@app.post("/api/action")
def action(ev: ActionEvent):
    out = journal.add(
        "action",
        {"tool": ev.tool, "args": ev.args, "result": ev.result, "meta": ev.meta},
        turn=ev.turn,
    )
    return out


@app.post("/api/state")
def state(ev: StateEvent):
    out = journal.add("state", {"snapshot": ev.snapshot}, turn=ev.turn)
    return out


class GateRequest(BaseModel):
    turn: int | None = None
    snapshot: object


@app.post("/api/gate")
def gate(ev: GateRequest):
    """Store the snapshot, diff it against the previous one, and return the
    decision-gate verdict: which triggers fired and which Jev questions (if
    any) are worth asking. No TypeSafe call happens here."""
    prev = journal.latest("state")
    prev_snapshot = (prev or {}).get("data", {}).get("snapshot") if prev else None
    result = gate_evaluate(ev.snapshot, prev_snapshot)
    journal.add("state", {"snapshot": ev.snapshot}, turn=ev.turn)
    journal.add(
        "gate",
        {"result": {k: result[k] for k in ("should_ask", "triggers", "question_ids", "skip_reason")}},
        turn=ev.turn,
    )
    return result


@app.get("/api/gate/latest")
def gate_latest():
    return journal.latest("gate")


# ── control modes (auto = Jev-driven autopilot, manual = player) ────────

class ControlState:
    mode: str = "manual"


control = ControlState()
pilot = AutoPilot(journal, gate_evaluate, llm_judge)
pilot.on_pause = lambda: setattr(control, "mode", "manual")


class ModeRequest(BaseModel):
    mode: str  # "auto" | "manual"


async def _set_mode(mode: str):
    if mode not in ("auto", "manual"):
        raise HTTPException(status_code=422, detail="mode must be auto|manual")
    if mode == "auto":
        was_auto = control.mode == "auto"
        resuming = (pilot.status_public().get("step") == "auto_paused"
                    or not was_auto)
        control.mode = "auto"
        if not pilot.task or pilot.task.done():
            pilot.start()
            journal.add("action", {
                "tool": "control_mode", "args": {"mode": "auto"},
                "result": ("autopilot resumed after pause — gate+Jev drive the game"
                           if resuming and was_auto else
                           "autopilot started — Jev drives the game via gate"),
            }, turn=pilot.status_public().get("turn"))
        return {"mode": control.mode, **pilot.status_public()}
    # manual
    if control.mode == "auto" or pilot.status_public().get("running"):
        await pilot.stop()
        control.mode = "manual"
        journal.add("action", {
            "tool": "control_mode", "args": {"mode": "manual"},
            "result": "player in control — Jev decisions stopped",
        }, turn=pilot.status_public().get("turn"))
    else:
        control.mode = "manual"
    return {"mode": control.mode, **pilot.status_public()}


@app.get("/api/mode")
async def get_mode():
    return {"mode": control.mode, **pilot.status_public()}


@app.post("/api/mode")
async def post_mode(req: ModeRequest):
    return await _set_mode(req.mode)


@app.get("/api/events")
def events(types: str = "jev,action,state", limit: int = 80, before_id: int | None = None):
    return journal.events(
        types=[t.strip() for t in types.split(",") if t.strip()],
        limit=min(max(limit, 1), 500),
        before_id=before_id,
    )


@app.get("/api/state/latest")
def state_latest():
    return journal.latest("state")


@app.get("/api/stats")
def stats():
    return journal.stats()


@app.get("/api/config")
def config_info():
    """Non-secret config summary for the UI / debugging."""
    cfg = load_config()
    return {"source": cfg.source, "engine": cfg.engine_summary(),
            "provider": cfg.llm.provider, "bridge_url": cfg.bridge.url,
            "llm": {
                "provider": cfg.llm.provider,
                "model": cfg.llm.model,
                "base_url": cfg.llm.base_url,
                "api_key_env": cfg.llm.api_key_env,
                "api_key_set": bool(cfg.llm.api_key or
                                    (cfg.llm.api_key_env and
                                     os.environ.get(cfg.llm.api_key_env))),
                "timeout_s": cfg.llm.timeout_s,
            },
            "bridge": {"game_host": cfg.bridge.game_host,
                       "game_port": cfg.bridge.game_port}}


class ConfigUpdate(BaseModel):
    provider: str | None = None     # typesafe | openai | anthropic | mock
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None      # inline; stored toml-side, shown never
    api_key_env: str | None = None
    timeout_s: float | None = None


def _write_llm_toml(cfg, upd: ConfigUpdate) -> Path:
    """Persist the [llm] section of jevciv6.toml, keeping other sections.

    The judge re-reads config on every call, so the file write IS the
    hot-swap: the next judgment uses the new backend, no restart needed.
    """
    path = Path(cfg.source) if cfg.source else Path("jevciv6.toml")
    if path.name != "jevciv6.toml":  # custom config path — respect it
        path = Path(os.environ.get("JEVCIV6_CONFIG", "jevciv6.toml"))
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    keep: list[str] = []
    in_llm = False
    for ln in lines:
        if ln.strip().startswith("["):
            in_llm = ln.strip() == "[llm]"
        if not in_llm:
            keep.append(ln)
    fields = {
        "provider": upd.provider if upd.provider is not None else cfg.llm.provider,
        "model": upd.model if upd.model is not None else cfg.llm.model,
        "base_url": upd.base_url if upd.base_url is not None else cfg.llm.base_url,
        "api_key": upd.api_key if upd.api_key is not None else cfg.llm.api_key,
        "api_key_env": upd.api_key_env if upd.api_key_env is not None
                       else cfg.llm.api_key_env,
        "timeout_s": upd.timeout_s if upd.timeout_s is not None
                     else cfg.llm.timeout_s,
    }
    block = ["[llm]"]
    for k, v in fields.items():
        if v in ("", None):
            continue
        if isinstance(v, float):
            block.append(f"{k} = {v}")
        else:
            block.append(f'{k} = "{v}"')
    text = "\n".join(x for x in keep if x.strip()) + "\n\n" + "\n".join(block) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


@app.post("/api/config")
def config_update(upd: ConfigUpdate):
    """Hot-swap the judge backend. Persists to jevciv6.toml; the next
    judgment call picks it up (config is re-read per call)."""
    cfg = load_config()
    if upd.provider and upd.provider not in ("typesafe", "openai",
                                             "anthropic", "mock"):
        raise HTTPException(400, f"unknown provider: {upd.provider}")
    if upd.provider and upd.provider != cfg.llm.provider and upd.model is None:
        upd.model = ""   # provider switch without an explicit model → reset
    path = _write_llm_toml(cfg, upd)
    new = load_config()
    journal.add("action", {
        "tool": "config_update",
        "args": {k: v for k, v in upd.model_dump().items() if v is not None
                 and k != "api_key"},
        "result": f"judge backend hot-swapped -> {new.engine_summary()}",
    })
    return {"ok": True, "source": str(path),
            "engine": new.engine_summary(),
            "llm": {"provider": new.llm.provider, "model": new.llm.model,
                    "base_url": new.llm.base_url,
                    "api_key_env": new.llm.api_key_env,
                    "api_key_set": bool(new.llm.api_key or
                                        (new.llm.api_key_env and
                                         os.environ.get(new.llm.api_key_env)))}}


@app.post("/api/config/test")
def config_test():
    """One tiny judge round-trip to verify the configured backend works."""
    cfg = load_config()
    try:
        resp, latency_ms = llm_judge(
            {"ping": True},
            {"ping": {"type": "choice", "instructions": "Reply with ok.",
                      "criteria": {"ok": "confirm", "no": "deny"}}},
        )
        answers = (resp or {}).get("answers", {}) if isinstance(resp, dict) else resp
        return {"ok": True, "engine": cfg.engine_summary(),
                "latency_ms": latency_ms, "sample": answers}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "engine": cfg.engine_summary(), "error": str(e)[:300]}


@app.get("/api/live")
async def live():
    """Game liveness: the autopilot's own FireTuner link, else a direct TCP
    probe of the game's tuner port. Does NOT depend on the civ6-mcp dashboard
    process — auto-mode takeover legitimately removes it."""
    import socket

    st = pilot.status_public()
    if st.get("connected"):
        return {"live": True, "source": "autopilot"}
    try:
        s = socket.create_connection(("127.0.0.1", 4318), timeout=0.6)
        s.close()
        return {"live": True, "source": "firetuner"}
    except Exception:
        return {"live": False, "source": "none"}


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


if __name__ == "__main__":
    cfg = load_config()
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)
