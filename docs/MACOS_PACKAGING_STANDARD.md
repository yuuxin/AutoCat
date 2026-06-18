# AutoCat macOS 打包与发布标准

版本：1.0

状态：已批准的目标规范，尚未完成工程化实施

适用范围：AutoCat 3.x 及后续 macOS 桌面版本

## 1. 目标

发布的 AutoCat 必须可以安装到普通用户的 Mac 上直接运行，不依赖用户预装或配置以下环境：

- Python、pip、venv
- Homebrew、MacPorts
- SQLite
- FFmpeg、FFprobe
- Qt、PySide6
- NumPy、ONNX Runtime 或其他 Python 包
- Xcode、Command Line Tools

用户机器已经安装上述软件时，AutoCat 也不得调用或加载用户环境中的版本。发布包必须是独立、可重复构建、可验证和可追溯的自包含应用。

## 2. 正式支持矩阵

### 2.1 基础支持范围

| 项目 | 标准 |
|---|---|
| 最低系统 | macOS 13.3 |
| 处理器 | Apple Silicon arm64、Intel x86_64 |
| 安装方式 | DMG 拖拽到 Applications |
| 用户权限 | 普通用户，不要求管理员权限 |
| 网络 | 应用可离线启动；联网功能单独提示 |
| 外部运行时 | 不允许 |

默认发布两个独立安装包：

- `AutoCat-<version>-macOS-arm64.dmg`
- `AutoCat-<version>-macOS-x86_64.dmg`

在两种架构都完成稳定验证前，不合并 Universal2 安装包。独立安装包更容易审计架构、控制体积并定位原生依赖问题。

### 2.2 Intel 支持门禁

Intel 版本不是“从 arm64 包转换”得到的，必须在 x86_64 构建环境中独立解析依赖、构建和测试。

当前宽兼容依赖基线要求 ONNX Runtime 不高于 `1.23.2`，因为后续官方 macOS wheel 已出现提高最低系统版本和不再提供 x86_64 wheel 的变化。升级 ONNX Runtime 前必须重新检查：

- Python 3.12 对应 wheel 是否存在；
- arm64 和 x86_64 wheel 是否同时存在；
- wheel 的 macOS deployment target 是否不高于 13.3；
- MobileCLIP 模型推理结果是否一致。

如果任何一项失败，不得静默取消 Intel 支持。必须在以下方案中明确选择并记录：

1. 保持经过验证的旧版本；
2. 为缺失架构自行构建 ONNX Runtime；
3. 发布说明中正式终止 Intel 支持。

### 2.3 更旧系统

macOS 13.2 及以下不属于正式支持范围。应用应在启动前显示清晰的系统版本提示，而不是闪退。

如业务需要支持 macOS 12，应建立单独的 Legacy 产品线、依赖锁和验收矩阵，不能降低主发布线的依赖标准后直接声称兼容。

## 3. 固定技术路线

### 3.1 Python

- 发布运行时采用 CPython 3.12。
- 每个发布分支必须锁定到完整补丁版本和下载制品 SHA-256。
- Python 必须来自可再分发、与目标架构和最低系统兼容的正式制品。
- 禁止直接复制构建机当前 Homebrew Python。
- 禁止回退到 `/usr/bin/python3`、`python3` 或用户 PATH 中的 Python。

Python 标准库及其原生扩展必须包含并验证：

- `sqlite3`
- `ssl`
- `decimal`
- `zlib`
- `bz2`
- `lzma`
- `ctypes`
- `multiprocessing`

### 3.2 应用冻结

- 使用 PyInstaller `onedir` 模式构建标准 `.app`。
- 使用版本控制中的固定 `.spec` 文件描述入口、资源、隐藏导入和 Qt 插件。
- 不采用 `onefile`，避免首次启动解压、临时目录权限和安全软件误判。
- PyInstaller 与 hooks 版本必须锁定，不得在发布时直接安装 latest。

### 3.3 Python 依赖

开发依赖可以保留范围约束，但发布构建必须使用按架构生成的完整锁文件，并包含哈希：

- `requirements-macos-arm64.lock`
- `requirements-macos-x86_64.lock`
- `requirements-build.lock`

锁文件生成后必须提交 Git，并且只能通过专门的依赖升级合并请求更新。不能在正式发布 Job 中运行无版本约束的 `pip install -e .`。

建议的宽兼容基线：

| 组件 | 基线 |
|---|---|
| Python | 3.12.x，完整补丁版本锁定 |
| PySide6 | 6.8 LTS 系列，完整版本锁定 |
| ONNX Runtime | 1.23.2，除非通过双架构兼容门禁 |
| NumPy/SciPy | 使用支持 CPython 3.12 且 deployment target 合格的 wheel |
| PyInstaller | 完整版本锁定 |

所有间接依赖同样必须锁定，不能只锁顶层依赖。

### 3.4 FFmpeg

FFmpeg 和 FFprobe 必须作为 AutoCat 私有运行时随包分发：

- arm64、x86_64 分别构建；
- 最低系统版本不高于 13.3；
- 优先采用满足功能需求的最小化构建；
- 所有非系统动态库必须随包内置；
- 不得引用 `/opt/homebrew`、`/usr/local` 或构建机 Cellar；
- 必须验证 AutoCat 使用的编码器、解码器、滤镜和字幕能力；
- 发布前必须审查 LGPL/GPL 配置及对应分发义务。

禁止直接从当前 Homebrew 安装目录复制 FFmpeg 作为发布制品。

## 4. 应用目录与用户数据

`.app` 内资源视为只读。运行中产生的数据必须写入用户目录：

| 数据 | 目录 |
|---|---|
| 数据库、任务、用户配置 | `~/Library/Application Support/AutoCat` |
| 缓存、临时分析结果 | `~/Library/Caches/AutoCat` |
| 日志 | `~/Library/Logs/AutoCat` |
| 用户主动导出的文件 | 用户选择的目录 |

必须支持：

- 中文、日文、空格和 Unicode 用户名；
- 应用安装在 `/Applications` 之外；
- 用户没有管理员权限；
- 应用路径和素材路径包含空格；
- 数据目录不存在时自动创建；
- 数据库升级前自动备份；
- 多次启动不破坏已有任务和配置。

不得把 API Key、用户数据库、运行日志、素材或缓存写入 `.app`。

## 5. 环境隔离

启动器必须清理或忽略可能污染运行时的变量：

- `PYTHONHOME`
- `PYTHONPATH`
- `VIRTUAL_ENV`
- `CONDA_PREFIX`
- `DYLD_LIBRARY_PATH`
- `DYLD_FRAMEWORK_PATH`

仅允许由应用自身设置指向包内资源的私有路径。不得搜索用户的 Homebrew、Conda、系统 Python 或当前工作目录来补齐依赖。

应用从 Finder 启动、Terminal 启动以及双击 DMG 安装后的行为必须一致。

## 6. 原生依赖审计

每次发布必须递归扫描 `.app` 中所有 Mach-O 文件，包括：

- 主程序；
- Python 解释器和 Framework；
- `lib-dynload`；
- Python `.so` 和 `.dylib`；
- Qt Framework 和插件；
- ONNX Runtime；
- NumPy、SciPy、Numba、llvmlite、SoundFile；
- FFmpeg、FFprobe 及其库。

发布门禁：

1. 不得存在 `/opt/homebrew`、`/usr/local`、构建机用户目录或项目源码绝对路径。
2. 非系统动态库必须位于 `.app` 内。
3. 动态加载路径必须使用 `@rpath`、`@loader_path` 或 `@executable_path`。
4. 每个 Mach-O 的架构必须与目标安装包一致。
5. 每个 Mach-O 的最低系统版本不得高于 13.3。
6. 不得依赖构建机已有文件才能通过验证。

任何一项失败都必须阻止发布。

## 7. 版本、签名与公证

### 7.1 版本一致性

版本号必须只有一个权威来源，并同步写入：

- Python 包版本；
- `CFBundleShortVersionString`；
- `CFBundleVersion`；
- DMG 文件名；
- Git Tag；
- 发布清单。

禁止在多个脚本中手工维护不同版本号。

### 7.2 正式分发

生产包必须完成：

1. 使用 Developer ID Application 证书签名；
2. 启用 Hardened Runtime；
3. 从最内层原生组件向外逐层签名；
4. 对 `.app` 做严格签名验证；
5. 提交 Apple Notary Service；
6. 将公证票据 staple 到 `.app`；
7. 创建并签名 DMG；
8. 对 DMG 再做公证/staple 验证；
9. 在无开发证书的干净机器上通过 Gatekeeper。

临时 ad-hoc 签名只能用于本地开发包，不能作为用户发布包。

签名证书、Apple ID、App Store Connect API Key 等机密只能保存在 GitLab Protected/Masked Variables 或专用 Keychain 中，禁止提交到 Git。

## 8. 构建环境

正式构建必须使用专用、可重建的 GitLab macOS Runner：

- `macos-13-arm64`：构建 arm64；
- `macos-13-x86_64`：构建 Intel。

Runner 必须：

- 不依赖开发人员个人 Homebrew 环境；
- 从锁文件创建全新构建环境；
- 每次构建使用干净工作目录；
- 记录 macOS、Xcode、Python、PyInstaller 和依赖版本；
- 只从受控缓存或可信上游下载依赖；
- 不把上一次构建的 `.venv`、`build`、`dist` 带入下一次发布。

如果无法长期保留 macOS 13.3 Runner，可以在更高系统上构建，但必须证明所有输入原生制品的 deployment target 合格，并继续在 macOS 13.3 干净测试机执行安装验收。仅设置 `MACOSX_DEPLOYMENT_TARGET` 不构成兼容性证明。

## 9. 测试与发布门禁

### 9.1 源码测试

发布前必须通过：

- 项目单元测试；
- 数据库迁移测试；
- UI 冒烟测试；
- 任务 CRUD 和状态机测试；
- 核心渲染、TTS、字幕、BGM、去重测试；
- 完整向导与端到端测试。

### 9.2 打包后运行时测试

测试必须调用 `.app` 内的实际可执行文件，至少验证：

- 应用可以启动并显示主窗口；
- 首次启动可以创建数据目录和 SQLite 数据库；
- `sqlite3` 建表、写入、查询和迁移正常；
- NumPy、SciPy、Librosa、SoundFile、Numba 可加载；
- ONNX Runtime 可加载 MobileCLIP 并得到预期维度；
- FFmpeg/FFprobe 可执行；
- 音频检测、图片抽帧、短视频渲染可完成；
- 退出后再次启动能读取已有数据；
- 缺少网络和 API Key 时不会闪退；
- 路径包含中文和空格时正常。

### 9.3 干净机器矩阵

正式发布至少在以下四种组合验收：

| 系统 | 架构 | 环境 |
|---|---|---|
| 最低支持 macOS | arm64 | 无 Homebrew、无额外 Python |
| 当前稳定 macOS | arm64 | 普通用户环境 |
| 最低支持 macOS | x86_64 | 无 Homebrew、无额外 Python |
| 当前可用 Intel macOS | x86_64 | 普通用户环境 |

另外至少验证一次“机器已经安装 Homebrew/Python/Conda”的环境，确保 AutoCat 不加载外部运行时。

## 10. 失败体验与可诊断性

启动失败不得只表现为 Dock 图标闪退。应用必须尽可能：

- 显示用户可理解的错误窗口；
- 把完整异常写入 `~/Library/Logs/AutoCat`；
- 记录应用版本、macOS 版本、架构和关键组件版本；
- 不记录 API Key、用户文案或其他敏感信息；
- 提供“打开日志目录”入口；
- 对系统版本过低、磁盘不足、权限不足、模型损坏分别提示。

崩溃日志必须足以区分运行时缺失、签名、公证、数据库、FFmpeg、模型和网络错误。

## 11. 发布制品

每个正式版本必须生成并保存：

- arm64 DMG；
- x86_64 DMG；
- 每个 DMG 的 SHA-256；
- 构建清单；
- 完整依赖锁文件；
- 第三方许可证清单；
- SBOM；
- 测试报告；
- 签名和公证验证结果；
- 发布说明；
- 与版本一致的 Git Tag。

DMG 不得包含 `.env`、API Key、用户数据库、日志、缓存、测试素材或开发工具状态。

## 12. 标准发布流水线

固定顺序如下：

1. 校验工作树、版本号和 Git Tag；
2. 从锁文件建立干净 Python 3.12 环境；
3. 运行源码测试；
4. 分架构构建 PyInstaller `onedir` 应用；
5. 内置受控 FFmpeg/FFprobe 和模型资源；
6. 执行原生依赖、架构和 deployment target 审计；
7. 执行打包后运行时测试；
8. 逐层签名并验证；
9. Apple 公证和 staple；
10. 制作并验证 DMG；
11. 在干净机器矩阵安装验收；
12. 生成 SHA-256、SBOM、许可证和测试报告；
13. 仅在所有门禁通过后发布 GitLab Release。

## 13. 当前仓库迁移要求

当前 `build_app.py` 和 `make dmg` 属于旧打包方案，不符合本标准，原因包括：

- 复制当前 Homebrew Python；
- 部分动态库收集范围不足；
- 构建机环境会掩盖缺失依赖；
- 当前制品包含过高的 macOS deployment target；
- 使用 ad-hoc 签名；
- 缺少正式公证和干净机器验收。

在新打包流程完成前：

- 旧脚本可用于本机构建和问题分析；
- 不得用旧脚本生成对外正式发布包；
- README 或发布说明不得声称旧 DMG 可在无环境用户机器上直接运行。

新流程实施完成的定义：

- 新 `.spec`、依赖锁、审计脚本、签名/公证脚本和 GitLab CI 全部纳入版本控制；
- 四类干净机器验收全部通过；
- 当前旧 DMG 问题不再复现；
- `make dmg` 明确切换到新流程后，旧脚本才能退役。

## 14. 规范变更

以下变化必须通过独立兼容性评审：

- 提高 Python、PySide6、ONNX Runtime、NumPy、SciPy、PyInstaller 或 FFmpeg 版本；
- 修改最低 macOS 版本；
- 停止 Intel 支持；
- 改为 Universal2；
- 修改签名 entitlements；
- 新增大型原生依赖或本地 AI 模型。

依赖“能安装成功”不等于“可发布”。只有完成本标准的原生审计、打包后测试和干净机器验收，才能更新发布基线。
