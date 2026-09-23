---
title: 测试验证报告
date: 2026-09-15
scope: 单元测试 + 项目自带验收门禁 + 服务级冒烟
verdict: 单元测试全通过；验收门禁结构性永远失败（已定位根因，需决策）；另修复 2 个缺陷
---

# 测试验证报告

## 结论摘要

| 验证项 | 结果 |
|---|---|
| 单元测试 | ✅ **108 passed**（本次新增 2 个回归测试） |
| 项目自带验收门禁 | ❌ **结构性永远失败** —— `validation_passed` 恒为 0，`--strict` 恒退出 1 |
| 服务级冒烟 | ✅ 首页/健康/错误路径/状态接口全部符合预期 |
| 断言有效性 | ✅ 2 个新回归测试均经"回退修复"验证确实能抓到缺陷 |
| 真实运行验证 | ✅ T2 修复走 UI 同路径、重启后纯读 DB 复验通过 |

本轮**新修复 2 个缺陷**，**新发现 3 个问题**（其中 2 个需你决策方向）。

---

## 一、本轮修复的缺陷

### T1 · 验收脚本裸调 ffprobe，直接崩溃（已修）

**现象**
项目自带的验收脚本无法运行：

```
FileNotFoundError: [Errno 2] No such file or directory: 'ffprobe'
```

**复现步骤**
```bash
.venv/bin/python scripts/evaluate_mode_outputs.py --strict
```

**根本原因**
`scripts/evaluate_mode_outputs.py:46` 调用裸 `ffprobe`。而 ffprobe 实际装在 `/opt/homebrew/bin`，**不在 PATH 上**（launchd、CI、未导出 Homebrew PATH 的 shell 都是这种情况）。

对比：`src/audio/merger.py` 早就为此写了 `_resolve_media_tool()`，在 4 处使用，注释明确写着"兼容 launchd 缺少 Homebrew PATH 的环境"。**生产代码解决了这个问题，验收脚本却没有**——同一个失败模式，一处防住、一处漏掉。

**修复方案**
让验收脚本复用生产代码的解析器，避免两套逻辑再次分叉：

```python
from src.audio.merger import _resolve_media_tool
...
_resolve_media_tool("ffprobe"), "-v", "error", ...
```

**验证**
脚本从"崩溃无输出"变为"正常输出完整指标 JSON"。

---

### T2 · 失败只写共享行，历史列表把失败显示成 new（已修）

**现象**
同一个变体，两个接口说法不一致：

```
/api/status/dQw4w9WgXcQ/faithful  →  status=failed   error_message="No module named 'faster_whisper'"
/api/tasks（历史列表）            →  faithful: new    （无错误信息）
```

历史抽屉里这条变体显示为 `new`（绿点），而它其实已经失败了。

**复现步骤**
1. 让某视频的共享阶段停在 `metadata_fetched`（例如缺 `transcript_clean.txt`）
2. 对它的一个新模式发起处理，并让转写阶段抛异常
3. 分别请求 `/api/status/<id>/<mode>` 与 `/api/tasks`，比对同一变体的状态

**根本原因**
`src/pipeline/orchestrator.py` 失败分支用的是 `elif`：

```python
if shared_status != TaskStatus.TEXT_READY:
    db.update_status(video_id, FAILED, error_message=error_msg)     # 只写共享 episode 行
elif not atomic_regeneration:
    db.update_variant_status(video_id, mode, FAILED, ...)           # 永远走不到
```

共享阶段未达 `TEXT_READY` 时，`elif` 分支被跳过，**变体行停在 `new`**。历史列表（`/api/tasks`）读的是变体行状态，于是显示 `new`；而 `/api/status` 里有一段补救逻辑 `if source_status == "failed" and db_status == "new": db_status = "failed"`，靠 episode 行"合成"出 failed —— 这个补救本身就是这个不一致的化石证据。

**修复方案**
失败分支改为：变体行无论如何都记下自己的失败，但**保留一个精确的例外**——原子重跑且旧变体确实可用时，刻意不覆盖旧成品。

```python
previous = db.get_variant(video_id, mode)
previous_is_usable = bool(
    previous
    and previous.get("status") == TaskStatus.DONE.value
    and previous.get("audio_zh_path")
)
if not (atomic_regeneration and previous_is_usable):
    db.update_variant_status(video_id, mode, FAILED, error_message=error_msg)
```

`update_variant_status` 已内置"无行则 upsert"的兜底，因此对全新视频也安全。

**⚠️ 这个修复经过两轮才到位（值得记录的过程）**

第一版我只把 `elif` 改成 `if`，**并在单元测试上通过了**。但拿到真实数据上验证时发现变体行**仍然是 `new`** —— 因为 UI「重新生成此模式」走的是 `action=regenerate_variant` → `force=True`，且变体行已存在 → `atomic_regeneration=True`，第一版的守卫直接跳过了写入。

**是运行时验证抓出了这个不完整的修复**，单元测试没有覆盖到这条分支（我第一版测试用的是不带 `force` 的路径）。第二版才引入 `previous_is_usable` 判据，并补了对应测试。

**验证（三层）**

1. **单元测试**：新增 2 个回归测试
   - `test_variant_failure_is_recorded_on_variant_row_not_only_episode`（非原子路径）
   - `test_force_regeneration_of_empty_variant_records_failure`（原子重跑 + 旧变体无可用产物）

2. **反向验证**：把两版修复分别临时回退，对应测试都确实失败（`1 failed`），证明断言不是恒真。

3. **真实运行验证**（走 UI 的同一路径，重启服务清空内存态后纯读 DB）：

   | 时刻 | DB 中 faithful 变体行 |
   |---|---|
   | 触发前 | `status='new'`，无错误 |
   | 触发重跑失败后 | `status='failed'`，`error='No module named 'faster_whisper''` |

   重启后两个接口一致（不再依赖内存态）：
   ```
   /api/tasks   →  faithful  status=failed  error="No module named 'faster_whisper'"
   /api/status  →  faithful  status=failed  error="No module named 'faster_whisper'"
   /api/status  →  podcast   status=done    error=''
   ```

   副作用：历史列表现在也带上了失败原因（数据层面解决了上一轮报告 F10 的一部分：历史里看不到失败原因）。

**已有行为保持不变**：`test_failed_force_regeneration_preserves_previous_variant` 仍通过——旧变体是 `done` 且有产物时，失败不会覆盖它。

---

## 二、验收门禁：结构性永远失败（需你决策）

这是本轮最重要的发现。

### 现象

```
$ .venv/bin/python scripts/evaluate_mode_outputs.py --strict
$ echo $?
1
```

全部 20+ 项指标中，**只有一项失败**，且失败原因与翻译质量无关：

```
validation_passed                    = 0
quality_failure_count                = 1
quality_failures                     = ['忠实版缺少通过的双向语义审计']
faithful_semantic_audit_passed       = 0
faithful_audit_status                = unknown
```

其余全部通过：

| 指标 | 实测 | 门槛 | 结果 |
|---|---|---|---|
| `outputs_exist` | 1 | =1 | ✅ |
| `translation_audits_complete` | 1 | =1 | ✅ |
| `faithful_long_english_runs` | 0 | =0 | ✅ |
| `podcast_long_english_runs` | 0 | =0 | ✅ |
| `condensed_long_english_runs` | 0 | =0 | ✅ |
| `faithful_han_per_source_word` | 1.39 | ≥1.0 | ✅ |
| `max_mode_similarity` | 0.6368 | <0.9 | ✅ |
| `condensed_to_faithful_duration_ratio` | 0.258 | 0.25–0.55 | ✅（余量 0.008） |
| `podcast_to_faithful_duration_ratio` | 1.0085 | 0.5–1.2 | ✅ |
| `faithful_numeric_recall` | 0.9 | — | 记录 |

### 根本原因

门禁要求 `faithful_semantic_audit_passed == 1`，而它需要审计 JSON 里存在 `semantic_audit.passed` / `checked_segments` / `failed_segments`（`evaluate_mode_outputs.py:143-154`）。

但审计产物**结构上不可能包含这些字段**。`src/translation/client.py:131 _build_translation_audit()` 只产出 7 个键：

```
version, mode, source_sha256, source_segment_count,
translation_segment_count, all_segments_present, segments[]
```

实测忠实版的 `translation_audit.json` 确实只有这 7 个键，**没有 `semantic_audit`，也没有 `quality_status`**。

所以：
```
audit.get("quality_status") or semantic.get("status") or "unknown"
→ "unknown" → faithful_semantic_audit_passed = 0 → validation_passed = 0
```
**无论翻译质量多好，这个门禁永远不可能通过。** 一个永不通过的门禁提供零信号。

### 为什么会这样：两份同日文档方向相反

我在仓库里找到两份 2026-07-05 的文档，对"语义审计"给出**相反**的结论：

| 文档 | 主张 |
|---|---|
| `docs/solutions/best-practices/avoid-blocking-llm-audit-loops.md`（best-practice，含 severity/component 元数据） | **移除**阻塞式语义审计。理由是它让 API 调用翻倍却无质量收益，改为"提示词约束 + 确定性门禁 + 非阻塞日志抽样"三层策略，并强调"缺失的数字记录为**证据，不作为阻塞失败**" |
| `docs/reports/2026-07-05-faithful-audit-validation.html` | **细化**语义审计（审计截断改为拆分、技术失败与内容缺陷分离、degraded 允许下载但严格验收必须失败） |
| `docs/plans/2026-07-05-001-fix-faithful-translation-audit-plan.html` | 被 best-practice 文档列为"另一个方案：细化而非移除" |

**代码最终走了"移除"**：`_audit_faithful_translation_async` 已不在 `src/` 中（只残留在历史日志 `data/server.log` 里），`_build_translation_audit` 也不再产出语义审计字段。

**但"细化"方案的验收要求留了下来** —— 验收脚本仍要求语义审计通过。这与已落地的设计直接矛盾。

### 两个候选方向（请选择）

**方向 A：以代码现状为准（推荐）**
验收脚本对齐已落地的三层策略：删除对已移除的 `semantic_audit` 的依赖，把确定性指标（审计完整性、无连续英文、汉字/词比、数字召回）作为门禁，把语义审计状态降级为**证据指标**（与 best-practice 文档"记录为证据，不作为阻塞失败"一致）。

**方向 B：以验收报告为准**
恢复语义审计：让管线重新产出 `semantic_audit`（分段检查 + 局部修复），写进审计产物，门禁随之有意义。代价是恢复 best-practice 文档所批评的成本（faithful 模式 API 调用约翻倍）。

我倾向 A——因为代码、`CONCEPTS.md` 和 best-practice 文档三者已经一致指向"移除"，只有验收脚本是孤例。但**这是验收标准的变更，我不擅自决定**。

---

## 三、另有 3 个发现（2 个需决策，1 个已确认）

### T3 · faithful 的 `audit_status` 恒为 "passed"（与 T2 同源，需决策）

`src/pipeline/orchestrator.py:564-566`：

```python
audit_status = translation_audit.get(
    "quality_status", "not_applicable" if mode != "faithful" else "passed"
)
```

`quality_status` **从未被写进审计产物**（见 T2 的键列表），所以这个读取永远落到默认值：faithful 恒为 `"passed"`，其他模式恒为 `"not_applicable"`。

实测佐证（`/api/tasks`）：`STH929HARLo` 的 `faithful:done/passed`、`podcast:done/not_applicable`。

后果：
- **前端恒显示"✅ 忠实翻译质量审计完全通过"**（`index.html:1094-1096`）——这是一个**没有执行任何检查就给出的通过声明**，与我上一轮建议的"零成本可验证性"正好相反。
- `audit_status == "degraded"` 分支（以及对应的"质量审计降级"提示、`audit_message` 计算）是**不可达的死代码**。

这个问题的修法与 T2 的方向绑定：选 A 则应把 `audit_status` 改为由确定性指标驱动（或明确标注为"未执行语义审计"），选 B 则恢复真实状态。

### T4 · "译文未被改动"校验形同虚设（与 T2 同源，需决策）

`evaluate_mode_outputs.py:129-133` 想校验审计记录与当前 `script_zh.txt` 是否一致：

```python
translation_hash_matches = (
    not audit.get("translation_sha256")          # ← 字段从不产出，左边恒为 True
    or audit.get("translation_sha256") == hashlib.sha256(current_script...).hexdigest()
)
```

审计产物**没有**顶层 `translation_sha256`（只有逐段的 `translation_sha256`）。`not None` 恒为 True，因此这个条件是恒真的——**"译文是否被改动"从未真正被校验过**，虽然代码看起来在校验。

修法同样取决于方向：选 A 就删掉这个空洞检查（或改成逐段校验，因为逐段哈希是有的）；选 B 则让产物补上顶层哈希。

### T5 · 待验证项仍未验证（非缺陷，是事实）

`docs/reports/2026-07-05-faithful-audit-validation.html` 里列出的真实验收条件，**至今仍无法自动验证**：

> "浏览器本地保存的 API Key 不会写入服务器或环境变量，自动化进程无法代替用户发起三次真实翻译。因此 180 秒中位数和三模式严格输出验收尚不能宣告通过。"

本轮同样受此限制：**没有 DeepSeek / MiMo 密钥，翻译之后的环节没有被实际执行**。所以：
- "翻译开始至语义审计结束耗时的三次中位数 ≤180 秒" —— **未验证**
- "进度页依次显示分段翻译、确定性门禁、语义审计、局部修复" —— **未验证**（其中"语义审计"环节按代码现状已不存在，需与 T2 一并澄清）

---

## 四、服务级冒烟结果

| 检查项 | 结果 |
|---|---|
| `GET /health` | ✅ 200 |
| `GET /` | ✅ 200，71,165 字节 |
| `POST /api/models` 无 key | ✅ 400 `请输入 API Key` |
| `POST /api/process` 无效链接 | ✅ 400，含支持的格式列表 |
| `POST /api/process` 非法模式 | ✅ 400，列出可选模式 |
| D4 跨变体错误泄漏复验 | ✅ `podcast: done / error_message=None`；`faithful: failed / 带自身错误` |

**上一轮修复在本次验证中全部保持生效**（D4 复验通过，且历史与状态的矛盾已由本轮 T2 修复）。

---

## 五、变更清单（本轮）

| 文件 | 变更 |
|---|---|
| `scripts/evaluate_mode_outputs.py` | 复用 `_resolve_media_tool()` 解析 ffprobe，不再裸调用 |
| `src/pipeline/orchestrator.py` | 失败分支记录变体行失败（含"旧变体可用则保留"例外） |
| `tests/test_pipeline.py` | +2 回归测试（均含反向验证） |

**测试结果**：`108 passed`（本轮前 106）。

---

## 六、建议的后续动作

1. **先定 T2 方向**（A 或 B）——它决定 T3、T4 的修法，也决定验收门禁是否能重新产生信号。
2. 定方向后，给验收脚本补一个**自动化测试**。当前 `scripts/` 下没有任何测试覆盖，这正是门禁与设计脱节七周没被发现的原因。建议用一个合成 episode 固件断言"门禁可通过"，并在改坏指标时断言"门禁必须失败"。
3. T5 的两个真实验收项需要你提供密钥或手动触发——这是唯一必须人工的环节。
4. 上一轮遗留的清理项仍在：`dQw4w9WgXcQ` 的 faithful 失败记录、历史里的 `test_recovery_001`、项目根下的 `.pytest_tmp/`。
