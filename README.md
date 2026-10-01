# Jev × Civilization VI · 战争议事厅

把 **civ6-mcp**（通过 FireTuner 控制文明6）与**可自配置的 LLM 判断层**组合成一套
可观测的自动化游戏工作流：代码负责收集状态与执行动作，LLM 在决策点做语义判断，
本项目的 WebUI 负责把每一次判断、每一次操作、每一份国势快照记录成可回溯的战争编年史。

> **仓库结构说明**：本仓库是**单仓双件**——`server/`（战争议事厅，本项目的决策与自治层）
> + `civ6-mcp/`（**桥进程本体**，`python -m civ_mcp`，被 war-room 作为子进程拉起并独占
> FireTuner 连接）。**civ6-mcp 不是遗留物**：本仓库对它做了大量针对性改造
> （批量 Lua 往返、end_turn 快速路径、政策/外交/议会修复等），两者互相依赖、缺一不可。
> 桥的原始上游是 [lmwilki/civ6-mcp](https://github.com/lmwilki/civ6-mcp)（v1.1.11），
> 致谢 👏；本仓库内的版本包含大量本地改动，请勿混用上游版本。
>
> ⚠️ **运行前提的硬规则**（踩坑沉淀，详见下文「关键运维规则」）：
> 读档前 bridge 必须已连上 tuner；同机只跑一个 war-room。

```text
                 ┌──────────────────────────────────────────────┐
                 │            War-Room :8080 (本项目)            │
                 │  · 决策闸门（纯代码，零 API 成本）              │
                 │  · LLM 网关（provider 可配置，密钥仅服务端持有） │
                 │  · SQLite 编年史 + WebUI（自动/手动开关）       │
                 │  · AutoPilot：监控式循环 + 堵塞处理 + 防死循环   │
                 └───────┬──────────────────────────▲───────────┘
            HTTP 读+写   │                          │ 定向提问 / typed 答案
                 ┌───────▼──────────────┐   ┌───────┴────────┐
                 │ civ6-mcp 桥进程 :8000 │   │  LLM 判断层     │
                 │ (autopilot 的子进程)   │   │ (typesafe/openai│
                 │ · 独占 FireTuner:4318 │   │  /anthropic/mock)│
                 │ · PopupWatcher 关弹窗 │   └────────────────┘
                 │ · 崩溃自动重连        │
                 └──────────────────────┘
```

**决策自治能力一览**（Auto 模式下全部自动处理，无需人工）：
研究/市政/生产选择、单位战术（攻击/撤退/驻守）、开拓者定居规划、工人改良、
政策槽填补、总督任命与派驻、伟人招募、宗教创建与传教引擎、
世界议会投票、AI 交易与和平提议（接受/拒绝由 LLM 判断）、
外交问答、时代献礼选择、使节派遣、忠诚度/住房/舒适度预警、
跨边境传教的开放边境提议、以及各种回合阻塞的自动解锁。

## 快速开始（uv 环境，独立运行，不依赖 ZCode）

```bash
# 1) 环境：uv 一键创建 .venv 并装齐服务端 + 桥的全部依赖（含 civ6-mcp editable）
#    （没有 uv？先安装：pip install uv    或   winget install astral-sh.uv）
uv sync

# 2) LLM 配置：复制模板并编辑（或直接使用默认 typesafe）
#    TypeSafe: 从用户环境变量读 TYPESAFE_API_KEY
#    OpenAI 兼容 / Anthropic：见 jevciv6.example.toml 中的示例
copy jevciv6.example.toml jevciv6.toml        # PowerShell: Copy-Item

# 3) 启动战争议事厅
uv run uvicorn server.app:app --host 127.0.0.1 --port 8080
# → 打开 http://127.0.0.1:8080

# 4) （可选）自检：不需要游戏、不需要网络
uv run python -m unittest discover -s tests -v   # 30 项单测
uv run python scripts/dry_run_demo.py            # 干跑：闸门→state→判定→动作计划

# 5) （可选）导入本战役的真实开局战报
uv run python seed.py
```

游戏侧前置：文明6 开启 FireTuner（选项 → 游戏选项 → 高级 → 启用 FireTuner，重启游戏）。
桥进程（`python -m civ_mcp`）由 war-room 自动 spawn，默认使用**与服务器同一个解释器**
（即 `.venv`——`uv sync` 已把 civ6-mcp 以 editable 方式装好）；万一未装，也会自动把
`./civ6-mcp/src` 注入 PYTHONPATH 兜底。Python 版本要求 ≥ 3.12。

### ⚠️ 关键运维规则（2026-10-01 事故沉淀）

1. **加载存档前，bridge 必须已连上 tuner。** Civ6 只在存档加载（Lua 上下文创建）
   那一刻把 `InGame`/`GameCore_Tuner` 注册给 tuner；如果加载时没有任何客户端连着，
   这些上下文**永远不会暴露**——游戏画面正常但 bridge 无法操作（握手只见前端状态）。
   正确顺序：先开 war-room（bridge 连上主菜单状态的游戏）→ 再读档。
   验证/修复工具：`scripts/tuner_keeper.py`（保持一条连接并轮询状态列表，
   出现 InGame+GameCore_Tuner 即退出码 0）。
2. **tuner 客户端配额会被死连接耗尽。** 游戏侧的 tuner 只放行有限个客户端且
   不回收半死连接（CLOSE_WAIT 堆积后新连接直接 WinError 1225 拒绝）。反复
   spawn/杀 bridge 会自我耗尽配额——唯一解法是重启游戏进程。
3. **同一台机只跑一个 war-room。** 多个 war-room 实例会互相 takeover 对方的
   bridge（`_takeover_once` 杀"竞争控制器"），表现为 `bridge not ready after 90s`。
   检查：`netstat -ano | findstr :8081` 是否只有一个 LISTEN，以及是否存在
   未绑定端口的孤儿 uvicorn 进程。
4. **游戏意外退出后的恢复顺序**：启动 war-room → AUTO（bridge 连上主菜单）→
   游戏内手动/自动化读档（读档瞬间 bridge 必须在线）→ 回合恢复推进。
   本仓恢复实例：杀游戏 → Epic 协议拉起（`com.epicgames.launcher://apps/Kinglet?action=launch&silent=true`）
   → 主菜单 CONTINUE → 选 auto 存档行 → LOAD。


## LLM 配置（jevciv6.toml）

所有 provider 共用同一条调用形态 `judge(state, questions) -> answers`，
因此闸门、编年史、执行器与 UI 完全不感知你用的是哪家模型。

| provider | 说明 | 关键配置 |
| --- | --- | --- |
| `typesafe` | 原版 TypeSafe System One（Jev），默认 | `TYPESAFE_API_KEY` 环境变量 |
| `openai` | 任意 OpenAI 兼容 `/chat/completions`（OpenAI、DeepSeek、Moonshot、Qwen、Ollama、vLLM、LM Studio…） | `base_url`、`model`、`api_key_env` |
| `anthropic` | Anthropic Messages API | `model`、`api_key_env` |
| `mock` | 离线确定性应答（测试/干跑用） | 无 |

```toml
[llm]
provider = "openai"
base_url = "https://api.openai.com/v1"   # 或 http://127.0.0.1:11434/v1 等
model    = "gpt-4o-mini"
api_key_env = "OPENAI_API_KEY"           # 或 api_key = "sk-..."（不推荐明文）
```

环境变量可覆盖文件：`JEVCIV6_LLM_PROVIDER / JEVCIV6_LLM_MODEL / JEVCIV6_LLM_BASE_URL /
JEVCIV6_LLM_API_KEY / JEVCIV6_BRIDGE_URL / JEVCIV6_PYTHON / JEVCIV6_PORT /
JEVCIV6_TAKEOVER / JEVCIV6_CONFIG`。命令行判官同样支持：
`python jev_judge.py request.json --provider openai --model gpt-4o-mini`。

> 非 TypeSafe 提供方输出会被服务端 **规范校验**：choice 必须是题目 criteria
> 里的键（不在则按概率取合法最大值），noul 会夹取到 0..1；全部不可用时明确报错，
> 不会把坏答案写进编年史。

## 监控式主循环（v2，核心改动）

自动模式的单个回合循环：

1. **收集** → **闸门** → （有决策点才）**LLM 判断** → **执行动作**
2. `end_turn` 以短超时发出；等待期间每 ~1 秒轻量探测桥的 `/api/turnstate`
   （GameCore-only 查询，AI 计算回合期间**安全**），回合一推进立即进入下一轮——
   不再有长时间的阻塞盲等
3. `end_turn` 的阻塞文案会被**分类处理**，而不是反复硬重试：
   - `World Congress fires` → 读取议会状态 → 注册默认投票策略（选项 A、摊开 favor；
     0 favor 时也照常投出免费票）→ 重试 end_turn
   - 交易邀请 → 按配置自动拒绝（`auto_decline_deals`）→ 重试
   - 外交会话 → 自动 EXIT 关闭 → 重试
   - `Cannot end turn`（单位/生产等）→ dismiss/skip 后重试
   - 处理不了的 → **暂停并写编年史**（人工介入），绝不无限空转
4. **防卡死看门狗**：`stall_limit_s`（默认 1800s）内回合毫无推进 → 自动暂停；
   同类阻塞处理上限（2 次/类、6 次/回合）——历史事故（T121 因世界议会空转 11 小时
   4751 次）在机制上不可能复现
5. 收集阶段只在**非 AI 计算期**执行重的 InGame 查询；监控探测全部走 GameCore，
   避免历史上导致 AI 卡死的上下文切换问题

## 控制模式（自动 / 手动）

WebUI 顶栏滑动块切换控制权（`GET/POST /api/mode`）：

- **手动（默认）**：桥进程停止，FireTuner 槽位释放，玩家直接操作游戏。
- **自动**：war-room spawn civ6-mcp 桥子进程（独占 FireTuner），按上述循环推进。
  - 自动模式启动执行**一次性接管**：终止其他持有 FireTuner 链接的 python 进程
    （Windows；可用 `takeover_on_start=false` 关闭；绝不触碰游戏本体）。
  - 桥丢失自动重生；连续 3 次失败自动暂停回手动，原因写入编年史。
  - 等待期间 UI 显示 `AUTO · ending_turn · 等待回合约 Ns`；处理阻塞时显示
    `handling_world_congress` 等步骤名。
- 设置 `auto_decline_deals=false` 可以改为"遇到交易邀请就暂停"，由玩家手动处理。

## 每回合工作流（编排者协议 v2）

1. **收集**：overview / units / cities / tech / threats；空闲城市拉取该市的
   `list_city_production`；有闲置开拓者时拉取 `get_settle_advisor` 候选；首都
   半径 2 的地图快照（半径内 `map_area`）。
2. **过闸门**：`POST /api/gate`（`{"turn","snapshot"}`）。纯代码差分 + 空转检测：
   `research_idle / civic_idle / production_idle(按城) / new_threat / settler_idle /
   policy_slot`。
3. **判断**：`should_ask=true` 时用返回的定向题目调用 LLM。state 为富结构：
   `situation / empire{cities,units,yields} / threats / map_near_capital /
   settle_candidates / available{techs,civics,production_by_city}`——题目文案里
   引用的字段名与实际 state 严格对齐（修复了 v1 的字段错位缺陷）。
4. **执行**（已接线到动作白名单）：
   - `research_pick` → `set_research`；`civic_pick` → `set_civic`
   - `production_pick`（按城）→ `set_city_production`（区划自动带顾问推荐地块）
   - `tactics:{unit_index}`（**v2.1，每个军事单位一道选择题**）→ 选项由引擎验证：
     `attack:x,y`（可打目标+敌血量/战力）→ `attack_unit`；`advance` → 向最近威胁
     移动一格；`fortify` → 驻守回血；`retreat`（重伤时出现）→ 向城市撤退。
     没被提问的其余军事单位默认 `fortify`（回血+防御，不再挂机）。
     （v2.0 的单比特 `threat_response` noul 已移除——0.5 阈值下的边缘答案
     曾导致连续多回合完全不动。）
   - `settle_pick` → 建立"定居计划"：逐回合向目标格移动，抵达后 `found_city`
5. **结束回合**：监控式 `end_turn`（见上）；回合一推进，桥的叙述只等
   `narration_grace_s`（默认 5s，原为硬编码 20s——那曾是每回合最大延迟来源）；
   连续 `timeout_blocker_after`（默认 2）次超时无推进 → 主动
   dismiss popup + skip remaining units（解决"单位还有移动力"型卡死）。

## WebUI 说明（http://127.0.0.1:8080）

- **战局编年史**：按回合分段的事件时间线（GATE 裁定 / JUDGMENT 卡片带置信度与概率条
  / ACTION 成功失败 / STATE 快照可展开）。
- **国势面板**：最新快照四宫格、城市、单位、威胁红卡。
- **军师裁定 / 决策闸门 / 战争账簿**：最近一次判定、闸门统计（ASK/SKIP 与触发标签）、
  Jev 调用数与 token 合计。
- **GAME LIVE**：探测游戏是否在线（自动模式下显示 LINKING… 属正常过渡）。
- 数据 4 秒自动刷新（只读本地 SQLite，从不产生额外 LLM 调用）。
- `GET /api/config` 可查看当前引擎（provider:model）与配置来源。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/gate` | 决策闸门：存快照 + 差分；返回 `{should_ask, triggers, questions}` |
| GET | `/api/gate/latest` | 最近一次闸门裁定 |
| POST | `/api/jev` | 判断网关：服务端调 LLM 并入账（按配置的 provider） |
| POST | `/api/jev/record` | 登记一次已在别处完成的调用 |
| POST | `/api/action` | 记录一条游戏操作 |
| POST | `/api/state` | 记录国势快照 |
| GET | `/api/events?types=…&limit=80&before_id=` | 事件（倒序） |
| GET | `/api/state/latest` | 最新快照 |
| GET | `/api/stats` | 计数与 token 合计 |
| GET | `/api/mode` · POST `/api/mode` | 自动/手动切换与状态 |
| GET | `/api/live` | 游戏在线探测 |
| GET | `/api/config` | 非敏感配置摘要（引擎、来源、桥地址） |

桥进程（civ6-mcp）侧新增只读端点（autopilot 专用，不对外开放）：
`GET /api/turnstate`（1 秒级监控用，GameCore-only）、
`GET /api/settle_candidates?unit_index=`、`GET /api/district_advisor?city_id=&district_type=`；
动作白名单扩充了世界议会/交易/外交相关工具。

## 项目结构

```text
jev-civ6/
├── server/
│   ├── app.py          # FastAPI：journal API + LLM 网关 + live 探测 + 模式切换 + 静态页
│   ├── config.py       # ★ jevciv6.toml + 环境变量配置加载（多 provider）
│   ├── llm.py          # ★ provider 无关的判断层（typesafe/openai/anthropic/mock）
│   ├── journal.py      # SQLite 编年史（state / gate / jev / action 四类事件）
│   ├── decision_gate.py# 纯代码决策闸门：何时值得问 LLM（按城生产/定居接线）
│   ├── autopilot.py    # ★ 监控式自动驾驶：短超时 end_turn + turnstate 轮询
│   │                   #   + 堵塞分类处理 + 防卡死看门狗 + 定居/威胁执行器
│   └── typesafe.py     # TypeSafe 客户端（429/529 退避；Windows 环境变量回退）
├── tests/              # ★ 30 项离线单测（闸门/主循环/配置/LLM/执行器）
├── scripts/
│   └── dry_run_demo.py # ★ 离线干跑：闸门→state→判定→动作计划→堵塞分类
├── web/                # 单页 UI（无构建步骤）
├── hooks/              # 可选 ZCode PostToolUse 自动记录器
├── artifacts/          # T12 真实判断样本（请求/答案）
├── civ6-mcp/           # FireTuner 桥（上游 v1.1.11 + 本项目补丁，见其 git diff）
├── pyproject.toml      # ★ uv 项目定义（uv sync 一键建环境）
├── uv.lock             # ★ 依赖版本锁定
├── jevciv6.toml        # ★ 活动配置（provider 一行切换）
├── jevciv6.example.toml# ★ 全量示例配置
├── jev_judge.py        # 独立 CLI 判官（支持 --provider/--model/--config）
├── seed.py             # 导入开局战报
├── requirements.txt    # pip 传统方式等价清单（uv 用户可忽略）
└── README.md
```

## 设计决策

- **监控式等待取代长阻塞**：`end_turn` 只作短调用，回合状态由 1 秒级、GameCore
  安全的 `turnstate` 探测；"何时可以行动"从此是数据问题，不是睡眠问题。
- **堵塞分类处理 + 保险丝**：已知阻塞（议会/交易/外交/单位）有确定性处理；
  未知阻塞与长期停滞会**暂停并留痕**——自动化最怕的不是失败，而是无声空转。
- **决策闸门先于判断**：按「次」计费的判断只花在真实决策点；闸门差分 + 空转检测
  全是零成本确定性代码。
- **LLM 抽象层**：所有 provider 同一调用形态；答案做规范校验后才入账；
  key 在服务端读取、从不落日志、不经过前端。
- **桥接而非抢链接**：FireTuner 单客户端槽位只归桥进程，autopilot 管零个 socket；
  重连/弹窗/持久性继承上游实现。
- **SQLite 而非 JSONL**：单文件零依赖，按类型/游标查询是 UI 第一需求。
- **可回滚**：项目已初始化 git，本次改造前的基线在首个提交中；备份见工作区。

## 走向胜利的能力栈（v2.6）

- **胜利路线战略层**：无策略/每 30 回合/换新局时问 Jev 五选一（科技/文化/
  统治/宗教/外交），存入 `autopilot_state.json` 跨重启持久；此后每道
  科研/市政/生产/定居/政策题都附带战略提示，选择不再互相打架。
- **政策卡审查**：游戏会预填默认政策（槽永远"非空"），所以按
  15 回合周期让 Jev 复审每个槽位（当前卡带 `(current)` 标记，可保留可换），
  换卡合并为一次 `set_policies` 调用。实测 T112 首审：纪律→边防军。
- **使者派遣**（v2.7）：收集端点携带城邦与令牌余额（`SECTION|cs` 零额外
  往返），有令牌即问 `envoy_pick`（每个城邦列出类型/我方使者数/宗主国），
  执行 `send_envoy`。实测 T113-T114 连续向同一城邦集中派遣冲宗主门槛。
- **时代着力点**（v2.9）：新时代的 Dedication 强制选择屏由 fast end_turn
  入场门禁捕获（`selections_allowed > 0`），Jev 按时代类型（黑暗/黄金/普通
  影响加成文案）结合胜利路线选择着力点并 `choose_dedication`。实测 T123：
  宗教路线 → COMMEMORATION_RELIGIOUS，弹窗即解。
- **问题型外交会话**（v2.17）：无枚举选项、只有台词的会话（如"允许建立
  大使馆吗？"）EXIT 关不掉——Jev 判定接受/拒绝后按 **交易应答 → ACCEPT/
  DECLINE → CHOICE_POSITIVE/NEGATIVE → EXIT** 兜底链执行，每步后验证会话
  真实关闭。实测：印尼大使馆请求 → DEAL_ACCEPTED,回合立即恢复。
- **外交/交易 Jev 决策**（v2.7）：交易阻塞不再盲目全拒——结构化读取
  pending deals（对方给什么/我们要给什么），`deal_response` 由 Jev 判断
  （使团/开放边境/公平交易通常值得接受）；外交会话有真实选项时
  （如结好请求）由 Jev 选择应答，纯告别屏仍自动关闭。判定失败回退旧行为。
- **建筑工经济层**（确定性反射,零 LLM）：闲置 builder 站上可改良格→直接
  `improve_tile`；否则走向最近未改良资源格（排除城市中心与被占格）。
  实测 T99 采石场、T103 营地——首次改良产出。
- **创教全流程**（v2.14）：`_advance_prophet` 反射层——预言家自动走向我方
  圣地格 → `activate_great_person` 激活 → 查询创教状态 → Jev 选宗教名+
  创始人信条+信徒信条 → `found_religion`；万神殿缺失时顺路 Jev 选择。
  创教标志持久化跨重启。实测 T162 预言家登圣殿格、T163 激活、
  T170 **佛教创立**（教皇权威+神灵的启示——与城邦使者战略协同）。
- **忠诚度/宜居度感知与应对**（v2.15）：城市快照携带 loyalty/amenities，
  生产题注入舒适度警报（忠诚<70 会翻独立警告、宜居不足提示娱乐/奢侈，
  城市数<3 时强调开拓者）；总督维护反射自动驻派未上任总督（**一城一总督**
  +每回合一次节流——驻派是异步的，不节流会用旧状态把自己的总督顶掉）。
  本战役教训：波季因无人管理忠诚而独立。
- **常驻事件框架**（v2.5）+ 战术批处理（v2.4）+ fast end_turn（v2.2-2.3）
  见下文延迟优化记录。

## 已知边界与后续可做

- 世界议会投票目前用桥的**默认策略**（选项 A、摊开 favor；先保证不卡死），
  后续可在有 favor 时把决议内容交给 LLM 判断。
- 政策卡槽（policy_slot）目前只标记不自动填——`set_policies` 已存在于桥，
  但需要一套"选卡"判断，建议下一轮做。
- 战术选择题每次最多覆盖 4 个军事单位（`TACTICS_UNIT_CAP`，控 token 成本）；
  大军团时其余单位默认驻守。更聪明的多单位协同（集火、夹击、地形）值得单独立项。
- 移动/攻击的路径智能有限（引擎验证 + 一格推进）；`retreat` 只退向第一座城。

## 延迟优化记录（v2.2/v2.3）

FireTuner 是单连接串行通道（桥内全局锁，实测每次 Lua 往返 ~1s），一切优化
都围绕"减少往返次数、不霸占通道"：

1. **end_turn(fast)**（v2.2）：入场快速检查后发令，回合一推进立即返回；
   不做快照差分/游戏内存档/通知/威胁扫描（那套收割曾独占通道 20-40s，
   把后续指令全部堵死——"操作要等 20s"的直接元凶）。AI 回合超 20s 未推进时
   返回 `FAST_NO_ADVANCE`，重试不发重复指令（in-flight 防重）。
2. **叙述宽限期**：20s（硬编码）→ `narration_grace_s`（默认 2s）。
3. **指令批处理**（v2.3）：`move_unit` 5 次往返 → 2 次（弹窗清扫 Lua 免费
   前置拼接 + 位置/视野合成单个 GameCore 查询，视野以 Lua 侧取单位实际
   落点为中心）；`attack_unit` 5 次往返 → 2 次（弹窗清扫 + 战斗预估 + 攻击
   合成一段脚本）。
4. **全量批处理**（v2.4）：
   - 收集：桥新增 `/api/warroom_collect`——五路状态（overview/units/cities/
     tech/threats）合并为 **2 次** Lua 往返（InGame + GameCore 各一段，
     SECTION 标记切分后喂给原有解析器），实测 ~3.5-5s。
   - 行军：`move_units_batch`——N 个单位的移动、位置回读、视野差分全部
     压进 **2 次**往返（每单位 pcall 包裹，一个失败不影响整批）。
   - 驻守：`fortify_units`——N 个单位一次往返。
   - 实测：一整回合的决策面（收集+判定+生产+攻击+行军+驻守）约 **7 秒**
     完成；"回合可操作 → 首个动作执行" 约 5-6 秒。回合总时长此后由
     游戏 AI 计算时间主导（~25-35s），不再是 war-room 的开销。
5. **preflight 五合一**（v2.10）：fast end_turn 的入场检查（存活/外交会话/
   待处理交易/世界议会临近+handler/着力点待选）合并为**一次往返**的
   `PF|` 探针，具体项的全量检查只在旗标触发时执行——入场从 ~7 次往返
   降到 3 次。实测 end_turn→下一回合收集从 26s 降到 4-8s；战术题上限
   4→8（单次判定覆盖全部单位）；叙述宽限 2s→0.5s。
6. **大伟人领取**（v2.11）：preflight 用 `gp:CanRecruitPerson` 计数可领取
   伟人（零额外往返），入场门禁触发后 Jev 按能力描述+战略提示选择领取
   （宗教路线明确标注大预言家关键）。实测 T142：Jev 领取大预言家琐罗亚斯德，
   回合立即恢复。无可领对象时的赞助弹窗自动拒绝兜底。
7. **政策槽位强制填补**（v2.12）：preflight 计数空槽（`GetSlotPolicy(s)<0`，
   零额外往返）→ 阻塞文案 → Jev 逐空槽选卡（兼容卡过滤+战略提示）→ 一次
   `set_policies` 填满。实测 T150：Jev 为宗教路线选 POLICY_SCRIPTURE（经文）。
8. **总督头衔**（v2.13）：preflight 用 `GetGovernorPoints()−GetGovernorPointsSpent()`
   （**累计−已花费**，直接用原值会误报）计数可用头衔 → Jev 选总督任命（宗教
   路线选了莫克夏/信仰总督）→ **自动驻派首都**（任命是异步的且总监屏要求
   驻派城市才关闭）→ 晋升路径同理。三处竞态实测修复。
9. **决策面三秒化**（v2.16）：①收集端点支持 `?pol=1&cs=1&gov=1` 按需段——
   政策/城邦/总督等低频数据只在事件提示或周期到点时拉取（tech/threats 实证
   不能并入 InGame 上下文，保持 GameCore 段）；②preflight 吸收弹窗清扫，
   end_turn 入场 3 往返 → 2；③policy_fill/governor 等异步动作统一"结算等待
   +盖章"防重触发。实测决策周期（收集→判定→动作）12-15s → **3-5s**，
   回合墙钟剩余部分为游戏 AI 计算时间。
10. **超时保险丝**：连续 `timeout_blocker_after`（默认 4）次无推进 → 主动
   dismiss + skip remaining units（"单位还有移动力"型卡死的兜底）。
11. **常驻事件框架**（v2.5，`lua/warroom.py`）：桥向游戏 InGame 上下文注入
   `__wr` 常驻框架——挂载精选事件钩子（回合/战斗/建城/科研/市政/外交…），
   事件入环形缓冲，随收集端点**零额外往返**带回；首次注入还会用
   `pairs(Events)` 枚举出本版本真实存在的事件名清单（实测 35 个）入账。
   事件以 `game_event` 类型进编年史，并注入快照 notes 供闸门/Jev 参考。
   自愈：上下文重载（读档/新局）后 `__wr` 消失 → 下次收集自动重注入。
- `takeover_on_start` 的进程接管仅 Windows 有效；其他平台请手动确保没有别的
  FireTuner 持有者。
- `turnstate` 的 `cities` 字段在个别版本可能返回 -1（探测不到）——监控不依赖它，
  收集阶段仍会拿到全量城市数据。
- 上游 civ6-mcp 更新时，`web_api.py` 与 `game_state.py` 的本项目补丁需要重新套用
  （见 `civ6-mcp` 仓库内 `git diff`）。

## 本次改造摘要（v1 → v2）

针对提出的三个问题，本次的定位与修复：

1. **"每次等待都很久，不能持续监控吗？"**
   - 证据：真实战报中 T121 一条回合因世界议会阻塞被空转 **4751 次 end_turn、682 分钟**；
     期间无人处理、无人暂停。
   - 修复：监控式等待（1s turnstate 探测）+ 堵塞分类处理 + 停滞看门狗 + 阻塞重试上限。
   - 监控探测使用 GameCore-only 查询（历史事故的另一诱因是 AI 计算期间发 InGame 查询）。
2. **"测试都失败了、被打败了，是不是背景信息和选项不对？"**
   - 核查结论：不是"选错"这么简单，是四处系统性缺陷——① 发给判定层的 state 只有
     `{"snapshot": …}` 一层，而题目文案引用了 `empire/terrain/focus` 等不存在的键；
     ② 开拓者从无候选、从未接线（110 回合 0 扩张的直接原因）；③ 生产选项只取第一座城、
     实际成为"永动机战士"；④ 世界议会/交易/外交没有执行通道（死循环与卡死的直接原因）。
   - 修复：富 state 对象（字段与题目严格对齐）、定居全流程接线、按城生产选项、
     堵塞处理通道、攻击优先的威胁响应、同回合动作去重。
   - 客观说明：旧对局的军事劣势也与地图/难度/规模有关，但上述缺陷会系统性放大败局；
     建议用新管线重新开一局对照验证。
3. **"将项目独立出来，可以自己配置 LLM"**
   - `jevciv6.toml` + 4 种 provider（TypeSafe / OpenAI 兼容 / Anthropic / mock），
     环境变量覆盖、CLI（`jev_judge.py --provider …`）、答案规范校验；
   - uv 一键环境：`uv sync` 建好 `.venv` 并装齐全部依赖（含桥的 editable 安装）；
     顺带修复桥在中文 Windows 因系统默认 GBK 编码导致的启动崩溃
     （`version.py` / `diary.py` 显式 utf-8——由 uv 环境实测发现）。
   - 桥自动从 `./civ6-mcp/src` 加载（免 pip 安装子项目）、Windows 特性隔离、
     全量 README（独立部署步骤 + ZCode 作为可选附录保留）。

## 测试与自检

```bash
uv run python -m pytest tests/ -q        # war-room: 57 tests
cd civ6-mcp && uv run pytest tests/ -q   # bridge:  99 tests
uv run python scripts/dry_run_demo.py    # offline end-to-end pipeline demo
```

这些命令都不需要游戏本体与网络连接；任何改动后建议先跑这些再上游戏。

## v3 改造摘要（2026-10-01 · 实战排障沉淀）

一次完整战役（T217→T330+，宗教胜利路径）中暴露并修复的系统性问题：

1. **FireTuner 生命周期三定律**（详见「关键运维规则」）：
   - 游戏的 tuner 客户端配额会被死连接耗尽（CLOSE_WAIT 堆积→WinError 1225），
     唯一解法是重启游戏；bridge 反复 spawn/杀会自我耗尽配额。
   - **读档瞬间必须有 tuner 客户端在线**，否则 `InGame`/`GameCore_Tuner`
     上下文永不注册（游戏画面正常但桥无法操作）。
     工具：`scripts/tuner_keeper.py` 验证/恢复。
   - 同机多 war-room 实例会互相 takeover 对方的桥（表现：`bridge not ready`）。
2. **政策填充静默失败**：`UNLOCK_POLICIES` 与 `RequestPolicyChanges` 同帧提交会被
   引擎丢弃。拆为「解锁→落定→提交→落定→读回验证→重试」序列（bridge 侧）。
3. **世界议会卡回合**：会话开启但无人提交时 end_turn 永久"处理中"。
   end_turn 超时熔断新增议会探测 + `submit_congress` 收尾（war-room 侧）。
4. **外交两处盲区**：① 叙事文案 `AI diplomatic proposal` 未被分类器捕获（死循环）；
   ② AI 求和/交易是 **无会话的 pending deal**（`HasPendingDeal`），旧查询看不见。
   现由超时熔断统一探测（议会→交易）并路由到 LLM 决策处理器。
5. **开拓者堵城**：无定居点时开拓者永站城市中心，阻断一切信仰购买
   （STACKING_CONFLICT）。新增 park reflex 挪出城一格。
6. **传播引擎优化**：宗教单位仅在城内/邻城时尝试传教；越境受阻时自动向
   对方提议互开边境（每文明 20 回合节流，跳过交战国）。
7. **杂项**：takeover 的 PowerShell 探测超时容忍（高负载下 PS 首启可超 15s）、
   policy_fill 过时标志不再误暂停、错误自动截图（`artifacts/screenshots/`）。

