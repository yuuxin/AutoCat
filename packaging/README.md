# AutoCat 发布规范目录

本目录保存 AutoCat macOS 正式发布所需的策略和检查材料。

| 文件 | 用途 |
|---|---|
| [`../docs/MACOS_PACKAGING_STANDARD.md`](../docs/MACOS_PACKAGING_STANDARD.md) | 完整打包、兼容、签名、公证和验收标准 |
| [`release-policy.toml`](release-policy.toml) | 供后续构建脚本和 CI 读取的机器可读策略 |
| [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md) | 每个正式版本逐项签字确认 |
| [`GITLAB_RELEASE_PIPELINE.md`](GITLAB_RELEASE_PIPELINE.md) | GitLab Runner、流水线和密钥管理要求 |

## 当前状态

这些文件定义的是新发布流程的目标标准，不代表当前旧打包脚本已经符合要求。

当前：

- `build_app.py` 是旧方案；
- `make dmg` 仍会调用旧方案；
- 旧 DMG 不得作为正式用户安装包发布；
- 尚未创建生产 `.gitlab-ci.yml`，避免误触发不完整的发布流程。

后续实施应按照完整标准第 13 节完成 PyInstaller spec、依赖锁、FFmpeg 制品、原生审计、打包后测试、签名、公证和 GitLab CI。
