# Jev × Civilization VI · 战争议事厅

把 **civ6-mcp**（通过 FireTuner 控制文明6）与 **TypeSafe Jev**（System One 判断模型）组合成一套
可观测的自动化游戏工作流：代码负责收集状态与执行动作，Jev 负责每回合的语义判断，
本项目的 WebUI 负责把每一次判断、每一次操作、每一份国势快照记录成可回溯的战争编年史。

```text
                 ┌──────────────────────────────────────────────┐
                 │            War-Room :8080 (本项目)            │
                 │  · 决策闸门（纯代码，零 API 成本）              │
                 │  · Jev 网关（TypeSafe API，密钥仅服务端持有）    │
                 │  · SQLite 编年史 + WebUI（自动/手动开关）       │
                 │  · AutoPilot：HTTP 桥客户端，零 socket 管理     │
                 └───────┬──────────────────────────▲───────────┘
            HTTP 读+写   │                          │ 定向提问/typed 答案
                 ┌───────▼──────────────┐   ┌───────┴────────┐
                 │ civ6-mcp 桥进程 :8000 │   │  TypeSafe Jev  │
                 │ (autopilot 的子进程)   │   │  api.typesafe  │
                 │ · 独占 FireTuner:4318 │   └────────────────┘
                 │ · PopupWatcher 关弹窗 │
                 │ · 崩溃自动重连        │
                 └──────────────────────┘
```

## 桥接架构（关键设计）

文明6 的 FireTuner 链接是**单客户端**的，且对重连很挑剔：优雅关闭（FIN）后游戏端会
长时间滞留 CLOSE_WAIT 继续占槽，导致新连接被立即重置。因此本项目的自动驾驶**绝不
自己持有游戏链接**，而是 spawn 一个 `civ6-mcp` 子进程作为桥：

- 桥独占 FireTuner 链接——它继承上游久经考验的连接管理（持久链接、带内自动重连、
  PopupWatcher 自动关闭"研究完成"等模态弹窗）
- 我们给上游 `web_api.py` 补了 `/api/threats` 与白名单制的 `POST /api/action`
  （end_turn / set_research / set_civic / set_city_production / move_unit …），
  autopilot 全部读写都走 HTTP
- 进入自动模式时做**一次性接管**（杀掉其他持有链接的 python 进程，绝不碰游戏本体），
  然后 spawn 桥；桥丢失时自动重生
- 手动模式 = 停桥，FireTuner 槽位即刻释放，玩家随意接手

## 快速开始

```bash
# 依赖（fastapi/uvicorn 已随 civ6-mcp 安装的可跳过）
pip install -r requirements.txt

# API key（Windows 用户级环境变量也可以，服务端会自动回退读取）
set TYPESAFE_API_KEY=...        # bash: export TYPESAFE_API_KEY=...

# 启动战争议事厅
python -m uvicorn server.app:app --host 127.0.0.1 --port 8080
# → 打开 http://127.0.0.1:8080

# （可选）导入本战役的真实开局战报
python seed.py            # journal 有数据时需 --force
```

游戏侧前置条件：文明6（本战役为 Epic 版）开启 FireTuner
（选项 → 游戏选项 → 高级 → 启用 FireTuner，重启游戏），
civ6-mcp 以 MCP 注册到 ZCode/Cursor（见下文「ZCode 集成」）。

## 控制模式（自动 / 手动）

WebUI 顶栏的滑动块切换两种控制权，状态经 `GET/POST /api/mode` 读写：

- **手动（默认）**：桥进程停止，FireTuner 槽位释放，玩家直接操作游戏；
  Jev 决策与自动 end_turn 全部停止。历史记录仍然可见。
- **自动**：war-room spawn 一个 civ6-mcp 桥子进程（独占 FireTuner 链接），
  循环为：**收集 → 决策闸门 → （有决策点时）Jev 定向判断 → HTTP 动作 → end_turn**。
  另有一个 **1 秒常驻哨兵**与回合驱动并行运行：它轻探 `/api/tech` 与 `/api/cities`，
  在 AI 回合处理期间发现"科研/市政/生产空转"立即触发决策（选择类弹窗的响应
  延迟 ≈ 1-4 秒，不再等回合边界）。
- 自动模式启动时执行**一次性接管**：终止其他持有 FireTuner 链接的 python 进程
  （ZCode 复活的 civ6 MCP 服务器是典型对象；绝不触碰游戏本体，非 python 进程
  一律拒绝终止）。桥丢失时自动重生（`respawning bridge`）。
- 连续 3 次失败自动暂停回手动，原因写入编年史；再次点击"自动"即恢复。
- 游戏处理 AI 回合期间 FireTuner 端口会短暂关闭——UI 显示金色 `LINKING…`
  （autopilot 正在等链接回来），这不是故障。

## 每回合工作流（编排者协议）

1. **收集**：MCP 调用 `get_game_overview / get_units / get_cities /
   get_tech_civics / get_city_production / get_map_area …`
2. **过闸门**：`POST /api/gate`（`{"turn", "snapshot"}`，内含 `available` 可选项、
   `threats`、`settle_candidates` 等）。服务端存快照并与上一份差分，纯代码规则
   判定是否存在真实决策点：
   - `should_ask=false` → **不调用 Jev**，按既有计划行动即可（`skip_reason` 说明缘由）
   - `should_ask=true` → 只用返回的 `questions`（仅触发的题目）发起判断
3. **判断（仅在有决策点时）**：`POST /api/jev`，state 用刚存入的快照，
   questions 直接取闸门返回的定向问题集
4. **执行**：按答案调用 MCP 工具；每条操作 `POST /api/action`
   （`{"tool", "args", "result", "turn"}`，失败也照记——失败是编年史的一部分）
5. **结束**：`end_turn`（5 项反思必填），回到 1

闸门触发器（`server/decision_gate.py`，全部确定性代码、零 API 成本）：
`research_idle`（科研空转）、`civic_idle`、`production_idle`（城市队列空）、
`new_threat`（与上一快照差分出的新敌情）、`settler_idle`（开拓者闲置且提供了
候选点）、`policy_slot`（仅标记，通常代码可解）。

问题设计规范（来自 TypeSafe skill）：一题一个窄判断；criteria 的每个选项都要有
独立含义的描述；把完整语义放进题目而不是 qid；投机性问题与主问题同批并行发送。

## WebUI 说明（http://127.0.0.1:8080）

- **战局编年史**：按回合分段的事件时间线。GATE 卡显示每次闸门裁定（ASK/SKIP
  与触发器）；JUDGMENT 卡逐题展示答案、置信徽章（≥0.8 绿 / ≥0.5 金 / 低灰）
  与概率条（金色为胜选）；ACTION 卡区分成功/失败；STATE 卡可展开原始 JSON。
- **国势面板**：最新快照——回合数、文明、产量四宫格、科研进度条、城市、单位、
  威胁红卡、待办提示。
- **军师裁定**：最近一次 Jev 调用的全部裁定摘要。
- **决策闸门**：最近一次 GATE 裁定（SKIP 青色 / ASK 金色）与触发器标签，
  以及「闸门检查 / 跳过 / Jev 实际调用」计数——事件驱动成本的直接可视化。
- **战争账簿**：Jev 调用数、操作数、token 总量。
- **GAME LIVE**：经 `/api/live` 代理探测 civ6-mcp 仪表盘（:8000）判断游戏是否在线。
- 顶部筛选按钮可只看闸门/判断/行动/国势；全部数据 4 秒自动刷新
  （轮询只读本地 SQLite，**从不产生 Jev 调用**）。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/gate` | **决策闸门**：存快照 + 与上一份差分；返回 `{should_ask, triggers, questions}`，无决策点时不需要调 Jev |
| GET | `/api/gate/latest` | 最近一次闸门裁定 |
| POST | `/api/jev` | **判断网关**：服务端调 TypeSafe 并入账；body `{state, questions, model?, turn?, meta?}`——只应传闸门返回的定向问题 |
| POST | `/api/jev/record` | 登记一次已在别处完成的调用 `{request, response, latency_ms?, turn?}` |
| POST | `/api/action` | 记录一条游戏操作 `{tool, args, result?, turn?}` |
| POST | `/api/state` | 记录国势快照 `{turn, snapshot}`（不经闸门的手动通道） |
| GET | `/api/events?types=gate,jev,action,state&limit=80&before_id=` | 事件（倒序） |
| GET | `/api/state/latest` | 最新快照 |
| GET | `/api/stats` | 计数与 token 合计（含 `gate_checks` / `gate_skipped`） |
| GET | `/api/live` | 代理 civ6-mcp `:8000` 的 overview/units（游戏在线探测） |

## ZCode 集成

**MCP 注册**（已完成）：`~/.zcode/cli/config.json` 的 `mcp.servers.civ6` →
`python -m civ_mcp`（注意不是文档里写的 `python -m civ_mcp.server`——上游 server.py
没有 `__main__` 守卫，后者会静默退出）。civ6-mcp 已移植到 mcp 2.0
（`mcp.server.mcpserver.MCPServer`，详见其仓库内改动与 pyproject `mcp>=2.0`）。

**自动记录 hook（可选，实验性）**：`hooks/post_tool_use.py` 可在 ZCode 的
PostToolUse 事件里把 `mcp__civ6__*` 工具调用自动转投 `/api/action`。在项目级
`.zcode/config.json` 配置：

```json
{
  "hooks": {
    "enabled": true,
    "PostToolUse": [{
      "matcher": "mcp__civ6__.*",
      "hooks": [{"type": "command", "command": "python hooks/post_tool_use.py"}]
    }]
  }
}
```

## 项目结构

```text
jev-civ6/
├── server/
│   ├── app.py         # FastAPI：journal API + Jev 网关 + live 探测 + 模式切换 + 静态页
│   ├── journal.py     # SQLite 编年史（state / gate / jev / action 四类事件）
│   ├── decision_gate.py # 纯代码决策闸门：何时值得问 Jev
│   ├── autopilot.py   # 桥接自动驾驶：spawn civ6-mcp 子进程，HTTP 读写，零 socket
│   └── typesafe.py    # TypeSafe 客户端（429/529 退避重试；Windows 用户环境变量回退）
├── web/
│   ├── index.html     # 单页 UI（自动/手动开关，无构建步骤）
│   ├── style.css      # 暗色金调 Civ 风（Cinzel/Inter/JetBrains Mono，CDN 失败有回退）
│   └── app.js         # 增量时间线（只追加新事件）/ 侧栏 diff 更新 / 模式开关
├── hooks/
│   └── post_tool_use.py  # 可选 ZCode PostToolUse 自动记录器
├── artifacts/
│   ├── turn12_request.json    # 真实的 T12 六问题判断请求
│   └── turn12_answers.json    # 对应的 typed 答案
├── jev_judge.py       # 独立 CLI 判官（不经服务器直连 TypeSafe）
├── seed.py            # 导入真实开局战报
├── requirements.txt
└── README.md
```

## 设计决策

- **决策闸门先于判断**：Jev 按「次」计费，让"何时问"成为代码问题而非习惯问题。
  闸门用确定性规则（差分 + 空转检测）过滤掉绝大多数回合，只在真实决策点发起
  定向提问——这正是 TypeSafe 指南"extra questions still use tokens"的工程化落地。
- **桥接而非抢链接**：FireTuner 单客户端槽位只有一个归属——桥进程。autopilot
  是纯 HTTP 客户端，管理零个 socket；弹窗、重连、持久性全部继承上游实现。
  （历史上自持链接的三种死法：僵尸 socket 自锁、FIN→CLOSE_WAIT 排异、
  与 ZCode 复活的 MCP 进程抢槽——全部被桥接架构消灭。）
- **SQLite 而非 JSONL**：单文件零依赖，按类型/游标查询是 UI 的第一需求。
- **Jev 网关放服务端**：API key 不经过页面与编排提示词；请求/响应/延迟/token
  在同一处入账，天然防漏记。
- **游戏控制仍走 MCP**：civ6-mcp 的 76 个工具是唯一执行通道，war-room 只做
  记录与展示，不碰游戏——故障域隔离。
- **mcp 2.0 移植**：上游锁 `mcp>=1.20` 会装上 2.0 并因 `fastmcp` 缺失而崩溃；
  本战役已改用 `mcp.server.mcpserver` 并把约束提升为 `mcp>=2.0`（含顺带修复
  的 `lq` 缺失导入）。

## 已知边界

- **上游 web_api.py 已被本项目增强**（editable 安装直改源码）：`/api/threats`、
  白名单 `POST /api/action`。上游更新时这两处需要重新套用（diff 见仓库）。
- 游戏处理 AI 回合期间 FireTuner 端口会短暂关闭——桥内部自动重连，UI 在此期间
  显示金色 LINKING… 状态而非 OFFLINE。
- ZCode 侧注册的 civ6 MCP 服务器与桥是竞争关系：自动模式启动时的接管只杀持有
  链接的 python 进程；若想永久停用 ZCode 侧实例，从 `~/.zcode/cli/config.json`
  移除 `civ6` 条目并重启 ZCode。
- Jev 的概率是校准判断而非真理：阈值（如蛮族风险 0.5）应按战局后果调整，
  失败案例回看 RAW PAYLOAD 定位是状态缺失、题目歧义还是服务故障。
