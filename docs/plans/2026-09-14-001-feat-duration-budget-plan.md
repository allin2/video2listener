---
title: 时长预算（Duration Budget）— 从 VideoLingo 学什么、不学什么
date: 2026-09-14
status: 部分实现（2026-09-24：口径定为相对原视频时长；已做 #1 预估器、#4 门禁复用、#6 进度页显示预计时长；#2 重写分支、#3 按时长拆集、#5、#7 未做）
source: 用户提议「应该学习 VideoLingo 里面的时间约束」
---

# 时长预算：从 VideoLingo 学什么、不学什么

## 0. 结论

**学它的「合成前预估 → 预算 → 行动」机制，不学它的「对齐画面」目的。**

VideoLingo 的时间约束是为「音画同步」服务的。本项目没有画面，照搬这套约束会直接对抗 STRATEGY.md 里的三条核心指标。但它的时间约束里有一个部件是我们完全没有的：**在调用 TTS 之前就预估出时长**。这一件值得偷，且成本几乎为零。

---

## 1. VideoLingo 的时间约束是为谁服务的

约束的目的决定了它能否移植。它的三个机制都指向同一件事——音频必须塞回画面：

| 机制 | 代码位置 | 目的 |
|---|---|---|
| 空隙插静音补齐 | `core/_11_merge_audio.py:71-82` | 让输出总时长恒等于原视频时长 |
| 超出槽位时调 LLM 砍文案 | `core/_8_1_audio_task.py:18-44` + `core/prompts.py:312` | 让译文变短以塞进字幕槽位 |
| 按槽位拉伸语速 1.0–1.4 | `config.yaml: speed_factor` | 让配音与口型/字幕对齐 |

`core/prompts.py:312` 的原文最能说明设计意图：*"You are a professional subtitle editor, editing and optimizing lengthy subtitles **that exceed voiceover time** before handing them to voice actors."* —— 文案为配音时长让步。

---

## 2. 照搬会伤到什么（明确不要做的四条）

| 不要做 | 会伤害什么 |
|---|---|
| 用 LLM 砍文案来适配时长 | **信息完整度**（核心指标 #3）。这是 condensed 模式**按设计**要做的事，不该变成所有模式**因技术限制**被迫做的事 |
| 段间插静音做时间轴对齐 | 播客中段出现静音是缺陷，不是特性。听感上是「卡住了」 |
| 按槽位拉伸语速 | **中文自然度**（核心指标 #3）。且本项目 MiMo 的 speed 参数有已知 bug（`synthesizer.py:267`） |
| 把输出总时长钉死等于源时长 | condensed 必须**更短**（这是它的产品定义）；faithful 允许**更长**——实测 faithful / 源视频 = **1.091**，本来就该更长 |

---

## 3. 但本项目其实已经有「时长约束」了

这一条容易漏掉：门禁已经存在，只是不在管道里。

`scripts/evaluate_mode_outputs.py:76-81`：

```python
condensed_ratio = float(metrics.get("condensed_to_faithful_duration_ratio", 0))
if not 0.25 <= condensed_ratio <= 0.55:
    failures.append("浓缩版时长不在忠实版的 25%–55% 范围")
podcast_ratio = float(metrics.get("podcast_to_faithful_duration_ratio", 0))
if not 0.5 <= podcast_ratio <= 1.2:
    failures.append("播客版时长相对忠实版异常")
```

**所以缺的不是「时长约束」这个概念，而是它的位置和时机：**

| | 现状 | 缺口 |
|---|---|---|
| 在哪里 | 离线评估脚本 | 不在管道内 |
| 什么时候 | TTS 跑完、MP3 已生成之后 | 无法事前干预 |
| 谁来跑 | 需人工手动执行 | 平时不跑就不知道超标 |

---

## 4. 实测现状（2026-09-14）

对本仓库唯一三种模式齐全的成品（`data/9_Free_AI_Skills_That_Feel_Like_Cheat_Codes`）实测：

| 模式 | 时长 | 字符数 | 字/秒 |
|---|---|---|---|
| 源视频 | 29.2 分钟 | — | — |
| faithful | 31.9 分钟 | 10,779 | 5.63 |
| podcast | 32.2 分钟 | 11,024 | 5.71 |
| condensed | **8.2 分钟** | 2,787 | 5.64 |

| 比率 | 实测 | 门禁 | 余量 |
|---|---|---|---|
| condensed / faithful | **0.258** | 0.25 – 0.55 | **仅 +0.008** |
| podcast / faithful | 1.009 | 0.50 – 1.20 | 充裕 |

**两个值得注意的点：**

1. **condensed 贴着门禁下界跑，余量只有 1.6 个百分点。** 任何模型输出波动、语速变化或源素材风格差异，都可能直接击穿门禁。
2. **condensed 相对源视频只有 28%**，低于 CONCEPTS.md 文档的「30–50%」目标。

> ⚠️ 口径提示：本次 condensed 产物生成于 2026-07-03 23:53，faithful 生成于 2026-07-05 11:34，**并非同一批次**。比率 0.258 是跨批次比较，方向性可信、绝对值需同批重跑确认。

---

## 5. VideoLingo 真正值得偷的一件：合成前时长预估

### 它的实现（比想象中简单）

`core/tts_backend/estimate_duration.py` 是一个**纯启发式**，无模型、无外部服务：

```python
self.duration_params = {'en': 0.225, 'zh': 0.21, ...}   # 每音节秒数
def estimate_duration(self, text, lang=None):
    return self.count_syllables(text, lang) * self.duration_params.get(lang or 'default')
# 中文：len(pinyin(text)) → 每个汉字一个音节
# 即：0.21 秒/字 ≈ 4.76 字/秒
```

`_8_1_audio_task.py:22` 用它做预算比较：`estimate_duration(text, ESTIMATOR) / speed_factor['max']`，超了就砍文案。

### 我们的现状

**完全没有预估。** 全仓库唯一的时长计算是 `src/audio/merger.py:179 _estimate_duration()`，它 ffprobe 的是**已经生成完**的片段文件——属于事后统计，不是预估。

### 我们可以做得比它准

用现成成品标定（3 个模式、2000+ 秒真实音频）：

**MiMo 苏打音色 ≈ 5.66 字/秒**（即 0.177 秒/字）

验证误差：
- faithful：10,779 ÷ 5.66 = 1,904 秒 vs 实际 1,914 秒 → **误差 0.5%**
- condensed：2,787 ÷ 5.66 = 492 秒 vs 实际 494 秒 → **误差 0.3%**

VideoLingo 用的常数是 0.21 秒/字，我们是 0.177 —— 它的常数偏保守约 19%（对它而言是安全方向）。我们因为语速固定（同一音色、同一引擎、未启用变速率），标定反而更准。

### 能解锁什么

| 用途 | 说明 | 对应指标 |
|---|---|---|
| **condensed 从「门禁」升级为「可 steer」** | 在 TTS 之前就知道预估时长，偏离目标时把「当前预估 X 分钟 / 目标 Y 分钟」回给 LLM 重写一次，而不是事后才发现超标 | 信息完整度 |
| **拆集改为按时长** | 见 §6 缺陷 2 | 单集时长上限 |
| **上屏「预计时长」** | 用户提交后即可看到预计产出多长 | 生成耗时体验 |
| **成本零增加** | 纯本地字符数计算，不调 API —— 与本项目既有的「零 API 成本审计」哲学一致 | — |

---

## 6. 顺带发现的三处问题（独立于本议题）

### 缺陷 1：TTS 清洗会吃掉下划线，破坏技术标识符

`src/tts/cleaner.py:34`：

```python
text = re.sub(r"[*_~`\[\]{}|\\]", "", text)
```

字符类里包含 `_`，**所有下划线被删除**。本项目是技术内容播客，而 prompt 明确要求「技术缩写保持英文原样」「专有名词保留英文原名」，下划线在 `snake_case` 标识符、环境变量名、文件名中很常见。

已在真实产物中复现（`data/dQw4w9WgXcQ/variants/podcast/tts_text.txt`）：

```
请设置环境变量 VIDEO2LISTENERDEEPSEEKAPIKEY 后重试
```

原文 `VIDEO2LISTENER_DEEPSEEK_API_KEY` 的下划线被全部删掉，**给用户的指引因此失效**。

### 缺陷 2：超长拆集按片段数平均分，不按时长

`src/audio/merger.py:67-68`：

```python
part_count = int(total_duration_minutes / max_minutes) + 1
segs_per_part = len(segments) // part_count + 1
```

按**片段个数**均分。而片段长度差异很大（一个 200 字片段 vs 一个 15 字片段长度差十倍以上），因此**单个 Part 可能超过 `max_output_minutes=60`**。应按预估时长贪心切分，并优先落在段落/主题边界上。

### 缺陷 3：时长目标的口径不一致

| 出处 | 口径 |
|---|---|
| `CONCEPTS.md` | condensed 压缩到「原内容量」的 30–50% |
| `evaluate_mode_outputs.py` | condensed **时长** / **faithful 时长** ∈ [0.25, 0.55] |

一个相对**源**，一个相对**忠实版**。两者不等价（faithful 本身已是源的 1.091 倍）。需要明确以哪个为预算目标——建议统一为**相对源视频时长**，因为那是用户可感知的基准。

### 附带观察：错误提示会被当成节目合成出来

`data/dQw4w9WgXcQ/variants/podcast/` 的产物内容是：

> 「注意：以下为英文原文（未翻译，因未配置 LLM API Key）请设置环境变量…」

这段错误提示被送进 TTS 合成了一个 41.4 秒的 MP3（字/秒 = 1.76，与正常值 5.66 严重偏离，是本次测量中唯一的异常点）。缺失 API Key 时应直接失败，而不是产出一个「只有一句报错的节目」。

---

## 7. 建议改动（待批，未实现）

| # | 文件 | 改动 | 影响指标 |
|---|---|---|---|
| 1 | 新增 `src/audio/budget.py` | `predict_duration(text) -> float`，常数从真实成品标定（初值 5.66 字/秒），并保留标定脚本以便换音色后重标 | — |
| 2 | `src/pipeline/orchestrator.py` | 翻译后、TTS 前插入预算检查；condensed 偏离目标时带「预估/目标」重写一次（**不砍文案，是重新组织**） | 信息完整度、生成耗时 |
| 3 | `src/audio/merger.py` | 拆集由「按片段数」改为「按预估时长贪心 + 优先段落边界」 | 单集时长上限 |
| 4 | `scripts/evaluate_mode_outputs.py` | 复用同一 predictor，保证预估口径 = 门禁口径 | 一致性 |
| 5 | `src/tts/cleaner.py` | 下划线不再无条件删除（仅删 Markdown 强调用的 `_`，保留标识符内的） | 技术内容保真 |
| 6 | `src/web/static/index.html` | 显示「预计时长」 | 生成耗时体验 |
| 7 | 管道出口 | 缺 API Key 时直接失败，不合成错误提示 | 产物可信度 |

---

## 8. 不建议做的（重申）

- ❌ 用 LLM 砍文案以适配时长
- ❌ 段间插静音做时间轴对齐
- ❌ 按槽位拉伸语速
- ❌ 把输出总时长钉死等于源时长
- ❌ 把 VideoLingo 的 `is_audio_only_input` 式「音频输入」概念引入——那是它的输入侧分支，与本议题无关

---

## 9. 待决策

1. **预算检查是否进管道**（而不是继续只留在离线脚本）？我倾向进——但这会让管道多一个「重写一次」的分支，需确认可接受的成本上限（建议 1 次）。
2. **语速常数用固定值还是按模式/音色分别标定**？实测三模式差异 <1.5%，固定值可能足够，但换 TTS 提供商后需重标。
3. **口径统一到哪个基准**（见缺陷 3）？建议统一为相对源视频时长。
4. 缺陷 1–3 是否单独开 issue 修复，还是并入这一批？

> 本文档为分析产出，**未做任何代码改动**。以上 7 项改动均待批准。
