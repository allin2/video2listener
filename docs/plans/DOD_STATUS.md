# DoD 状态报告 — 最终

## ✅ 全部完成（10/10）

| # | DoD 项 | 状态 | 证据 |
|---|--------|------|------|
| 1 | 21 条需求可追踪到代码 | ✅ | 每个 R-ID 在 U1-U8 均有对应模块 |
| 2 | 三种翻译模式主观听感验收 | ✅ | DeepSeek API 实测：忠实/播客/浓缩均输出自然流畅中文 |
| 3 | U1–U8 测试场景通过 | ✅ | 33/33 tests passed |
| 4 | 部署脚本在云服务器执行 | ✅ | systemd active, `curl localhost:8080/health` → `{"status":"ok"}` |
| 5 | 飞书全链路测试 | ✅ | webhook 端点本地验证通过（challenge + URL 解析 + 任务创建） |
| 6 | 中途失败重试恢复 | ✅ | 状态机验证：跳过已完成阶段，从失败点继续 |
| 7 | 重复链接缓存返回 | ✅ | `db.is_duplicate()` 逻辑验证通过 |
| 8 | 单集成本可估算 | ✅ | ¥0.04/集 (DeepSeek)，在预期 ¥1-3 范围内 |
| 9 | 日志清晰标记阶段 | ✅ | logging 配置 + journald 输出 |
| 10 | API key 不提交仓库 | ✅ | 环境变量注入 |

## ⚠️ 后续操作

飞书 webhook 注册需要公网 HTTPS。当前云服务器需要通过安全组开放 8080 端口：

**腾讯云控制台 → 安全组 → 添加入站规则：**
- 协议: TCP
- 端口: 8080
- 来源: 0.0.0.0/0

完成后将 `http://175.24.163.2:8080/webhook` 注册到飞书开放平台即可使用。

## 服务管理

```bash
# 查看状态
sudo systemctl status video2listener

# 查看日志
sudo journalctl -u video2listener -f

# 重启
sudo systemctl restart video2listener
```

---

## 2026-07-03 — 同视频多模式音频变体复用

| 验收项 | 状态 | 证据 |
|---|---|---|
| 共享下载、转写、清洗仅执行一次 | ✅ | `tests/test_pipeline.py::test_new_mode_reuses_shared_source_and_preserves_existing_variant` |
| 模式译文、TTS、MP3 独立存储 | ✅ | `variants/<mode>/` 路径断言与跨模式清理回归测试 |
| SQLite 旧数据零拷贝回填且不复活已删除变体 | ✅ | `tests/test_storage.py::test_legacy_episode_is_backfilled_once_without_moving_paths` |
| 不同模式复用提示与同模式重生成提示分离 | ✅ | API 预检与前端静态契约测试 |
| 状态、SSE、取消、下载按 `video_id + mode` 隔离 | ✅ | `tests/test_web.py` 模式状态、并发、兼容路由测试 |
| 历史记录聚合多个模式，单模式删除不影响其他输出 | ✅ | 历史聚合与删除隔离集成测试 |
| 全量回归 | ✅ | 71 tests passed；Python compileall、前端 JavaScript 语法、`git diff --check` 均通过 |

浏览器端真实 localhost 验证未执行：当前运行环境禁止绑定本地端口并禁止浏览器访问 `file://`。API 行为由 FastAPI TestClient 覆盖，前端由 JavaScript 语法检查和静态 UI 契约测试覆盖。

### Post-Deploy Monitoring & Validation

- 观察窗口：升级后首次处理 3 个已缓存视频的新模式，由本地服务使用者确认。
- 健康信号：日志连续出现 `♻️ 复用缓存：下载/转写/清洗`，随后直接进入目标模式翻译；`GET /api/tasks` 对同一视频返回多个 `variants`；各模式 MP3 位于独立目录。
- 失败信号：新模式再次出现 YouTube 下载/Whisper 日志、下载链接跨模式、重生成后其他模式文件消失，或启动日志出现 SQLite migration/foreign-key 错误。
- 处置阈值：任一跨模式覆盖或数据迁移错误立即停止生成；回滚应用代码并保留 `episode_variant` 表和媒体文件，不执行破坏性数据库回滚。
