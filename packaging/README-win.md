# AutoCat Windows 打包规范

> 构建目标：Windows 11 x86_64 安装包 (.exe)，内部使用，无签名

## 快速开始（Windows 机器上）

```powershell
# 1. 安装构建工具
winget install Python.Python.3.12 NSIS.NSIS

# 2. 克隆代码
git clone https://github.com/your-repo/AutoCat.git
cd AutoCat

# 3. 运行打包
python build_win.py
```

输出：`dist/AutoCat-3.0.1-windows-x86_64.exe`

---

## 构建流程

```
build_win.py
  ├── [1/5] 下载 Python 3.12 embeddable + FFmpeg gyan.dev essential
  ├── [2/5] pip install 依赖到 embeddable site-packages
  ├── [3/5] PyInstaller onedir (AutoCat.spec)
  ├── [4/5] 打包 FFmpeg/Python embeddable 到 onedir/
  └── [5/5] NSIS 生成安装程序
```

## 文件说明

| 文件 | 用途 |
|------|------|
| `build_win.py` | Windows 打包入口脚本 |
| `packaging/AutoCat-win.spec` | PyInstaller spec（onedir 模式） |
| `packaging/AutoCat-win.nsi` | NSIS 安装脚本 |
| `packaging/requirements-win.lock` | Windows 依赖锁文件 |

## 技术决策

| 项目 | 方案 |
|------|------|
| Python | CPython 3.12 embeddable（绿色，内嵌到 onedir） |
| FFmpeg | gyan.dev essential build（随包分发，不依赖系统） |
| 打包工具 | PyInstaller onedir |
| 安装程序 | NSIS |
| 最低系统 | Windows 10 1903+ |
| 架构 | x86_64 |

## 数据目录（Windows 惯例）

| 数据类型 | 路径 |
|---------|------|
| 用户配置/数据库 | `%APPDATA%\AutoCat` |
| 缓存 | `%LOCALAPPDATA%\AutoCat` |
| 日志 | `%LOCALAPPDATA%\AutoCat\logs` |

## 已知限制

1. **SmartScreen 拦截**：无签名安装程序首次安装时 Windows SmartScreen 会显示警告，点击"仍要运行"即可
2. **杀毒软件误报**：PyInstaller 打包的程序可能被某些杀毒软件标记为可疑（所有 PyInstaller 程序都面临此问题）
3. **FFmpeg 体积**：gyan.dev essential ~70MB，解压后含调试符号约 120MB

## 分发清单

每个正式版本应包含：

- `AutoCat-{version}-windows-x86_64.exe` — NSIS 安装程序
- `SHA256SUMS` — 安装包校验和
- `requirements-win.lock` — 依赖快照
