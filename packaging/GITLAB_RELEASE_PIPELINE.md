# GitLab macOS 发布流水线要求 (Apple Silicon + macOS 14+)

本文定义后续实现 `.gitlab-ci.yml` 时必须遵守的结构。当前仓库尚未完成新打包脚本，因此暂不提供会被误触发的生产 CI Job。
当前策略仅打 arm64 DMG, Intel Mac 用户走 Rosetta 2 转译, 不需要单独的 x86_64 Runner.

## 1. Runner

一个受保护 Runner 即可:

| Tag | 架构 | 用途 |
|---|---|---|
| `macos-14-arm64` | arm64 | Apple Silicon 构建 + macOS 14 最低系统验收 |

Runner 应是专用真机或受控虚拟机, 不能与开发者日常环境共用.
构建账户不得把个人 Homebrew、Conda、Python 配置暴露给发布 Job.
Intel Mac 用户走 Rosetta 2 转译, 由 packaging/RELEASE_CHECKLIST.md 第 5 节覆盖.

## 2. Pipeline 阶段

建议固定为：

```text
validate
  -> test
  -> build_arm64
  -> audit_arm64
  -> packaged_test_arm64
  -> sign
  -> notarize
  -> dmg
  -> clean_machine_acceptance
  -> release
```

`release` 只能依赖全部前置 Job 成功，不能设置为允许失败。

## 3. 触发规则

- 合并请求：运行源码测试、依赖解析验证和非签名测试构建。
- `main` 分支：运行完整测试与非发布构建，不上传正式 DMG。
- 受保护 Tag `v<major>.<minor>.<patch>`：运行完整签名、公证、验收和发布。
- 手工发布：仍需指向受保护 Tag，禁止从未提交工作树或任意分支发布。

## 4. GitLab 机密变量

正式发布预计需要以下 Protected/Masked Variables，具体名称在实现时统一：

- Developer ID 证书的加密 PKCS#12；
- 证书导入密码；
- 临时 Keychain 密码；
- App Store Connect Issuer ID；
- App Store Connect Key ID；
- App Store Connect API 私钥；
- 签名 Identity；
- Notary Team ID。

要求：

- 仅受保护 Tag 可读取；
- 不在 Job 日志中输出；
- Job 结束后删除临时 Keychain 和私钥；
- Fork、普通分支和合并请求不可读取；
- 不把证书打进缓存或 Artifact。

## 5. 缓存与 Artifact

可以缓存：

- 按 SHA-256 校验的 Python/依赖下载文件；
- 不包含密钥的只读 wheelhouse。

不得缓存：

- `.venv`；
- 已签名 `.app`；
- 临时 Keychain；
- 公证私钥；
- 上次构建的 `build/`、`dist/`；
- 用户数据和测试数据库。

中间 Artifact 必须带 Commit SHA、版本和架构 (arm64), 防止交叉使用.

## 6. 可追溯构建清单

每次构建生成 `build-manifest.json`，至少记录：

- Git Commit、Tag、是否 dirty；
- 产品版本；
- 构建时间；
- Runner 标识；
- macOS 和 Xcode 版本；
- CPU 架构；
- Python 和 PyInstaller 版本；
- 依赖锁文件 SHA-256；
- FFmpeg 制品 SHA-256；
- 模型文件 SHA-256；
- 签名证书 Team ID；
- 公证 Submission ID；
- 最终 DMG SHA-256。

## 7. CI 门禁

CI 必须调用仓库内版本控制的工具完成以下检查，而不是把复杂命令直接散落在 YAML：

- 版本一致性检查；
- 依赖锁验证；
- Mach-O 架构检查；
- deployment target 检查；
- 绝对路径和动态库依赖检查；
- 包内运行时冒烟测试；
- 签名、公证和 staple 检查；
- SBOM、许可证和 SHA-256 生成。

后续实现建议把这些能力放在 `packaging/` 下的独立脚本中，并为每个检查编写单元测试。

## 8. 首次启用 CI 前的条件

只有以下内容全部落地后，才能把生产 macOS 发布 Job 接入根 `.gitlab-ci.yml`：

- PyInstaller `.spec`；
- 单一 arm64 依赖锁文件；
- 受控 Python Runtime (python.org universal2 3.12.x) 获取方式；
- arm64 FFmpeg 制品；
- 原生依赖审计工具 (packaging/python_runtime_audit.py)；
- 打包后测试工具；
- 签名和公证工具；
- 一个 GitLab Runner (`macos-14-arm64`)；
- 至少一次手工执行的 macOS 14.x / 15.x / 16.x 干净机器验收。
