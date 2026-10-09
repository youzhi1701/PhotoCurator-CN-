# PhotoCurator-CN V2.0 · 断点恢复任务账本

此文件与 [历史工程检查点](docs/archive/PROJECT_PROGRESS.md) / [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md) 配合使用。恢复时必须先核对 GitHub main、PR、Actions、工作流产物的真实状态，不能仅凭本文件判定完成。

## 本轮工作（v2.0.0 Candidate）

| ID | 模块 | 代码目标 | 当前状态 | 验收 |
| --- | --- | --- | --- | --- |
| V2-CORE-01 | 核心筛选 | 全量服务器清晰/模糊/待删除/格式过滤与全局计数 | 代码合并，自动化通过；待用户验证 | `tests/test_cull_paged_review.py` |
| V2-CORE-02 | UI 性能 | Cull 600 卡片滚动窗口、上下批次回溯 | 代码合并，自动化通过；待用户验证 | 浏览器/前端守卫 + 真实大型图库 |
| V2-CORE-03 | 相似分组 | 192 组上限、滚动加载和历史窗口回看 | 代码合并，自动化通过；待用户验证 | 前端守卫 + 手动操作体验 |
| V2-CORE-04 | 离线图库 | 多个历史根目录快速切换丢弃迟到响应 | 代码合并，自动化通过；待用户验证 | `tests/test_frontend_stale_request.py` |
| V2-CORE-05 | 版本/安装器 | v2.0.0 Candidate、Windows EXE、v1.5.0 覆盖升级 | 代码合并，自动化通过；待用户验证 | Source + Windows Candidate |
| V2-QA-01 | 实机大图库 | 4TB、19k+ 原始照片，内存与交互长测 | 待用户实机验收 | 用户实机验证 |
| V2-QA-02 | 存储故障 | HDD/USB 热插拔、换盘符、不同盘复用和掉电恢复 | 尚待实机 | 两块真实盘与重启测试 |
| V2-QA-03 | 格式/智能识别 | 跨相机 RAW/HEIF 实际格式与场景误判集 | 尚待实机和数据集 | 真实格式样本与人工标注 |
| V2-RELEASE-01 | 正式发布 | 数字签名、QA 批准和 Stable Release | 保持阻断，待实盘/签名 | `RELEASE_ACCEPTANCE.md` |

## 2026-10-09 V2.0.0 Candidate 真实验证检查点

- [PR #90](https://github.com/youzhi1701/PhotoCurator-CN-/pull/90) 已 squash 合并 main，提交 `604400728c757b6300a1c58c8038aa4e9e6ed369`。
- [Source syntax + Windows smoke](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37890904679) 的两个 Windows 工作均成功（含真实浏览器 UI 交互烟测、性能回归门禁、合成 19,000 条筛选回归）。
- [Windows EXE / Inno Setup Candidate](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37890904657) 构建成功，真实 Windows 启动/窗口响应、v1.5.0 正式安装器升级与静默卸载留存测试通过。
- 唯一主交付构件 `PhotoCurator-v2.0.0-test-installer`，ZIP 内 `PhotoCurator-Setup-v2.0.0.exe`，安装器 SHA-256 `5102a263d6f811428462818f6b3c2e284a1360fdb3c77bcbf520cda0b672aac5`，Artifacts 截止 2026-10-23 06:01 UTC。
- 未完成人工实盘和数字签名，仅能称 Candidate。用户此轮实际测试反馈积累后再按约定集中修复，不再自动启动另一轮 V2 补丁。

## 中断恢复步骤

1. 查 `main` 真实 HEAD、最新 PR、Actions 和构件，不从早期摘要重做。
2. 对比每个任务的已合并提交和已通过的测试，只继续尚未合并/失败/未实现的事项。
3. 用户未授权时只记录与分析；收到“开始/继续”才执行。
4. 每轮最多生成一个主要用户测试安装包；其余使用 GitHub 内部检查点。
5. 自动化 CI 通过只能标为“自动化已验证”，不能替代真实原盘/Windows/签名 QA。
