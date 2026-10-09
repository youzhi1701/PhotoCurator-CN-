# PhotoCurator-CN V2.0 · 开发与发布检查点（2026-10-09）

> 此文件记录已经提交并验证的代码工作，以及尚未满足正式发布标准的工作。**不得把候选安装包、自动化测试或 GitHub 合并等同于用户实机验收与正式发布。**
>
> 本轮核对并开发的基线：main `81dbb510deb58f191448ed13d791094f9a1f9d7e`（PR #50）。每次恢复时必须重新读取 GitHub main 最新 SHA、未合并 PR、CI 和构件；本文件不是实时 API。

## 最新已合并的 V2.0 开发升级

| PR | 修复内容 | 状态与自动化验证 |
| --- | --- | --- |
| [#40](https://github.com/youzhi1701/PhotoCurator-CN-/pull/40) | 未注册文件处理器不能误抢已恢复任务 | 已合并 |
| [#41](https://github.com/youzhi1701/PhotoCurator-CN-/pull/41) | 待删除照片集中复核、二次确认与选择绑定 | 已合并；此前缺乏强制复核凭证已由 #42 修复 |
| [#42](https://github.com/youzhi1701/PhotoCurator-CN-/pull/42) | 批量移入回收站必须具有服务端随机、限时、一次性、绑定本批选择的复核令牌；补缺失/伪造/重放/过期测试 | Source [37796094774](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37796094774) 成功；Windows Candidate [37796094811](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37796094811) 成功 |
| [#43](https://github.com/youzhi1701/PhotoCurator-CN-/pull/43) | 单张文件后台任务先成功入队，再标记待处理；失败时保持原生命周期 | Source [37796984335](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37796984335) 成功；Windows Candidate [37796984303](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37796984303) 成功 |
| [#44](https://github.com/youzhi1701/PhotoCurator-CN-/pull/44) | 回收站永久删除复核凭证、批量恢复及删除原子入队、离线回收站历史记录及清单不丢失、离线文件禁止处理 | Source [37798582293](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37798582293) 成功；Windows Candidate [37798582672](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37798582672) 成功 |
| [#45](https://github.com/youzhi1701/PhotoCurator-CN-/pull/45) | 直接永久删除增加服务端一次性凭证，并绑定文件真实路径、设备/文件身份、大小、mtime；文件被替换、凭证重放时拒绝操作 | Source [37799538474](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37799538474) 成功；Windows Candidate [37799538559](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37799538559) 成功 |
| [#46](https://github.com/youzhi1701/PhotoCurator-CN-/pull/46) | 相似组“不保留照片”的批量移入回收站改为集中预览、选择绑定随机令牌及原子入队 | Source [37800881979](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37800881979) 成功；Windows Candidate [37800881813](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37800881813) 成功 |
| [#47](https://github.com/youzhi1701/PhotoCurator-CN-/pull/47) | 新增按大小预筛、SHA-256 分块内容哈希、同字节优先归组与失效缓存；与 pHash/ORB 相似识别分离，UI 标记精确重复，原片不自动删除 | Source [37802912709](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37802912709) 成功；Windows Candidate [37802912614](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37802912614) 成功 |
| [#48](https://github.com/youzhi1701/PhotoCurator-CN-/pull/48) | 同盘及跨盘暂存最终提交以原子 no-replace 语义替代 check-then-os.replace，增加并发目标占位防覆盖测试 | Source [37803254042](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37803254042) 成功；Windows Candidate [37803254041](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37803254041) 成功 |
| [#49](https://github.com/youzhi1701/PhotoCurator-CN-/pull/49) | 相似组首屏页量降为 64 组，启用屏外 group content-visibility，保留分页与滚动状态 | Source [37803785935](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37803785935) 成功；Windows Candidate [37803786039](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37803786039) 成功 |
| [#50](https://github.com/youzhi1701/PhotoCurator-CN-/pull/50) | 集中批量复核令牌同时绑定原片设备、文件号、大小和 mtime，预览后同路径被替换拒绝处理；覆盖模糊、相似、回收站三条路径 | Source [37804353458](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37804353458) 成功；Windows Candidate [37804353771](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37804353771) 成功 |


| [#51](https://github.com/youzhi1701/PhotoCurator-CN-/pull/51) | 所有后台文件任务绑定提交时的文件身份，执行时拒绝同路径替换/换盘误删；更新崩溃恢复冒烟测试 | Source [37808398464](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37808398464) 与 Candidate [37808398429](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37808398429) 均通过，已合并 |
| [#52](https://github.com/youzhi1701/PhotoCurator-CN-/pull/52) | 全扫描末尾核验原设备身份，拔盘/换盘后不误标历史照片缺失 | Source [37808545593](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37808545593)、Candidate [37808545608](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37808545608) 通过，已合并 |
| [#53](https://github.com/youzhi1701/PhotoCurator-CN-/pull/53) | 智能优选新增失焦、曝光、噪点、动态范围的可解释人工复核建议 | Source [37808869232](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37808869232)、Candidate [37808869195](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37808869195) 通过，已合并 |
| [#54](https://github.com/youzhi1701/PhotoCurator-CN-/pull/54) | 入队时记录 XMP/AAE 辅件身份，后台安全移动/恢复/永久删除及半完成移动对账 | Source [37809840744](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37809840744)、Candidate [37809840743](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37809840743) 通过，已合并 |
| [#55](https://github.com/youzhi1701/PhotoCurator-CN-/pull/55) | 开发版、桌面、安装器、清单及 Windows Candidate 一致升级为 v1.7.9，不改历史正式下载地址 | Source [37810479111](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37810479111)、Candidate [37810479140](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37810479140) 通过，已合并 |
| [#56](https://github.com/youzhi1701/PhotoCurator-CN-/pull/56) | Top-N 从全量排序改为有界堆选取，含 19,000 条合成候选/离线回退及稳定排序测试 | Source [37810636881](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37810636881)、Candidate [37810637195](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37810637195) 通过，已合并 |
| [#58](https://github.com/youzhi1701/PhotoCurator-CN-/pull/58) | Stable 发布增加逐版本真实 QA 批准、EXE 和安装器 Authenticode 签名与验签要求 | Source [37811617983](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37811617983)、Candidate [37811618077](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37811618077) 通过，已合并；真实签名尚未执行 |
| [#59](https://github.com/youzhi1701/PhotoCurator-CN-/pull/59) | 有界的本地 OpenCV 正脸检测，未检测到脸绝不冒称风景；继承替换冲突 PR #57 | Source [37812894532](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37812894532)、Candidate [37812894510](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37812894510) 通过，已合并；#57 关闭 |
| [#61](https://github.com/youzhi1701/PhotoCurator-CN-/pull/61) | 持久图库每批次写入前检查原设备身份，扫描中途拔盘/换盘不插入异盘照片 | Source [37813476841](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37813476841)、Candidate [37813476828](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37813476828) 通过，已合并 |
| [#62](https://github.com/youzhi1701/PhotoCurator-CN-/pull/62) | 优选卡片和大图直接显示质量候选标签及可信的人像内容线索 | Source [37813971966](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37813971966)、Candidate [37813971831](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37813971831) 通过，已合并 |
| [#60](https://github.com/youzhi1701/PhotoCurator-CN-/pull/60) | 验证真实 v1.5.0 → v1.7.9 升级中软件配置、离线预览和原片哈希不变 | Windows 日志实测两次安装都成功，但 YAML 在调用 PowerShell 脚本后误把空 LASTEXITCODE 当成失败；以 #63 修复并合并 |
| [#63](https://github.com/youzhi1701/PhotoCurator-CN-/pull/63) | v1.7.10 Candidate 版本号/安装器、整合已发布 v1.5.0 安装器覆盖升级测试（SHA-256 校验），保留用户配置与离线预览 | Source [37816419000](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37816419000)、Windows [37816419017](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37816419017) 均通过；已合并 |


| [#65](https://github.com/youzhi1701/PhotoCurator-CN-/pull/65) | 空扫描与数据缺失处理：避免异常/空结果误判整个旧图库均已消失 | Source [37818869453](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37818869453)、Windows [37818869457](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37818869457) 通过；已合并 |
| [#66](https://github.com/youzhi1701/PhotoCurator-CN-/pull/66) | RAW+JPEG 同拍保护：仅对真正 JPEG 配对执行折叠，PNG/TIFF/HEIF 不再被误隐藏 | Source [37817846937](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37817846937)、Windows [37817846878](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37817846878) 通过；已合并 |
| [#67](https://github.com/youzhi1701/PhotoCurator-CN-/pull/67) | 跨盘移动复制窗口的源文件身份复核及 Windows 安全回归（替代冲突 #64） | Source [37820472201](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37820472201)、Windows [37820472163](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37820472163) 通过；已合并 |

以上状态只代表相应代码和已有自动化测试通过；**没有执行 4TB 外接机械硬盘、19,000 张以上真实图库、全格式 RAW/HEIF 样本以及多版本覆盖安装等完整实机验收**。本轮没有将候选包发布成 Stable。

## V2.0 八模块验收表

| 模块 | 当前客观状态 | 尚需完成的发布验收或开发工作 |
| --- | --- | --- |
| 1. 核心架构与后台任务 | 基本完成，任务并发、原子入队和任务安全逐步加固 | 崩溃、断电重试、任务中断/恢复、长时稳定性与队列压力实测 |
| 2. 持久图库与设备识别 | 部分实现：Catalog/SQLite、设备源、离线元数据、重挂载路径处理 | 真正拔盘/插盘、原盘识别与换盘符、相同盘符不同设备、历史预览持久性、全盘多根目录一致性验收 |
| 3. 扫描、增量索引与格式支持 | 部分实现：增量扫描、恢复会话、RAW/HEIF 与回退路径已有底座 | 断点续扫精度、部分失败避免误标 missing、真实 JPG/PNG/HEIF/RAW/XMP/RAW+JPG 跨盘回归、格式注册统一 |
| 4. 精确去重、相似与智能质量 | 已有 SHA-256 字节精确去重、传统 CV 技术复核及轻量人脸候选提示；不是完整场景语义模型 | 大图库精确索引性能实测、场景语义识别与可解释标签、精细主体/噪声/曝光可信度、误判集评测与人工优先 |
| 5. 人工筛选与集中复核 | #41–#46、#50 已加强批量复核、一次性授权、状态及源文件身份保护 | 相似/精选全部交互边界、历史选择恢复、跨设备重连的人工复核与撤销一致性实测 |
| 6. UI 布局与性能 | 相似组 64 组分批显示、屏外渲染跳过，Top-N 有界排序；19,000 条合成数据回归已通过 | 统一工作台密度/缩放/弹窗/完整虚拟化、空闲 CPU/GPU、19,000+ 张图库响应及 Windows 高 DPI、多分辨率实测 |
| 7. 回收站与文件生命周期 | 二次复核、原子入队、离线历史、文件及 XMP/AAE 身份绑定、跨盘 SHA-256/no-replace 与中断恢复已加固 | 真实断电/崩溃窗口、跨卷与 sidecar、同名竞态实机确认、掉盘后恢复、持久状态原子同步全链路验证 |
| 8. Windows 安装/升级/正式发布 | v1.7.10 Candidate 版本元数据与已发布 v1.5.0 地址保持分离；#63 在 Windows Runner 验证旧版覆盖升级；正式发布有强制 QA/签名门禁 | 真实设备与真实资料迁移/数据库备份恢复测试、实际数字签名证书与正式 Release 签核 |

## 发布原则与下次继续规则

1. 本文件列出的是**剩余发布阻塞项**，不是宣称 V2.0 完成，也不是重新做已经合并的 PR。原先“约 50%”只是人工工作量粗估，不应自动累加为验收比例。
2. 以 `main` 实时 SHA 为唯一代码基线；对 `perf/v1.8.0-runtime-architecture` 等旧开发分支只审查差异、隔离迁移，不整分支覆盖当前主线。
3. 代码开发、PR 合并、Source/Windows 自动化通过、Candidate EXE 生成、真实设备长时验收、Stable 版本发布，是**六种不同状态**。缺任何发布门禁均不得公开标称“V2.0 正式完成”。
4. 下一轮核心优先级：**场景语义分析/可信度与误判集 → 持久图库/断盘恢复实测 → 大图库性能/布局 → 文件生命周期真实断电、跨盘与 sidecar 回归 → Windows 覆盖升级与发布签核**。始终先复核 GitHub 实际进度，避免重复修改、避免破坏原始照片。

## 最新主线与发布阻塞同步

- 已核对并合并 PR #67，主线代码提交：`1ef8d061a3c6e77eb16003fdd07dde535e25ad27`（2026-10-09）。
- PR #63、#65、#66、#67 均已完成各自 Source/Windows 双门禁；#64 为冲突/过时分支，不得再整分支合并。
- 代码 CI、Windows Candidate 与旧版覆盖升级自动化不等于实机原厂硬盘场景验收，也不代表数字签名凭据已配置。
- 正式版 Release 仍需 `RELEASE_ACCEPTANCE.md` 的完整人工和设备验收。

## 最新交接说明（2026-10-09）

- 当前正式可下载 Stable 仍为 **v1.5.0**。v1.7.10 为**候选测试版**（此版本更新后以 Windows CI 结果为准），严禁将 Windows 候选安装器当作完成实机验收的 V2.0 Stable。
- GitHub 无真实 4TB 外接机械硬盘与用户 Windows 测试机连接；正式签名所需 PFX/密码不能由模型凭空生成。
- 发布级实机测试、签名设置、评估数据和发布前签核统一按 [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md)。
- 后续迭代必须重新从 GitHub main 的真实 SHA、PR 状态和 Actions 构件核对，不依赖本文件猜测进度。
