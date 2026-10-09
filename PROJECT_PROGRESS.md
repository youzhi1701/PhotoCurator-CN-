# PhotoCurator-CN V2.0 · 开发与发布检查点（2026-10-09）

> 此文件记录已经提交并验证的代码工作，以及尚未满足正式发布标准的工作。**不得把候选安装包、自动化测试或 GitHub 合并等同于用户实机验收与正式发布。**
>
> 最初历史开发基线为 main `81dbb510deb58f191448ed13d791094f9a1f9d7e`（PR #50）；截至本检查点，V2.0 候选已合并 [PR #87](https://github.com/youzhi1701/PhotoCurator-CN-/pull/87)，合并提交 `c6bd12a9a445ab7a8c445c2fcf244b78189dbe07`，源码版本 v1.7.17。Source 与已发布的 v1.7.17 Windows Candidate 构建通过；真实实盘、语义模型评测、正式签名仍未完成。本文件不是实时 API，下轮先核对 GitHub HEAD/CI/构件。

## 2026-10-09 V2.0 v1.7.18 候选：精确去重缓存二次校验（待 CI）

- 精确重复复用缓存的候选组重新计算真实 SHA-256，验证打开的文件身份和扫描前后元数据，避免可写缓存被篡改导致不同原片误报字节精确重复；直接 symlink 不参与。
- 回归覆盖缓存伪造、可信重复组、无重复组低 I/O 路径、符号链接。源数据只读，任何文件删除仍需人工复核令牌。
- Windows 候选目标 v1.7.18（待 Actions 成功），前版 v1.7.17 完整 CI 和 EXE 可下载；硬盘/RAW/AI 模型实测、签名和人工审核仍待验收。

## 2026-10-09 V2.0 候选 v1.7.16 → v1.7.17（已合并，非 Stable）

- [PR #86](https://github.com/youzhi1701/PhotoCurator-CN-/pull/86) 已合并，v1.7.16：RAW/JPEG 多对一歧义不自动合并、区分 POSIX 大小写、DirEntry 流式源扫描跳过 symlink/junction；[Source](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37881401116) 和 [Windows Candidate](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37881401130) 通过。
- [PR #87](https://github.com/youzhi1701/PhotoCurator-CN-/pull/87) 已合并，v1.7.17：回收站在线状态、清单导入/写入、后台恢复/销毁/移动根据已登记的原实体设备进行实时身份核验；防止另一 USB 盘重占 F: 等旧盘符后误显示为原盘或误操作照片。保留离线数据与旧非 Catalog 文件夹兼容。
- 修正 Windows Installer 卸载留存回归测试中的静态误判：Inno Setup 安装目录清理段落的中文注释包含 runtime data 路径，不能误认为实际删除指令；现只审查有效配置行。最新版 [Source/Windows smoke 37884600508](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37884600508) **success**；[v1.7.17 Windows Candidate 37884600438](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37882668181) **success**（对应 PR #87 最终提交，完整通过）。
- 可测试 Windows ZIP：`PhotoCurator-v1.7.17-test-installer`，解压 EXE `PhotoCurator-Setup-v1.7.17.exe`，安装器 SHA-256 `56b85abe23f69aca7cf30817458fdfb34e893c0917349be787e57f0f80ca5978`，Actions Artifact 截止 2026-10-23。已运行旧版 v1.5.0→新 Candidate 原位覆盖和静默卸载保留数据的测试。
- **未完成的 V2.0 Stable 发布门禁**：真实 4TB/19,000+ 图测试、外接 HDD/USB 热插拔与断电、跨相机 RAW/HEIF 样本、语义识别质量评估、真实 Win10/11 + 多 DPI 体验、正式 PFX 数字签名与人工 QA 批准。不得把自动化候选宣称为最终稳定正式版。

## 2026-10-09 V2.0 新增候选 v1.7.15（已通过 CI，尚非 Stable）

- [PR #85](https://github.com/youzhi1701/PhotoCurator-CN-/pull/85) 已合并至 main `6708cb2d709ef982a222a37866d142a050d373d6`，新增外接设备回收站清单的目录可信边界、符号链接/伪造元数据阻断、离线记录保留、并发写入原子发布与 6 项安全回归。
- [Source 和 Windows smoke](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37879822374)：**success**；[Windows Candidate 安装器构建及已发布 v1.5.0→v1.7.15 覆盖升级、卸载留存](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37879822456)：**success**。
- Windows 测试 ZIP 构件：`PhotoCurator-v1.7.15-test-installer`（[GitHub Actions 下载](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37879822456)，有效期至 2026-10-23），包含 `PhotoCurator-Setup-v1.7.15.exe`；构建时安装器 SHA-256：`e5ce68661babf904f93ad76831d431caba253b21b5b693e0419d36b25242454d`。
- **未解决的硬门禁**：4TB/19k+ 原片实盘、真实设备拔插/断电、完整 RAW/HEIF 机型与场景识别误判评测、Win10/11 高 DPI 多机运行、实际 Authenticode 签名与真实用户最终批准。v1.7.15 仍是 Candidate，正式 Stable 仍为 v1.5.0，不创建虚假的 v2.0 稳定标签。

## 2026-10-09 第三批实际完成的 V2.0 代码与 CI 回归（非 Stable）

| PR | 完成内容 | 双平台自动化验证 |
| --- | --- | --- |
| [#81](https://github.com/youzhi1701/PhotoCurator-CN-/pull/81) | Linux 区分 A.jpg / a.jpg 独立生命周期，重叠图库根不再错误折叠大小写不同的原片 | [Source](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37876533611) ✅ / [Windows](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37876533580) ✅ |
| [#82](https://github.com/youzhi1701/PhotoCurator-CN-/pull/82) | 修复历史图库实时缩略图可读取异盘照片、普通缩略图可写入旧盘持久 media_id 的漏洞；只准已验证物理设备与文件身份的后台任务发布持久离线预览；模拟图片解码期间换盘 | [Source](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37877488431) ✅ / [Windows](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37877488413) ✅ |
| [#83](https://github.com/youzhi1701/PhotoCurator-CN-/pull/83) | 实际子进程在任务执行期间 `os._exit(47)` 后立即退出，重启新任务管理器验证队列恢复、审计和只重执行一次 | [Source](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37877384715) ✅ / [Windows](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37877384702) ✅ |
| [#84](https://github.com/youzhi1701/PhotoCurator-CN-/pull/84) | 源码、桌面、Installer、VersionInfo、manifest、Candidate CI 全部升级至 v1.7.14，统一验证整合状态 | [Source](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37878225849) ✅ / [Windows](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37878225848) ✅ |

- 当前 Windows 候选 CI 构件：`PhotoCurator-v1.7.14-test-installer`，解压后文件 `PhotoCurator-Setup-v1.7.14.exe`；原生安装器 SHA-256：`bf72867e2a94506d4200cd927f41cd9c33ace6a80238fab2fb97832a8cbd255f`（2026-10-23 到期）。
- 已验证：源文件不触碰的模拟换盘、后台进程级崩溃、Windows 旧版 v1.5.0 安装原位升级、正常卸载后用户数据留存。
- **未验证/不可声明完成：** 真实 4TB/19K+ 用户照片、真实 USB 热拔插与断电、完整 RAW/HEIF 多品牌样本、语义分类及误判集、最终 UI 多分辨率真实机器体验、实际签名证书与用户 QA 批准。当前 v1.7.14 仅是 Candidate，不满足 V2.0 Stable。

## 2026-10-09 最新完成的第二批代码修复（非 V2 Stable）

- [PR #75](https://github.com/youzhi1701/PhotoCurator-CN-/pull/75)：设备重连必须校验真实 Windows Volume GUID；拒绝仅凭 F: 等旧盘符把另一块盘识别为原库。Linux 使用实际 mount 设备身份，不再默认将全部来源归属根文件系统。源码与 Windows CI 通过。
- [PR #80](https://github.com/youzhi1701/PhotoCurator-CN-/pull/80)：统一集成 PR #76–#79，完成扫描错误落库失败时中止、RAW/HEIF 可发现和可解码分离、路径索引提升、大小写文件身份纠错、安全 Windows 卸载、用户文件夹前缀误过滤修复，全部纳入跨平台 CI。旧 #76–#79 因已由 #80 集成而关闭，不重复合并。
- **统一版本：v1.7.13 Candidate**；[Source checks](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37874982398) 成功，[Windows build/upgrade/uninstall](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37874982341) 成功。Windows 实际旧版安装→原位升级→无提示静默卸载后，程序外原片、运行数据、离线预览仍保留。
- CI Windows 安装包 `PhotoCurator-Setup-v1.7.13.exe`，SHA-256 `e8a40a73c5bac1ee8f111d19ef888a01403bb46b9d99ae4f4aa0bd682323e23e`；Actions ZIP 构件 `PhotoCurator-v1.7.13-test-installer`，有效期至 2026-10-23。此为 Candidate，**绝不冒称完成真实 4TB 设备、19,000+ 真实图库、全相机格式、断电和代码签名的最终版本**。

## 2026-10-09 最新完成的代码修复（非 Stable 发布）

| PR | 本轮内容 | Source / Windows CI |
| --- | --- | --- |
| [#70](https://github.com/youzhi1701/PhotoCurator-CN-/pull/70) | SQLite 旧版迁移改为在线 WAL 一致性快照、快速完整性检查、原子无覆盖备份；新增断电恢复相关回归 | [源码通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37871879060) / [Windows 通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37871878978) |
| [#71](https://github.com/youzhi1701/PhotoCurator-CN-/pull/71) | 后台已恢复任务在处理器尚未注册时不再反复空转；注册后按优先级继续处理 | [源码通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37872005024) / [Windows 通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37872004843) |
| [#72](https://github.com/youzhi1701/PhotoCurator-CN-/pull/72) | 清理缓存/日志/离线预览时拒绝透过符号链接、Junction 和重定向子目录触及照片源 | [源码通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37872342494) / [Windows 通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37872342577) |
| [#73](https://github.com/youzhi1701/PhotoCurator-CN-/pull/73) | SHA-256 精确去重在读文件前后验证已打开文件句柄身份，防扫描期间路径替换导致误匹配 | [源码通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37872291129) / [Windows 通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37872291322) |
| [#74](https://github.com/youzhi1701/PhotoCurator-CN-/pull/74) | 自动 SQLite 备份先完整校验后原子发布；纳秒唯一命名，避免误清理升级前历史恢复点 | [源码通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37872588044) / [Windows 通过](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37872588038) |

另外，主线提交 `3c540af`、`0358fa0`、`b679db1` 修正智能质量评分异常输入容错，补充测试并纳入检查。

**严格交付边界：** 上述仅代表相应代码开发和 CI 已完成；完整 V2.0 的场景语义模型/误判评测、真实大型硬盘与多品牌 RAW/HEIF、断电实盘、Windows 多机器及高 DPI 交互验收、真实数字签名仍未完成。当前不得创建/宣称 V2.0 Stable，也不得把旧版 v1.7.12 构件冒充本轮最终安装包。

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
| [#68](https://github.com/youzhi1701/PhotoCurator-CN-/pull/68) | 中途换盘：批次元数据收集后、SQLite 写入前再次核验物理磁盘；Windows 本机增加文件安全、RAW+JPG、Catalog 及质量标签门禁；桌面 EXE 启动时验证已打包的人脸级联数据；统一升级 **v1.7.11 Candidate**、随包输出 SHA-256 | Source [37867935741](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37867935741) 成功、Windows Candidate [37867935635](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37867935635) 成功，已合并；安装包 SHA-256 已独立校验 |
| [#69](https://github.com/youzhi1701/PhotoCurator-CN-/pull/69) | 后台离线预览生成前验证真实数据源身份、照片文件范围/大小/mtime，并在解码后核对原文件是否变化；补 5 项回归，Windows 及源码升级为 **v1.7.12 Candidate** | Source [37869003613](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37869003613)、Windows [37869003552](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37869003552) **均通过**；已合并至 `00dabf12` |

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
| 8. Windows 安装/升级/正式发布 | v1.7.14 Candidate 已完成 Source+Windows 两项 CI、Windows v1.5.0 覆盖升级及实际静默卸载数据留存、EXE 检查与 SHA-256；正式稳定版仍为 v1.5.0 | 真实设备与真实资料迁移/数据库备份恢复测试、实际数字签名证书与正式 Release 签核 |

## 发布原则与下次继续规则

1. 本文件列出的是**剩余发布阻塞项**，不是宣称 V2.0 完成，也不是重新做已经合并的 PR。原先“约 50%”只是人工工作量粗估，不应自动累加为验收比例。
2. 以 `main` 实时 SHA 为唯一代码基线；对 `perf/v1.8.0-runtime-architecture` 等旧开发分支只审查差异、隔离迁移，不整分支覆盖当前主线。
3. 代码开发、PR 合并、Source/Windows 自动化通过、Candidate EXE 生成、真实设备长时验收、Stable 版本发布，是**六种不同状态**。缺任何发布门禁均不得公开标称“V2.0 正式完成”。
4. 下一轮核心优先级：**场景语义分析/可信度与误判集 → 持久图库/断盘恢复实测 → 大图库性能/布局 → 文件生命周期真实断电、跨盘与 sidecar 回归 → Windows 覆盖升级与发布签核**。始终先复核 GitHub 实际进度，避免重复修改、避免破坏原始照片。

## 最新主线与发布阻塞同步

- PR #69 的业务代码已在主线：`00dabf12be59ca940d0dfa942fcc53669c552248`，v1.7.12 Windows CI 已完成（2026-10-09）。
- PR #63、#65、#66、#67 均已完成各自 Source/Windows 双门禁；#64 为冲突/过时分支，不得再整分支合并。
- 代码 CI、Windows Candidate 与旧版覆盖升级自动化不等于实机原厂硬盘场景验收，也不代表数字签名凭据已配置。
- 正式版 Release 仍需 `RELEASE_ACCEPTANCE.md` 的完整人工和设备验收。

## 最新交接说明（2026-10-09）

- 当前正式可下载 Stable 仍为 **v1.5.0**。v1.7.14 为**候选测试版**，Source 与 Windows CI 已通过，但实盘/代码签名尚未完成。严禁将候选安装器当作完成 V2.0 正式发布。
- GitHub 无真实 4TB 外接机械硬盘与用户 Windows 测试机连接；正式签名所需 PFX/密码不能由模型凭空生成。
- 发布级实机测试、签名设置、评估数据和发布前签核统一按 [RELEASE_ACCEPTANCE.md](RELEASE_ACCEPTANCE.md)。
- 后续迭代必须重新从 GitHub main 的真实 SHA、PR 状态和 Actions 构件核对，不依赖本文件猜测进度。

## v1.7.12 历史测试包（2026-10-09）

- [Windows CI 构建及下载入口](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37869003552) — Artifact 名为 **PhotoCurator-v1.7.12-test-installer**，下载的是含 EXE 与 .sha256 的 ZIP；构件有效期至 **2026-10-23 01:23 UTC**。
- 文件名称：`PhotoCurator-Setup-v1.7.12.exe`；SHA-256：`6710c8a00df0b3e81b69ce8162a1562ba402576fcd1e1c11ae91f25506fbb8eb`；验证合格。
- **真实外接 4TB HDD 长时、19,000+ 张真实照片、完整 RAW/HEIF 样本、突然断电、Windows 高 DPI 多版本验收与正式签名**依然是人工/硬件门槛，不能宣布 V2 Stable。

## v1.7.13 最新候选构件（2026-10-09）

- [Windows CI 构建与下载入口](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37874982341)，Artifact 名称 `PhotoCurator-v1.7.13-test-installer`，ZIP 内包含 `PhotoCurator-Setup-v1.7.13.exe` 及其 `.sha256`，2026-10-23 到期。
- Windows EXE 安装器 SHA-256：`e8a40a73c5bac1ee8f111d19ef888a01403bb46b9d99ae4f4aa0bd682323e23e`。
- GitHub 仓库代码已合并：`cd589ccee7b2a653a61de61a06a9147da5697f80`；后续文档提交不应改变此代码快照对应的 CI 证据。
- 仍需高风险场景真实设备/实盘和交互验收、完整场景模型评测、实际 Authenticode 签名与逐版本 QA 签核后才能标记 V2.0 Stable。

## v1.7.14 当前代码快照（2026-10-09）

- [Source 验证](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37878225849)：success。
- [Windows 原生 EXE 和 Installer 构建、真实 v1.5.0 升级/卸载回归](https://github.com/youzhi1701/PhotoCurator-CN-/actions/runs/37878225848)：success。
- [第三批完整集成 PR #84](https://github.com/youzhi1701/PhotoCurator-CN-/pull/84)，源代码合并 SHA `bf62baf9e8d3268330723549122d2ccf8c419594`，文档更新不改动其候选程序构建证据。
- 数字签名与真实硬盘 QA 尚缺，**不可创建声称正式完成的 v2.0 tag/Stable release**。
