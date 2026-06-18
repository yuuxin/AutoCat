# AutoCat macOS 发布检查表

该清单用于每次正式版本发布。任何标记为“必须”的项目未完成时，不得上传用户安装包。

## 发布信息

- [ ] 版本号：
- [ ] Git Commit：
- [ ] Git Tag：
- [ ] 发布负责人：
- [ ] 发布日期：
- [ ] 最低 macOS：
- [ ] arm64 Runner：
- [ ] x86_64 Runner：

## 1. 源码与版本

- [ ] 工作树没有未提交文件。
- [ ] 版本号来自唯一权威来源。
- [ ] Python 包、App Bundle、DMG 和 Git Tag 版本一致。
- [ ] `.env`、API Key、数据库、日志、缓存、用户素材未进入 Git 或安装包。
- [ ] 发布分支已通过代码评审。

## 2. 工具链和依赖

- [ ] CPython 3.12 完整补丁版本已锁定。
- [ ] Python Runtime 下载制品 SHA-256 已记录。
- [ ] PyInstaller 和 hooks 完整版本已锁定。
- [ ] arm64 与 x86_64 依赖锁文件已提交。
- [ ] 所有锁文件包含哈希。
- [ ] FFmpeg/FFprobe 来自受控发布制品，不来自当前 Homebrew 目录。
- [ ] ONNX Runtime 同时满足两架构和最低系统要求。
- [ ] 第三方许可证已重新生成和审查。

## 3. 源码测试

- [ ] 单元测试通过。
- [ ] 数据库 CRUD、迁移和锁重试测试通过。
- [ ] UI 冒烟和向导流程测试通过。
- [ ] TTS、字幕、BGM、去重和渲染测试通过。
- [ ] 完整端到端测试通过。
- [ ] 测试报告已保存为发布制品。

## 4. arm64 安装包

- [ ] 在 `macos-13-arm64` 干净 Runner 构建。
- [ ] 只包含 arm64 或明确批准的通用资源。
- [ ] 所有 Mach-O deployment target 不高于最低系统。
- [ ] 不含 `/opt/homebrew`、`/usr/local`、构建机用户目录或源码路径。
- [ ] 所有非系统动态库都位于 `.app` 内。
- [ ] 包内 SQLite 可建表、写入、查询。
- [ ] 包内 ONNX Runtime 可完成 MobileCLIP 推理。
- [ ] 包内 FFmpeg/FFprobe 可执行并完成短视频渲染。
- [ ] Finder 启动、Terminal 启动和二次启动均正常。

## 5. x86_64 安装包

- [ ] 在 `macos-13-x86_64` 干净 Runner 构建。
- [ ] 只包含 x86_64 或明确批准的通用资源。
- [ ] 所有 Mach-O deployment target 不高于最低系统。
- [ ] 不含 `/opt/homebrew`、`/usr/local`、构建机用户目录或源码路径。
- [ ] 所有非系统动态库都位于 `.app` 内。
- [ ] 包内 SQLite 可建表、写入、查询。
- [ ] 包内 ONNX Runtime 可完成 MobileCLIP 推理。
- [ ] 包内 FFmpeg/FFprobe 可执行并完成短视频渲染。
- [ ] Finder 启动、Terminal 启动和二次启动均正常。

## 6. 常规用户环境

- [ ] 无 Homebrew、无额外 Python 的最低系统 arm64 机器通过。
- [ ] 无 Homebrew、无额外 Python 的最低系统 x86_64 机器通过。
- [ ] 当前稳定 macOS arm64 机器通过。
- [ ] 当前可用 Intel macOS 机器通过。
- [ ] 已安装 Homebrew/Python/Conda 的机器通过，且没有加载外部运行时。
- [ ] 普通用户、无管理员权限可以运行。
- [ ] 安装在 `/Applications` 之外可以运行。
- [ ] 中文、空格和 Unicode 路径可以运行。
- [ ] 无网络和缺少 API Key 时可以打开应用并显示合理提示。
- [ ] 数据、缓存和日志写入规范目录。
- [ ] 已有数据库升级前备份并成功迁移。

## 7. 签名、公证和 Gatekeeper

- [ ] 所有内部 Mach-O、Framework 和插件逐层签名。
- [ ] 使用 Developer ID Application 证书。
- [ ] Hardened Runtime 已启用。
- [ ] `codesign --verify --deep --strict` 通过。
- [ ] App 已通过 Apple Notary Service。
- [ ] App 公证票据 staple 验证通过。
- [ ] DMG 已签名并通过公证。
- [ ] DMG staple 验证通过。
- [ ] 从浏览器下载后 Gatekeeper 首次打开通过。

## 8. 发布制品

- [ ] `AutoCat-<version>-macOS-arm64.dmg`
- [ ] `AutoCat-<version>-macOS-x86_64.dmg`
- [ ] `SHA256SUMS`
- [ ] `build-manifest.json`
- [ ] `sbom.cdx.json`
- [ ] `THIRD_PARTY_LICENSES.txt`
- [ ] `test-report.json`
- [ ] 发布说明
- [ ] 已创建对应 GitLab Release。

## 9. 最终批准

- [ ] 所有必须门禁通过。
- [ ] arm64 安装包批准：
- [ ] x86_64 安装包批准：
- [ ] 签名/公证批准：
- [ ] 最终发布批准：
