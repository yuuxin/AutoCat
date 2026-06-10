"""AutoCat 入口"""

import os
import sys


def main():
    """命令行入口"""
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "import":
            from autokat.core.material import import_files
            stats = import_files(sys.argv[2:], generate_kenburns=True)
            print(f"导入完成: {stats}")
        elif cmd == "generate":
            # CLI 调用走和 GUI 完全一样的参数路径，确保效果一致
            from autokat.core.cli_runner import run_generate
            from pathlib import Path

            if len(sys.argv) < 3:
                print("用法: autokat generate '文案内容' [count=100] [options]")
                print()
                print("选项:")
                print("  --lang <zh|th|en>           语言（默认 zh）")
                print("  --count <N>                生成视频数量（默认 100）")
                print("  --workers <N>              并发进程数（默认 2）")
                print("  --fps <30|60>              帧率（默认 30）")
                print("  --voice <name>             TTS 音色（如 th-TH-PremwadeeNeural）")
                print("  --rate <pct>               语速 -50..+50（如 -5）")
                print("  --pitch <hz>               音调 -50..+50 Hz")
                print("  --min-shot-duration <s>    每段最短秒数（默认 2.0）")
                print("  --bgm <path>               指定 BGM 文件路径")
                print("  --no-bgm                   不用 BGM")
                print("  --materials <id1,id2,...>  限定素材池（逗号分隔 id）")
                print()
                print("例: autokat generate '你好世界' 5 --lang zh --rate -5")
                return

            text = sys.argv[2]
            # 解析 args（key=val 或 --key val）
            opts = {}
            i = 3
            while i < len(sys.argv):
                a = sys.argv[i]
                if a.startswith("--") and i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
                    opts[a[2:].replace("-", "_")] = sys.argv[i + 1]
                    i += 2
                elif a.startswith("--"):
                    opts[a[2:].replace("-", "_")] = True
                    i += 1
                else:
                    # 位置参数（count）
                    if "count" not in opts:
                        opts["count"] = int(a)
                    i += 1

            try:
                task_id = run_generate(
                    text=text,
                    name=opts.pop("name", "CLI生成"),
                    **opts,
                )
                print(f"任务已创建: task_id={task_id}")
                print(f"  查看进度: autokat status {task_id}")
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"生成失败: {e}")
                sys.exit(1)
        elif cmd == "resume":
            from autokat.core.renderer import resume_pending_tasks
            resume_pending_tasks()
        elif cmd == "ui":
            from autokat.ui.main_window import run_ui
            run_ui()
        elif cmd == "init":
            from autokat.models.db import init_db
            init_db()
            print("数据库已初始化")
        elif cmd == "bgm-split":
            # 智能拆 BGM 为多个短段
            # 用法: autokat bgm-split [files...] [-n 3] [-l 30] [--force]
            from pathlib import Path
            from autokat.core.bgm import split_bgm_to_segments, get_bgm_files

            args = sys.argv[2:]
            files = []
            num_segments = 3
            segment_length = 30.0
            min_gap = 3.0
            force = False
            i = 0
            while i < len(args):
                a = args[i]
                if a in ("-n", "--num-segments") and i + 1 < len(args):
                    num_segments = int(args[i + 1])
                    i += 2
                elif a in ("-l", "--length") and i + 1 < len(args):
                    segment_length = float(args[i + 1])
                    i += 2
                elif a in ("-g", "--min-gap") and i + 1 < len(args):
                    min_gap = float(args[i + 1])
                    i += 2
                elif a == "--force":
                    force = True
                    i += 1
                elif a.startswith("-"):
                    print(f"未知参数: {a}")
                    return
                else:
                    files.append(a)
                    i += 1

            targets = files if files else get_bgm_files()
            if not targets:
                print("没有可处理的 BGM 文件")
                return

            print(f"准备处理 {len(targets)} 个 BGM 文件，每首拆 {num_segments} 段 × {segment_length}s（min_gap={min_gap}s）\n")
            for f in targets:
                print(f"--- {Path(f).name} ---")
                results = split_bgm_to_segments(
                    f,
                    segment_length=segment_length,
                    num_segments=num_segments,
                    min_gap=min_gap,
                    skip_existing=not force,
                )
                for r in results:
                    status = "已存在" if r["skipped"] else "新建"
                    print(f"  [{status}] {Path(r['path']).name}  {r['start']:.1f}s - {r['end']:.1f}s  (能量 {r['energy']:.4f})")
                print()
        else:
            print(f"未知命令: {cmd}")
            print("可用命令: init, import, generate, resume, ui, bgm-split")
    else:
        # 默认启动 UI
        from autokat.ui.main_window import run_ui
        run_ui()


if __name__ == "__main__":
    main()
