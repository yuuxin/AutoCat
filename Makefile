# AutoCat 构建与安装

.PHONY: install app dmg clean

# 开发安装
install:
	python3 -m venv .venv
	.venv/bin/pip install -e .

# 安装 AI 可选依赖（Qwen 文案生成）
install-ai:
	.venv/bin/pip install -e ".[ai]"

# 构建 macOS .app
app:
	.venv/bin/python3 build_app.py

# 构建 macOS .dmg 安装包
dmg:
	.venv/bin/python3 build_app.py --dmg

# 清理构建产物
clean:
	rm -rf dist build *.egg-info __pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
	find . -type f -name "*.pyc" -delete
