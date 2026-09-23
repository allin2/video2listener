---
title: 开源生态调研 — 「YouTube 英文口播 → 中文 MP3 播客」是否有同类项目
date: 2026-09-14
scope: 开源项目与自托管方案
verdict: 无完全等价项目；能力被拆散在三条互不重叠的路线里
---

# 开源生态调研：YouTube → 中文播客

## 0. 一句话结论

**没有找到完全覆盖 video2listener 需求的开源项目。** 相关能力散落在三条**互不重叠**的路线里：

| 路线 | 覆盖 | 代表 | 缺的那一块 |
|---|---|---|---|
| A. 视频翻译/配音 | 下载 → ASR → LLM 翻译 → TTS | VideoLingo、pyVideoTrans、Voice-Pro | 不做播客式内容组织、不出订阅源 |
| B. 视频 → 播客 RSS | 订阅 → 自动同步 → MP3 → 播客分发 | PigeonPod、Podsync、podcast2 | **完全不翻译**，只是把原音轨转成 MP3 |
| C. 内容 → 播客生成 | 播客脚本 + 中文 TTS | Open Notebook、Podcastfy | 输入是文档不是视频；产出是双人对谈不是忠实翻译 |

**②中文翻译 + ③播客式改写 + ④中文单主播 TTS + ⑤播客分发** 这四项，没有任何一个开源项目同时具备。最接近的两个极端是：

- **VideoLingo**（18.4k★）— 有 ②③④，差 ⑤（且心智是"视频配音"，输出以视频为主）
- **PigeonPod**（1.2k★）— 有 ①⑤，差 ②③④

把这两个拼起来约等于 video2listener 的 70%，但"零边际成本的全本地管线 + 三种翻译模式 + 播客原生音频"这一段，开源界目前是空白。

---

## 1. 需求拆解：六个能力位

把 STRATEGY.md 的目标转成可逐个核对的判据：

| # | 能力 | 判据 |
|---|---|---|
| ① | 视频源获取 | 能从 YouTube 拉音频/字幕，能穿代理 |
| ② | 中文翻译 | 英文口播 → 中文译文，术语/人名处理可配 |
| ③ | 播客式改写 | 不是逐句直译，而是按"听"重组内容（口播化 / 浓缩 / 忠实三态） |
| ④ | 中文单主播 TTS | 稳定输出自然中文语音，非对话体 |
| ⑤ | 播客分发 | 产出 MP3 + RSS/订阅源，能被播客客户端消费 |
| ⑥ | 本地优先 | 无需 GPU 也能跑，零边际成本，可私有部署 |

---

## 2. 路线 A：视频翻译 / 配音（能力 ①②③④ + ⑥部分）

这一路线是最热的，但**目标物是"另一种语言的视频"**，不是播客音频。

| 项目 | Stars | 协议 | 最近提交 | 关键事实 |
|---|---|---|---|---|
| [Huanshere/VideoLingo](https://github.com/Huanshere/VideoLingo) | 18,438 | Apache-2.0 | 2026-09-14 | 12 步管线；中间产物包含 `output/dub.mp3`；v3.0.2 支持 Ollama + Edge-TTS 全免费本地跑；TTS 可选 azure/openai/fish-tts/GPT-SoVITS/edge-tts。**注意：`audio-only flow` 指"输入是纯音频文件时跳过合回视频"（`core/_1_ytdlp.py:142` 的 `is_audio_only_input`），不是"只输出音频"的输出模式** |
| [jianchang512/pyvideotrans](https://github.com/jianchang512/pyvideotrans) | 19,009 | GPL-3.0 | 2026-09-14 | 全链路 + 零样本声音克隆 + **说话人分离（多角色各自配声线）**；GUI/exe/CLI 齐全；翻译可接 DeepSeek/Ollama；CLI 有独立 `--task tts` 可从 SRT 直接出配音 |
| [abus-aikorea/voice-pro](https://github.com/abus-aikorea/voice-pro) | 12,810 | GPL-3.0 | 2026-07-13 | **已公告暂停更新**（团队转做 WeConnect），最后版本 v4.0；集 yt-dlp + Demucs + Whisper + F5-TTS/CosyVoice |
| [krillinai/OpenCreator](https://github.com/krillinai/OpenCreator) | 11,386 | — | — | **原 KrillinAI，已转型**为 creator workspace，不再是纯视频翻译工具 |
| [Kedreamix/Linly-Dubbing](https://github.com/Kedreamix/Linly-Dubbing) | 3,337 | Apache-2.0 | 2025-03-05 | **停更约 18 个月**；含口型同步，依赖重（UVR5 + CosyVoice + Linly-Talker） |
| [R3gm/SoniTranslate](https://github.com/R3gm/SoniTranslate) | 1,413 | Apache-2.0 | 2026-08-29 | Colab 路线最省事，无需本地环境 |
| [shang-zhu/violin](https://github.com/shang-zhu/violin) | 1,060 | MIT | 2026-09-04 | 33 语言配音，可插拔架构（YAML 配置），有 CLI / FastAPI / Claude Code skill 三种形态 |
| [cezarc1/podcast_dub](https://github.com/cezarc1/podcast_dub) | 5 | Apache-2.0 | 2026-09-09 | **架构思想最贴近**：全开源权重、局部本地推理（M 系 Mac 16G 可跑）、Qwen3-ASR + ForcedAligner 短语级对齐、说话人分离、LLM 翻译、克隆 TTS；**输出是 mp4 不是 MP3** |

**这条路线的共同特征**
- 交付物是**视频**（配音 + 字幕压制）
- 心智是"字幕对齐驱动"——分段依据是原视频时间轴，超出槽位就砍文案或拉伸语速；这与"为听而重组内容"是两件事
- 多数默认依赖云端 LLM 或 GPU；VideoLingo 是唯一明确承诺"Ollama + Edge-TTS 零 API 可跑"的
- 都不产出 RSS / 订阅源

### 2.1 源码级对等比较：VideoLingo vs video2listener

管线拓扑两者高度重合（yt-dlp → ASR → LLM 翻译 → TTS → ffmpeg 合并），技术选型也多有重叠。**真正的分野不在阶段划分，而在每个阶段内部的约束条件**——它决定了产物是"一集播客"还是"视频里的一条配音轨"。

| 维度 | VideoLingo | video2listener |
|---|---|---|
| **分段依据** | 原视频**时间轴**（`min_subtitle_duration`=2.5s） | 译文字符数（`max_chars`=200） |
| **段落时长** | 必须塞进原字幕槽位；不足则**延长**时间轴，超出则**调 LLM 砍文案** | 等于中文朗读的自然时长，无槽位约束 |
| **语速控制** | `speed_factor` 1.0/1.2/1.4 拉伸对齐画面 | **不压缩**（`synthesizer.py:267` 注释明确"speed 参数有已知 bug，不传"） |
| **合并音频** | **插静音补齐间隙**，总时长 ≈ 原视频时长（毫秒级对齐） | **直接 concat，零静音、零时间轴映射**，总时长 = 各段之和 |
| **输出规格** | 16 kHz / mono / **64 kbps**（配音轨规格，为后续混音） | 44.1 kHz / mono / **128 kbps**（播客投递规格） |
| **响度标准** | `loudnorm I=-20`（dub bed，给原声留空间） | `loudnorm I=-16`（播客投递标准） |
| **超长处理** | 无概念（视频多长就多长） | **>60 分钟自动拆 `_Part1/_Part2`** |
| **翻译约束** | 字幕级：单行、超时则**砍内容**（`prompts.py:312`：*"editing and optimizing lengthy subtitles that exceed voiceover time"*）；无模式概念 | 三种模式；condensed 主动压到 30–50% 并按主题重组 |
| **分发概念** | 全库 grep `rss\|podcast\|episode` **零命中** | 有 episode / variant 模型（缺 RSS） |

**一句话**：VideoLingo 的分段在**迁就画面**，video2listener 的分段在**服务听觉**。前者产出的是"配好音的视频里那条音轨"，后者产出的是"一集播客"。

**证据位置（可复现）**
- 插静音对齐：`core/_11_merge_audio.py:71-82`
- 16k/64k 配音轨规格：`core/_11_merge_audio.py:42-50, 128`
- 按时间轴合并/延长字幕：`core/_8_1_audio_task.py:103-124`
- 文案为配音时间让步：`core/prompts.py:312`
- audio-only 的真实语义：`core/_1_ytdlp.py:142-149`
- 无播客概念：全库 grep `rss|podcast|episode` 无命中

---

## 3. 路线 B：视频 → 播客 RSS（能力 ①⑤⑥）

**这一路线就是 video2listener 的"分发层"**，它已经把「订阅频道 → 自动追更 → MP3 → 播客客户端」整套做完了，唯一没做的就是翻译。

| 项目 | Stars | 协议 | 最近提交 | 关键事实 |
|---|---|---|---|---|
| [mxpv/podsync](https://github.com/mxpv/podsync) | 1,956 | — | 2026-08-24 | Go 单二进制，最老牌；YouTube / Vimeo；cron 刷新；yt-dlp 自更新；支持 OPML 导出 |
| [aizhimou/pigeon-pod](https://github.com/aizhimou/pigeon-pod) | 1,169 | GPL-3.0 | 2026-08-19 | **功能最完整**：Java/Spring Boot + React；YouTube + B 站；频道/播放列表/单视频三种订阅粒度；token 保护的 RSS；多用户 + 角色权限；本地或 S3 存储；按订阅做关键词/时长过滤与保留策略；失败重试与邮件/webhook 告警；内建播放器；Podcasting 2.0 章节 |
| [madiele/vod2pod-rss](https://github.com/madiele/vod2pod-rss) | 385 | MIT | 2026-08-31 | **不落盘**，实时转码 192k MP3，树莓派 3/4 就能带；也可把普通 RSS 降码率再分发 |
| [yajuhua/podcast2](https://github.com/yajuhua/podcast2) | 201 | — | — | B 站 / YouTube / 干净世界 / girigiri 多源聚合 |
| [nbr23/ydl-podcast](https://pypi.org/project/ydl-podcast/) | — | MIT | 2026-03-28 | PyPI 包，cron 驱动 + 静态目录托管，最轻量的自建方案 |

**值得注意的坑（PigeonPod 实测）**
- YouTube 路径**必须要 Google Data API Key**，否则 API 直接拒绝；B 站路径免 key 但撞风控
- 官方云服务按订阅数收费，免费层只有 1 个订阅额度

**这一路线最大的价值不是代码，是设计**：订阅模型、增量同步、按订阅过滤、保留策略、OPML 导出、失败重试告警——如果 video2listener 要从"单链接提交"走向"频道自动追更出中文集"，这是现成的参照系。

---

## 4. 路线 C：内容 → 播客生成（能力 ③④⑤部分 + ⑥）

**这一路线解决的是"中文播客怎么生成得自然"**，输入是文档而非视频。

| 项目 | Stars | 协议 | 最近提交 | 关键事实 |
|---|---|---|---|---|
| [lfnovo/open-notebook](https://github.com/lfnovo/open-notebook) | 38,776 | MIT | 2026-09-13 | NotebookLM 平替的事实标准；**1–4 位主播**，可自定义人设/语调；完整 REST API；支持 16+ 模型商含 Ollama；有用户实测全本地（Qwen3 + Kokoro-82M ONNX）**15 分钟播客约 20 分钟生成**；来源包含 YouTube 视频 |
| [MODSetter/SurfSense](https://github.com/MODSetter/SurfSense) | 16,139 | — | 2026-09-14 | 多源研究智能体 + 播客生成智能体，可接 YouTube 等外部源 |
| [souzatharsis/podcastfy](https://github.com/souzatharsis/podcastfy) | 6,552 | Apache-2.0 | 2026-05-04 | Python 包/CLI/Web；**输入源包含 YouTube URL**；短视频(2–5min)与长节目(30min+)两种形态；100+ LLM；TTS 支持 OpenAI/Google/ElevenLabs/**Edge-TTS**；多语言 |
| [run-llama/notebookllama](https://github.com/run-llama/notebookllama) | 1,972 | MIT | 2026-03-02 | LlamaIndex 官方教学示范，轻量、适合读源码理解"文档→播客"编排 |

**这一路线与需求的关键错位**：它生成的是**两个 AI 主播的对谈**，把内容"再创作"成对话；而 video2listener 要的是**忠实/浓缩地把一场演讲或访谈本身变成中文口播**。前者是内容生成，后者是内容转译。目标听众的信息完整度诉求完全不同。

---

## 5. 相邻但不同：字幕 / 笔记类

这些常被误认为同类，实际交付物是**文字**：

| 项目 | Stars | 交付物 |
|---|---|---|
| [WEIFENG2333/VideoCaptioner](https://github.com/WEIFENG2333/VideoCaptioner) | 15,983 | 字幕工作台（识别/断句/校正/翻译/压制） |
| [JefferyHcool/BiliNote](https://github.com/JefferyHcool/BiliNote) | 7,313 | 视频 → 结构化笔记 + 时间轴 + 截图 |
| Open-Lyrics / MioSub / Silhouette | — | 字幕翻译与对齐 |

**商业侧同样不在同赛道**：BibiGPT（视频转摘要/转文章，¥9.9/月起）、Podwise（播客结构化笔记，$7.99/月）、Snipd（英文播客高亮）——全部是**文字输出**路线，不产出母语音频。

真正同赛道的商业品只有 NotebookLM 的 Audio Overview（文档 → 对话播客），而它不做视频转译、不做订阅源、也把内容留在 Google 服务器上。

---

## 6. 能力矩阵：空白在哪

| | ① 视频获取 | ② 中文翻译 | ③ 播客式改写 | ④ 中文单主播 TTS | ⑤ 播客分发 | ⑥ 全本地/零边际 |
|---|---|---|---|---|---|---|
| 路线 A（VideoLingo 为代表） | ✅ | ✅ | ⚠️ 配音向切句 | ✅ | ❌ | ✅ 可选 |
| 路线 B（PigeonPod 为代表） | ✅ | ❌ | ❌ | ❌ | ✅ | ✅ |
| 路线 C（Open Notebook 为代表） | ⚠️ 部分 | ⚠️ 生成非翻译 | ✅ 但为对话体 | ✅ | ⚠️ 部分 | ✅ |
| **video2listener** | ✅ | ✅ | ✅ 三模式 | ✅ | 🔶 待建 | ✅ |

🔶 = 当前是"产出 MP3 下载"，还没有订阅源。若要做，路线 B 是完整参照。

---

## 7. 建议：可以复用、不必重造

**已经在复用、决策正确**：yt-dlp、faster-whisper、edge-tts、ffmpeg。

**如果要新增能力，按优先级参考以下实现，不要从零写：**

| 想做的事 | 抄谁 | 具体抄什么 |
|---|---|---|
| 多说话人视频（访谈/对谈）按角色配音 | pyVideoTrans、podcast_dub | 说话人分离 + 每个角色独立声线映射；podcast_dub 的 ForcedAligner 短语级时间戳对齐思路 |
| 提升翻译质量 | VideoLingo | NLP 断句 + 术语表（可人工暂停校对）+ "翻译-反思-改写"三步法——对 faithful 模式有直接参考价值 |
| 频道自动追更 + RSS | PigeonPod | 订阅模型、增量同步、按订阅过滤/保留策略、OPML 导出、失败告警 |
| 长音频分段合成的编排 | VideoLingo `_10_gen_audio.py` / `_11_merge_audio.py` | 分段 TTS + 响度归一化（loudnorm）后合并的工程细节 |
| 中文播客脚本的 TTS 编排 | open-notebook、podcastfy | 多音色编排与 REST 接口设计 |

**要守住、不要被开源方案稀释的差异化**：
1. **三种翻译模式**（podcast / faithful / condensed）——开源方案里没有任何一个做模式化输出
2. **播客原生的内容组织**——不是字幕对齐，是为"听"重组信息
3. **零边际成本的全本地管线**——无 GPU、无 Google API Key、无按量计费
4. **Audit 质量元数据**——忠实模式的零成本可验证性

---

## 8. 需要盯的信号

| 项目 | 什么信号意味着正面竞争 |
|---|---|
| **VideoLingo** | 18.4k★ + 活跃 + 技术栈与本项目高度重合 + 出厂就有 `dub.mp3`。**威胁点不是"它现在就等于我们"，而是"它离加一个音频输出开关只差一个勾选框"**：一旦它把分段依据从时间轴切成文本长度、输出规格放开到 44.1kHz/128kbps，就是正面竞争。最高优先盯 |
| **open-notebook** | 38.8k★ + MIT + 完整 REST API；若把"YouTube 视频作为可翻译源"补齐，会从另一侧靠拢 |
| PigeonPod / Podsync | 若开始接 LLM 翻译节点，会补掉路线 B 唯一的缺口 |
| ~~KrillinAI~~ | 已转型 OpenCreator，退出同类竞争，不必再盯 |
| ~~Voice-Pro~~ | 已停更，不要作为依赖 |

---

## 9. 数据来源

- GitHub REST API（star / 协议 / 最近提交，采集时间 2026-09-14）
- 各项目 README 与源码（VideoLingo 整仓库 tarball 源码级审阅：`core/_1_ytdlp.py`、`_8_1_audio_task.py`、`_11_merge_audio.py`、`_12_dub_to_vid.py`、`core/prompts.py`、`config.yaml`；podcast_dub 管线说明）
- 自托管实测与评测文章：techmoon.xyz（VideoLingo / PigeonPod 实测）、videodubbing.com（开源配音工具横评 2026）、selfhosting.sh（自托管播客托管横评）
- 中文项目合集：[JuneYaooo/awesome-ai-media-cn](https://github.com/JuneYaooo/awesome-ai-media-cn)

> **勘误（2026-09-14 修订）**：本报告初版将 VideoLingo 的 `audio-only flow` 理解为"可只输出音频"，据此低估了两者的差距。经整仓库源码核对，该标识的真实语义是 **"输入为纯音频文件时跳过合回视频"**（`core/_1_ytdlp.py:142-149`），并非输出模式。已补充 §2.1 源码级对等比较，并下调该项目的同类程度。
>
> star 数与提交时间为 2026-09-14 快照。§2.1 中的结论均可在上述源码行号处复现，非推断。
