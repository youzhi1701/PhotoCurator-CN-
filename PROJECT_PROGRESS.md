# PhotoCurator-CN V2.0 · 实时开发检查点（2026-10-08）

> 此文件记录已经提交并验证的代码工作，以及尚未满足正式发布标准的工作。**不得把候选安装包、自动化测试或 GitHub 合并等同于用户实机验收与正式发布。**
>
> 本轮核对并开发的基线：main `a5184ab5fed7b3c2a1265683261c3173e05e71fb`（PR #45）。每次恢复时必须重新读取 GitHub main 最新 SHA、未合并 PR、CI 和构件；本文件不是实时 API。

## 最新已合并的安全升级

| PR | 修复内容 | 状态与自动化验证 |
| --- | --- | --- |
| [#40](https://github.com/youzhi1701/PhotoCurator-CN-/pull/40) | 未注册文件处理器不能误抢已恢复任务 | 已合并 |
| [#41](https://github.com/youzhi1701/PhotoCurator-CN-/pull/41) | 待删除照片集中复核、二次确认与选择绑定 | 已合并；此前缺乏强制复核凭证已由 #42 修复 |
| [#42](https://github.com/youzhi1701/PhotoCurator-CN-/pull/42) | 批量移入回收站必须具有服务端随机、限时、一次性、绑定本批选择的复核令牌；补缺失/伪造/重放/过期测试 | Source [37796094774](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37796094774) 成功；Windows Candidate [37796094811](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37796094811) 成功 |
| [#43](https://github.com/youzhi1701/PhotoCurator-CN-/pull/43) | 单张文件后台任务先成功入队，再标记待处理；失败时保持原生命周期 | Source [37796984335](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37796984335) 成功；Windows Candidate [37796984303](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37796984303) 成功 |
| [#44](https://github.com/youzhi1701/PhotoCurator-CN-/pull/44) | 回收站永久删除复核凭证、批量恢复及删除原子入队、离线回收站历史记录及清单不丢失、离线文件禁止处理 | Source [37798582293](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37798582293) 成功；Windows Candidate [37798582672](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37798582672) 成功 |
| [#45](https://github.com/youzhi1701/PhotoCurator-CN-/pull/45) | 直接永久删除增加服务端一次性凭证，并绑定文件真实路径、设备/文件身份、大小、mtime；文件被替换、凭证重放时拒绝操作 | Source [37799538474](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37799538474) 成功；Windows Candidate [37799538559](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37799538559) 成功 |

以上状态只代表相应代码和已有自动化测试通过；**没有执行 4TB 外接机械硬盘、19,000 张以上真实图库、全格式 RAW/HEIF 样本以及多版本覆盖安装等完整实机验收**。本轮没有将候选包发布成 Stable。

## V2.0 八模块验收表

| 模块 | 当前客观状态 | 尚需完成的发布验收或开发工作 |
| --- | --- | --- |
| 1. 核心架构与后台任务 | 基本完成，任务并发、原子入队和任务安全逐步加固 | 崩溃、断电重试、任务中断/恢复、长时稳定性与队列压力实测 |
| 2. 持久图库与设备识别 | 部分实现：Catalog/SQLite、设备源、离线元数据、重挂载路径处理 | 真正拔盘/插盘、原盘识别与换盘符、相同盘符不同设备、历史预览持久性、全盘多根目录一致性验收 |
| 3. 扫描、增量索引与格式支持 | 部分实现：增量扫描、恢复会话、RAW/HEIF 与回退路径已有底座 | 断点续扫精度、部分失败避免误标 missing、真实 JPG/PNG/HEIF/RAW/XMP/RAW+JPG 跨盘回归、格式注册统一 |
| 4. 精确去重、相似与智能质量 | 部分实现：感知哈希/ORB/连拍聚类、传统 CV 评分 | **单独的字节级精确去重索引和验收**、场景语义识别与可解释多标签、精细主体/噪声/曝光可信度、误判集评测与人工优先 |
| 5. 人工筛选与集中复核 | 主路径已有，#41–#45 关闭多项文件处理权限漏洞 | 相似/精选界面全部操作边界、历史选择恢复、全流程多轮人工交互与撤销一致性验收 |
| 6. UI 布局与性能 | 多轮优化，但不能只凭自动 UI 冒烟宣称最终完成 | 统一工作台密度/缩放/弹窗/虚拟化、空闲 GPU/CPU、19,000+ 张图库响应及 Windows 高 DPI、不同分辨率实测 |
| 7. 回收站与文件生命周期 | 已修复大量安全边界：二次复核、原子批量任务、离线恢复记录、跨盘校验 | 真实断电/崩溃窗口、跨卷移动与 sidecar、同名冲突、掉盘后恢复、持久状态原子同步全链路验证 |
| 8. Windows 安装/升级/正式发布 | Windows Candidate 工作流成功，但并非 V2.0 Stable | 版本元数据统一、历史 Stable 覆盖升级、数据库备份/恢复、哈希/签名、安装卸载、正式 Release 准入与手工签核 |

## 发布原则与下次继续规则

1. 本文件列出的是**剩余发布阻塞项**，不是宣称 V2.0 完成，也不是重新做已经合并的 PR。原先“约 50%”只是人工工作量粗估，不应自动累加为验收比例。
2. 以 `main` 实时 SHA 为唯一代码基线；对 `perf/v1.8.0-runtime-architecture` 等旧开发分支只审查差异、隔离迁移，不整分支覆盖当前主线。
3. 代码开发、PR 合并、Source/Windows 自动化通过、Candidate EXE 生成、真实设备长时验收、Stable 版本发布，是**六种不同状态**。缺任何发布门禁均不得公开标称“V2.0 正式完成”。
4. 下一轮核心优先级：**字节级精确去重与内容语义分析 → 持久图库/断盘恢复实测 → 大图库性能/布局 → 文件生命周期压力与 Windows 覆盖安装 → 发布签核**。始终先复核 GitHub 实际进度，避免重复修改、避免破坏原始照片。
