# Jev × Civilization VI · 战争议事厅

把 **civ6-mcp**（通过 FireTuner 控制文明6）与**可自配置的 LLM 判断层**组合成一套
可观测的自动化游戏工作流：代码负责收集状态与执行动作，LLM 在决策点做语义判断，
本项目的 WebUI 负责把每一次判断、每一次操作、每一份国势快照记录成可回溯的战争编年史。

> **v2 更新（本次完善）**：主循环改为「监控式等待」——1 秒级探测回合状态，
> 回合一结束立即行动；世界议会 / 交易 / 外交等回合阻塞会被自动分类处理；
> 新增防卡死看门狗；LLM 后端可在 `jevciv6.toml` 中自选
> （TypeSafe Jev / OpenAI 兼容 / Anthropic / 离线 mock）。详见文末「本次改造摘要」。

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

## 快速开始（独立运行，不依赖 ZCode）

```bash
# 1) 依赖（服务端 + 桥的运行时依赖；桥的源码从 ./civ6-mcp/src 自动加载）
pip install -r requirements.txt

# 2) LLM 配置：复制模板并编辑（或直接使用默认 typesafe）
#    TypeSafe: 从用户环境变量读 TYPESAFE_API_KEY
#    OpenAI 兼容 / Anthropic：见 jevciv6.example.toml 中的示例
copy jevciv6.example.toml jevciv6.toml        # PowerShell: Copy-Item

# 3) 启动战争议事厅
python -m uvicorn server.app:app --host 127.0.0.1 --port 8080
# → 打开 http://127.0.0.1:8080

# 4) （可选）自检：不需要游戏、不需要网络
python -m unittest discover -s tests -v      # 30 项单测
python scripts/dry_run_demo.py               # 干跑：闸门→state→判定→动作计划

# 5) （可选）导入本战役的真实开局战报
python seed.py
```

游戏侧前置：文明6 开启 FireTuner（选项 → 游戏选项 → 高级 → 启用 FireTuner，重启游戏）。
桥进程（`python -m civ_mcp`）由 war-room 自动 spawn；它需要的 `civ_mcp` 包直接从
`./civ6-mcp/src` 注入 PYTHONPATH，**不需要 pip 安装这个子项目**（其依赖已并入
requirements.txt）。

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
   - `threat_response ≥0.5` → 优先引擎确认可打的 `attack_unit`；否则最多两台单位
     向最近威胁推进一格（睡着先唤醒、被阻挡改 fortify、同回合同一移动不重复）
   - `settle_pick` → 建立"定居计划"：逐回合向目标格移动，抵达后 `found_city`
5. **结束回合**：监控式 `end_turn`（见上）；5 项反思由桥的叙事返回。

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
├── jevciv6.toml        # ★ 活动配置（provider 一行切换）
├── jevciv6.example.toml# ★ 全量示例配置
├── jev_judge.py        # 独立 CLI 判官（支持 --provider/--model/--config）
├── seed.py             # 导入开局战报
├── requirements.txt    # 服务端 + 桥依赖
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

## 已知边界与后续可做

- 世界议会投票目前用桥的**默认策略**（选项 A、摊开 favor；先保证不卡死），
  后续可在有 favor 时把决议内容交给 LLM 判断。
- 政策卡槽（policy_slot）目前只标记不自动填——`set_policies` 已存在于桥，
  但需要一套"选卡"判断，建议下一轮做。
- 移动/攻击的路径智能有限（引擎验证 + 一格推进）；更复杂的军事 AI 值得单独立项。
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
   - 桥自动从 `./civ6-mcp/src` 加载（免 pip 安装子项目）、Windows 特性隔离、
     全量 README（独立部署步骤 + ZCode 作为可选附录保留）。

## 测试与自检

```bash
python -m unittest discover -s tests -v   # 30 tests: gate / autopilot / config+llm / executor
python scripts/dry_run_demo.py            # offline end-to-end pipeline demo
```

两个命令都不需要游戏本体与网络连接；任何改动后建议先跑这两个再上游戏。
