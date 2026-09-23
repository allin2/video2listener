---
title: 现存缺陷修复与可用性优化建议
date: 2026-09-14
scope: 缺陷复核与修复 + 实际运行产品后的可用性评估
verification: 106 个测试通过；服务实测启动并走通真实提交→进度→失败→历史全流程
---

# 缺陷修复与可用性优化建议

## 摘要

- 复核了 7 个问题：**5 个确认存在并已修复**，1 个此前已修（我仅验证），1 个是规格口径不一致（需决策，未改代码）。
- 全部修复附回归测试，测试套件 **106 passed**（修复前 97）。
- 实际启动产品并通过浏览器走通真实流程，过程中**新发现 5 个可用性问题**，其中"模型字段阻塞提交"和"未知状态默认显示成功绿点"两处是代码级可确认的缺陷。
- ⚠️ **本次运行在产品里留下了一条副作用记录**（`dQw4w9WgXcQ` 的 faithful 变体失败记录），需要的话我可以清掉，见文末。

---

# 第一部分：缺陷复核与修复

## D1 · TTS 文本清洗删除下划线，破坏技术标识符

**现象**
朗读文本里的下划线被全部删除，技术标识符失真。真实产物中的证据（`data/dQw4w9WgXcQ/variants/podcast/tts_text.txt`）：

```
请设置环境变量 VIDEO2LISTENERDEEPSEEKAPIKEY 后重试以获取中文版
```

原文是 `VIDEO2LISTENER_DEEPSEEK_API_KEY`。这条提示是要念给用户听的配置指引，下划线一删，变量名就变成了一个字面上不存在的名字，指引失效。

**复现步骤**
1. 调用 `clean_for_tts("请设置 VIDEO2LISTENER_DEEPSEEK_API_KEY 后重试。")`
2. 观察返回值：下划线全部消失

或直接用现成产物：打开上面那个 `tts_text.txt`。

**根本原因**
`src/tts/cleaner.py:34` 的字符类里包含了 `_`：

```python
text = re.sub(r"[*_~`\[\]{}|\\]", "", text)
```

这一行的意图是清掉 Markdown 强调符号和朗读无意义的符号，但 `_` 在本项目里是**内容**而不是格式：prompt 明确要求"技术缩写保持英文原样""专有名词保留英文原名"，而本产品的素材是 AI/科技内容，`snake_case` 标识符、环境变量名、Python dunder（`__init__`）、带下划线的文件名都很常见。

**修复方案**
`src/tts/cleaner.py`：从字符类里移除 `_`。

同时**刻意不为 `__强调__` 加还原规则**——`__init__` 与 Markdown 粗体在散文中都是"空格 + 双下划线 + 词 + 双下划线 + 空格"，无法区分，保内容优先。`**粗体**`、`*斜体*`、反引号的清理保持不变。

```python
text = re.sub(r"[*~`\[\]{}|\\]", "", text)
```

**验证**
新增 3 个回归测试（`tests/test_tts.py`）：

| 输入 | 期望 | 结果 |
|---|---|---|
| `VIDEO2LISTENER_DEEPSEEK_API_KEY` | 原样保留 | ✅ |
| `__init__` | 原样保留 | ✅ |
| `api_key` / `get_config` / `config.yaml` | 原样保留 | ✅ |
| `**重点**：使用 snake_case 命名。` | `**` 去掉且 `snake_case` 保留 | ✅ |

---

## D2 · 多集拆分按片段个数平均分，单集时长可超上限

**现象**
输出音频超过 `max_output_minutes`（默认 60 分钟）时会拆成多集，但**某一集可能远超上限**。用真实片段分布复算：

| 片段时长分布（分钟） | 修复前产出 | 修复后产出 |
|---|---|---|
| 40 / 1 / 1 / 40 / 40 / 1 | 2 集，最长 **81 分钟** ❌ | 3 集，最长 42 分钟 ✅ |
| 20 × 5 | 2 集，最长 60 分钟 ✅ | 2 集，最长 60 分钟 ✅ |

81 分钟 > `max_output_minutes=60`，单集上限形同虚设。

**复现步骤**
1. 构造若干时长差异较大的音频片段（例如几个 40 分钟 + 几个 1 分钟）
2. 调用 `merger.merge(segments, output_path)`
3. 用 ffprobe 量任一 `_Part*.mp3` 的时长，会超过 60 分钟

**根本原因**
`src/audio/merger.py` 原逻辑按**片段个数**均分，与片段实际长度无关：

```python
part_count = int(total_duration_minutes / max_minutes) + 1
segs_per_part = len(segments) // part_count + 1        # ← 按个数
```

片段长度差异可达十倍以上（一个 200 字片段 vs 一个 15 字片段），按个数分必然出现某一集过载。

**修复方案**
`src/audio/merger.py`：

1. 新增 `_probe_durations()` —— 逐片段用 ffprobe 取**实际时长**，单个文件读取失败时回退到按文件大小估算，缺失文件按 0 处理（不让一个坏文件毁掉整批计算）。
2. 新增 `_group_by_duration()` —— 按时长贪心切分，保证每集不超过上限。
3. 单个片段自身就超过上限时**不切分**（从句子中间断开听感更差），单独成集并 `logger.warning` 告警，便于事后排查。
4. 删除不再使用的 `_estimate_duration()`（原实现还存在"预设平均语速 250 字/分钟"的过时假设）。

**验证**
新增 4 个回归测试（`tests/test_audio.py`）：时长差异大、均匀分布、恰好压线、略超上限、单片段超限保持完整、缺失文件回退。全部通过。

---

## D3 · 部署配置指向不存在的模块，服务无法启动

**现象**
按 `deploy/setup.sh` 部署后 systemd 服务起不来。三处独立问题：

| # | 问题 | 后果 |
|---|---|---|
| a | `ExecStart=... uvicorn src.bot.server:app` | **`src/bot/server.py` 根本不存在**（`src/bot/` 下只有 `__init__.py` 和 `handler.py`），服务启动即失败 |
| b | `setup.sh` 创建 `venv/`，而 `CLAUDE.md` 与 launchd plist 用 `.venv/` | 目录名与文档/其他入口不一致，PATH 指向错误路径 |
| c | 单元文件里 `Environment=VIDEO2LISTENER_DEEPSEEK_API_KEY=` 等**空值** | 显式空值会覆盖外部传入的同名变量，密钥永远注入不进去 |

另有 `VIDEO2LISTENER_FEISHU_APP_ID` / `_APP_SECRET` 两个变量——飞书接入已从代码中移除，`src/config.py` 只读 3 个变量，这两个是**死配置**，会误导部署者去申请无用的凭据。

**复现步骤**
1. `grep -n ExecStart deploy/video2listener.service` → `src.bot.server:app`
2. `ls src/bot/` → 只有 `__init__.py`、`handler.py`，无 `server.py`
3. `grep -rn "FEISHU" src/` → 无命中
4. `grep -n "venv" deploy/setup.sh` → `python3 -m venv venv`

**根本原因**
模块在一次重构中从 `src.bot.server` 改名为 `src.web.server`，但 deploy 下的两个文件没有同步；飞书功能下线后环境变量也未清理。

**修复方案**
- `deploy/video2listener.service`：`ExecStart` 改为 `src.web.server:app`；删掉 4 个 `Environment=` 空值行，改为 `EnvironmentFile=-/etc/video2listener.env`（前缀 `-` 表示文件不存在时不报错，便于先在页面配置密钥）；补注释说明为何不能写空值。
- `deploy/setup.sh`：虚拟环境统一为 `.venv`（与 `CLAUDE.md`、launchd plist 一致）；sed 替换改用 `$VENV_DIR`；环境变量提示改为只列真实存在的 3 个，并指向 env 文件。
- 新增 `deploy/env.example`：列出 `src/config.py` 实际读取的 3 个变量，并注明"名字写错不会有任何报错，服务会表现为未配置 API Key"。

**验证**
`scripts/com.video2listener.server.plist` 本身是正确的（`src.web.server:app` + `.venv`），可交叉比对——正是它佐证了 `.venv` 才是约定。

---

## D4 · 状态接口把 A 变体的错误泄漏给 B 变体

**现象**
实测（修复前）：

```
GET /api/status/dQw4w9WgXcQ/podcast
→ { "status": "done",
    "output_path": ".../Rick_Astley_..._podcast.mp3",
    "error_message": "No module named 'faster_whisper'" }   ← 矛盾
```

播客版**是好的、可下载的**，却带着另一条失败信息。同一个响应里 `status` 说成功、`error_message` 说失败。

**复现步骤**
1. 对同一视频，先生成 `podcast` 变体至成功
2. 再生成 `faithful` 变体，让它失败
3. `GET /api/status/<video>/podcast` → 返回 `status: done` 且 `error_message` 非空

**根本原因**
`src/web/server.py` 的 `/api/status` 兜底分支里，错误信息先取变体自己的，取不到就**回落到共享的 episode 行**：

```python
"error_message": (
    variant.get("error_message") if variant and variant.get("error_message")
    else episode.get("error_message")          # ← 共享行，被别的变体写过
),
```

`episode` 行是按视频共享的，`db.update_status(video_id, FAILED, error_message=...)` 会把任一模式的失败写进去，因此跨变体污染。

**影响范围（诚实说明）**
当前前端只在 `state.status === 'failed'` 时才用 `error_message`，所以**用户界面暂时看不到这个矛盾**。但这是 API 契约层面的错误，任何其他消费者（历史面板改造、脚本、第三方客户端）都会踩到。属于"潜在缺陷"，不是"当前可见缺陷"。

**修复方案**
`src/web/server.py`：错误信息只在对应状态本身失败时透出。

```python
variant_error = variant.get("error_message") if variant else None
if variant_error:
    error_message = variant_error
elif db_status in ("failed", "cancelled"):
    error_message = episode.get("error_message")
else:
    error_message = None
```

**验证**
新增 2 个回归测试（`tests/test_web.py`），一正一反：已完成变体不再带错误信息；失败变体仍照常透出错误。运行时复验：

```
GET /api/status/dQw4w9WgXcQ/podcast  → status=done   error_message=None          ✅
GET /api/status/dQw4w9WgXcQ/faithful → status=failed error_message="No module named 'faster_whisper'"  ✅
```

---

## D5 · 多集文件按字典序排序，集数错乱

**现象**
`Episode_Part10.mp3` 会排在 `Episode_Part2.mp3` 前面，下载列表集数乱序。

**复现步骤**
1. 目录里放 `e_Part1.mp3` … `e_Part10.mp3`
2. 打开历史抽屉 → 集数显示顺序为 1, 10, 2, 3 …

**根本原因**
`src/web/server.py` 的 `_detect_parts()` 用 `sorted(..., key=lambda p: p.name)`，字符串排序而非数值排序。

**修复方案**
新增 `_part_sort_key()`，用正则取 `_Part(\d+)` 的数值作为排序主键，文件名作次键。

**说明**
这个缺陷影响面较小（需单集超过 9 集，即源素材超过 9 小时才会触发），但修复成本极低，一并处理。

---

## 已修复但残留脏数据：缺 API Key 时产出"报错音频"

**这一条不是本次修复的，代码里已经有守卫**，我核实后记录在此，因为**残留产物仍在污染产品**。

**历史现象**（`data/dQw4w9WgXcQ/variants/podcast/`，产出于 2026-07-03）

```
注意：以下为英文原文（未翻译，因未配置 LLM API Key）请设置环境变量…（下划线已被 D1 删除）
```

这段错误提示被送进 TTS，合成了一个 **41.4 秒的 MP3**，并且在数据库里状态是 `done`。字/秒 1.76，是本次测量中唯一的异常点（正常值 5.66）。

**当前状态**：`src/translation/client.py:800-807` 已有守卫，明确拒绝生成英文兜底产物：

```python
if not api_key or api_key == "placeholder":
    logger.error("No LLM API key configured — refusing to create an English fallback artifact")
    raise RuntimeError("未配置翻译 API Key。请在页面填写并保存配置后重试；系统不会再把英文原文当作中文音频生成。")
```

**残留问题**：历史里那条 `done` 记录仍在 UI 上作为**可下载的"中文播客版"**展示（我在浏览器里确认了下载按钮存在，且 `/api/download` 返回 `HTTP 206 audio/mpeg`）。用户点下载会得到一段念报错的音频。这是数据卫生问题，不是代码问题——需要清理该条记录。

---

## 需决策：condensed 时长目标两个口径不一致（未改代码）

| 出处 | 口径 |
|---|---|
| `CONCEPTS.md` | condensed 压缩到**原内容量**的 30–50% |
| `scripts/evaluate_mode_outputs.py:76-78` | condensed **时长** / **faithful 时长** ∈ [0.25, 0.55] |

一个相对**源**，一个相对**忠实版**，两者不等价（实测 faithful 本身是源的 1.091 倍）。建议统一为相对源视频时长——那是用户可感知的基准。

这属于产品定义问题，我没有擅自改阈值。

---

# 第二部分：实际运行与可用性优化建议

## 运行环境与边界（先说清楚）

| 项目 | 情况 |
|---|---|
| 服务启动 | ✅ 成功，`uvicorn src.web.server:app` @ 127.0.0.1:8080 |
| 依赖安装 | ⚠️ 完整依赖安装卡在解析阶段（实测带宽仅 ~70–110 kB/s）；改用最小集（fastapi/uvicorn/pyyaml/openai/edge-tts/yt-dlp/httpx/pytest）——`faster_whisper` 是函数内延迟导入，不影响服务启动 |
| 测试 | ✅ 106 passed（需 `--basetemp` 指向项目内目录，沙箱禁止写 pytest 默认临时目录） |
| 真实流程 | ✅ 提交 → 进度时间线 → 失败收敛 → 历史抽屉，全部走通并截图 |
| **无法验证** | ❌ 无 DeepSeek / MiMo 密钥，**未跑通完整成功链路**（下载→转写→翻译→TTS→合并）。翻译之后的环节未被实际执行 |

因此下面的建议基于：真实接口响应 + 真实浏览器渲染 + 源码级确认，而不是猜测。

## 实测发现

### 交互流程

**F1 · 配置区占据首屏，核心动作被压到底部（优先级：高）**

实测首屏（1280×900）从上到下是：LLM 配置（API Key / Base URL / 查询模型 / 模型）→ TTS 配置（API Key / Base URL / 加载音色 / 模型 / 音色 / 试听）→ 记住配置 → **YouTube 链接** → 输出模式 → 开始处理。

用户的核心动作（贴链接 → 开始）在首屏底部，必须先滚过 7 个配置字段。"记住配置"能缓解重复使用，但**首次使用的心智负担很高**。

建议：把两组密钥配置折叠进"高级设置"，默认收起并在标题显示配置状态（如"已配置 ✓ / 未配置"）；链接输入 + 模式选择上移到首屏。若两项密钥都已保存，直接展开为"一行式"摘要。

预期效果：首次使用从"读 7 个字段"降到"读 1 个输入框"；重复使用场景下直接可见提交按钮，不用滚动。

**F2 · 三种模式零解释（优先级：高）**

UI 只有三个 radio 标签"中文播客版 / 忠实翻译版 / 精华浓缩版"，**没有任何一句差异说明**。而 `CONCEPTS.md` 里三种模式的定义相当清晰（忠实=逐句对应，播客=口语化重组，浓缩=压到 30–50% 并按主题重组）。

三种模式是本项目的核心差异化，但用户此刻只能靠猜——尤其"忠实翻译版"和"中文播客版"的差别不解释就无从判断。

建议：每个选项下方加一行说明 + 时长预期（如"忠实翻译版 · 保留全部细节 · 约等于原片长"、"精华浓缩版 · 只留核心观点 · 约原片的 1/3"）。可复用 `CONCEPTS.md` 的措辞。

预期效果：减少"选错模式 → 重新生成"的往返；把差异化直接呈现在决策点。

### 界面清晰度

**F3 · 未知任务状态默认渲染成"成功"绿点（优先级：中，代码级确认）**

`src/web/static/index.html:1426`：

```javascript
const statusDot = STATUS_CLASS[t.status] || 'status-dot-done';
```

未知状态的兜底值是"完成"。实测历史抽屉里，`6W4qIuUfR8g`、`MHPGeQD8TvI`、`test_recovery...` 这些**没有任何产物**的条目，左侧都显示绿点，右侧还挂着"中文播客版"标签。

默认值语义选错了：未知应当中性（灰），而不是成功（绿）。

建议：兜底改为中性灰；同时按 `variants` 中是否存在 `status === 'done'` 且有 `audio_zh_path` 来决定是否显示可下载标签。

预期效果：历史列表不再出现"看起来已完成、点进去没东西"的条目。

**F4 · 标题缺失时直接暴露 video_id；标题被双重截断（优先级：中）**

- `index.html:1424`：`const title = t.title_original || t.video_id;` → 标题为空时把 `6W4qIuUfR8g` 这种技术 ID 直接展示给用户。
- `index.html:1425`：JS 先截到 40 字符加省略号，CSS 又叠加 `text-overflow: ellipsis`，实测把 `Rick Astley - Never Gonna Give You Up (Official Video)...` 显示成 **`R...`**。

建议：标题缺失时显示"（标题未知）· <缩略 ID>"而不是裸 ID；只保留一层截断（交给 CSS），并加 `title` 属性让悬停可看全称。

预期效果：列表可读性提升，消除 `R...` 这类信息量为零的显示。

**F5 · 进度区同时显示"处理中"和"处理失败"（优先级：中）**

实测失败瞬间的截图里，同一个页面同时出现：
- 卡片标题 `⏳ 处理中`
- 状态徽章 `处理中...`
- 时间线里"转写"已标红 `失败`
- 下方另有一张 `❌ 处理失败` 卡片

用户的疑问是"到底是在跑还是失败了"。

建议：收敛为单一状态来源。失败时把卡片标题与徽章一并切换为失败态（或失败后直接隐藏进度卡片，只留失败卡片 + 时间线作为诊断信息）。

预期效果：消除状态自相矛盾，用户不需要自己仲裁哪个是真的。

**F6 · 时间线是产品亮点，但缺耗时信息（优先级：低）**

六阶段时间线（下载/转写/清洗/翻译/TTS/合并）带状态图标、"从此阶段重试"按钮——这是很好的设计，失败时用户知道坏在哪、能精准重试。但实时视图里各阶段**没有显示耗时**（数据层已有 `duration_s`，历史/轮询路径会用到）。

建议：阶段完成时在右侧显示耗时；`STRATEGY.md` 的指标 #2 是"生成耗时"，把耗时显性化同时也服务于优化。

预期效果：用户能感知瓶颈在哪（通常会是转写或 TTS），也便于判断"5–15 分钟"的提示是否准确。

### 错误提示

**F7 · 内部异常原文直接展示给用户（优先级：高，实测复现）**

真实运行（浏览器实测）中错误卡片的内容是：

```
❌ 处理失败
No module named 'faster_whisper'
```

纯英文技术异常，无中文解释、无下一步动作。用户看到的是内部依赖名，无从处理。而且这条信息**在同一页出现两次**（时间线内联 + 失败卡片）。

根因在 `src/pipeline/orchestrator.py:770`：`error_msg = str(e)` 直接落库并透传到 UI，中间没有分类/映射层。

建议：加一层异常映射，把已知失败模式翻译成"原因 + 下一步"：

| 异常特征 | 建议文案 |
|---|---|
| `ModuleNotFoundError` | 运行环境缺少依赖，请在项目目录执行 `pip install -e .` 后重启服务 |
| `未配置翻译 API Key` | （已有良好文案，保持） |
| 网络/超时 | 网络连接失败，请检查代理设置（当前代理：`<config.network.proxy>`）后重试 |
| 兜底 | 显示"处理失败" + 折叠的原始错误（`<details>`），默认收起 |

预期效果：把"用户看不懂的开发者异常"变成"用户知道下一步做什么"。

**F8 · 模型字段强制前置，接口不可用时彻底卡死（优先级：高，实测确认）**

`validateForm()` 要求 `modelSelect.value` 非空，而该下拉**只能**通过点击"查询模型"填充（实测确认：`optionCount: 1`、无自由文本输入框）。因此当 `/api/models` 不可用时——

- 用户的 API 端点不支持 `/models`（很多第三方中转/自建网关不支持）
- 或网络抖动、密钥无列举权限

——**用户完全无法提交任务**，且错误提示是 `请先点击「查询模型」获取可用模型列表`，用户点了也没用。

对比：TTS 的"模型"是**自由文本框**（`mimo-v2.5-tts`）。同一页面上两个"模型"字段行为不一致。

建议：LLM 模型字段改为"下拉 + 可手动输入"的组合（或在查询失败时自动降级为文本框），与 TTS 侧保持一致；placeholder 给出默认值 `deepseek-chat`。

预期效果：解除硬阻塞，把"查询模型"从必经步骤降级为便利功能。

**F9 · 校验提示顺序与用户填写顺序不一致（优先级：低）**

实测同时触发三个错误时，从上到下的视觉顺序是：LLM API Key → 模型 → YouTube 链接（DOM 顺序），而用户的心理顺序是 链接 → 密钥。代码里聚焦的是 URL（正确），但用户第一眼看到的是最上面的密钥错误。

顺带一提：**字段级校验本身做得很好**——红框 + 行内红字 + 自动聚焦第一个错误，这是产品优点，建议保留并推广到其他表单。

建议：调整错误消息的展示顺序与聚焦顺序一致（都按"链接 → 密钥 → 模型"）。

预期效果：用户按从上到下的顺序就能依次修完，不用来回扫视。

### 操作效率

**F10 · 历史抽屉缺少状态与失败原因（优先级：中）**

实测抽屉里每行只有：状态圆点、标题/ID、模式标签、日期、下载、删除。**没有失败状态标识，也没有失败原因**——历史里失败过的条目和成功的看起来一样（都是绿点，见 F3）。

建议：行内增加状态文字（已完成 / 失败 / 处理中）；失败行展示截断后的失败原因，悬停看全文。

预期效果：不用逐个点开就知道哪次需要重试，配合"从此阶段重试"形成闭环。

**F11 · 删除是破坏性操作，但确认方式不统一（优先级：中）**

- 单模式删除用了原生 `confirm()`
- 删除失败用原生 `alert()`（`index.html:1496, 1528, 1532`）

页面其余部分都是自绘的卡片/弹窗，只有这里弹系统对话框，体验割裂；且 `confirm()` 无法说明"哪些数据会被保留"（虽然文案写了）。

建议：统一改用已有的 modal 组件；确认框里明确列出将被删除的文件/记录。

预期效果：破坏性操作的可理解性与一致性提升，降低误删。

**F12 · 缺少整体进度与剩余时间预估（优先级：中）**

进度区有阶段级百分比（如 `TTS 合成中... (18/49)`）和已用时间，但**没有整体进度**和**剩余时间预估**。表单下方写死"处理通常需要 5–15 分钟"。

这与我在上一份时长预算方案里的分析直接相关：项目已有语速常数可标定（实测 5.66 字/秒，误差 <1%），能在翻译完成后、TTS 开始前**零成本预测产出时长**。

建议：进入 TTS 阶段后显示"预计还需 X 分钟 / 预计产出 Y 分钟音频"，并用历史同类视频的实际耗时做校正。

预期效果：直接服务 `STRATEGY.md` 的"当天提交当天听"判断；减少用户因为不确定而反复刷新。

**F13 · 测试残留数据出现在用户历史里（优先级：低，但应立即清理）**

实测历史抽屉里有一条 `test_recovery_001`——测试数据写进了正式库。同时 `6W4qIuUfR8g`、`MHPGeQD8TvI` 是无标题、无产物的残留记录。

建议：清理库中测试与残留记录；测试改用独立的临时数据库路径（`db_path` 可通过配置覆盖），避免再污染 `db/tasks.db`。

预期效果：历史列表只出现真实任务。

---

## 优先级汇总

| 优先级 | 编号 | 问题 | 类型 |
|---|---|---|---|
| 高 | F7 | 内部异常原文直接展示 | 错误提示 |
| 高 | F8 | 模型字段阻塞提交 | 错误提示 / 交互 |
| 高 | F1 | 配置区压住核心动作 | 交互流程 |
| 高 | F2 | 三种模式零解释 | 界面清晰度 |
| 中 | F5 | 同时显示"处理中"和"失败" | 界面清晰度 |
| 中 | F3 | 未知状态默认显示成功 | 界面清晰度 |
| 中 | F4 | 裸 ID 与双重截断 | 界面清晰度 |
| 中 | F10 | 历史缺状态与失败原因 | 操作效率 |
| 中 | F11 | 删除确认不统一 | 操作效率 |
| 中 | F12 | 无整体进度与剩余预估 | 操作效率 |
| 低 | F6 | 阶段无耗时 | 界面清晰度 |
| 低 | F9 | 校验提示顺序 | 错误提示 |
| 低 | F13 | 测试残留数据 | 操作效率 |

---

## 本次运行的副作用

实际运行产品时提交了一次真实任务，在产品里留下了记录：**`dQw4w9WgXcQ` 的 `faithful` 变体，状态 `failed`，错误 `No module named 'faster_whisper'`**。

这条记录不反映产品缺陷，只反映我这边没装 `faster-whisper`。需要的话可以清掉：

```bash
curl -X DELETE http://127.0.0.1:8080/api/tasks/dQw4w9WgXcQ/faithful
```

另有测试遗留的 `.pytest_tmp/` 目录（项目根下），删除时会触发批量删除保护，建议手动清理。

---

## 变更清单

| 文件 | 变更 |
|---|---|
| `src/tts/cleaner.py` | 下划线不再被删除；补注释说明为何不做 `__强调__` 还原 |
| `src/audio/merger.py` | 新增 `_probe_durations` / `_group_by_duration` / `_group_minutes` / `_estimate_from_size`；拆集改为按时长贪心；删除过时的 `_estimate_duration` |
| `src/web/server.py` | 状态接口错误信息不再跨变体泄漏；新增 `_part_sort_key` 自然排序；新增 `import re` |
| `deploy/video2listener.service` | `ExecStart` 修正为 `src.web.server:app`；环境变量改为 `EnvironmentFile` |
| `deploy/setup.sh` | 虚拟环境统一 `.venv`；模块名修正；环境变量提示更正 |
| `deploy/env.example` | **新增**，列出实际会被读取的 3 个变量 |
| `tests/test_tts.py` | +3 回归测试 |
| `tests/test_audio.py` | +4 回归测试 |
| `tests/test_web.py` | +2 回归测试 |

**测试结果**：`106 passed`（修复前 97）。

> 注：在受限沙箱中运行测试需加 `--basetemp=./.pytest_tmp`，因为 pytest 默认临时目录不可写。这是环境限制，与代码无关，未写入仓库配置。
