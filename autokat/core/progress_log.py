"""Renderer 进度日志 — 跨线程消息桥

工作线程（WorkerThread）调用 emit() 推送带时间戳的进度消息，
主线程在 UI 轮询时调用 drain() 取出所有待发消息，append 到 _wiz_log。
内部用 queue.Queue 实现线程安全。
"""

import queue
import threading
import time
from datetime import datetime

_log_queue: "queue.Queue[str]" = queue.Queue()

# 当前高层阶段（"配音中"/"脚本生成中"/"渲染中"）
# 任意工作线程调用 set_stage() 写入；UI 端 1s 轮询通过 get_stage() 读取。
# 用 Lock 保护，简单赋值在 CPython GIL 下也是原子的，Lock 只是显式表达意图。
_current_stage: str = ""
_current_stage_lock = threading.Lock()
_current_stage_at: float = 0.0  # 上次写入时间戳，用于"卡在 X 阶段已 X 秒"提示


def emit(message: str) -> None:
    """Renderer 工作线程调用：推一条带时间戳的进度消息"""
    _log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")


def drain() -> list:
    """UI 主线程调用：一次性取出所有待发消息，返回 list[str]"""
    msgs = []
    while True:
        try:
            msgs.append(_log_queue.get_nowait())
        except queue.Empty:
            break
    return msgs


def clear() -> None:
    """新任务开始时清空队列，避免上一次任务的消息泄漏到新任务的日志"""
    while True:
        try:
            _log_queue.get_nowait()
        except queue.Empty:
            break
    # 同时重置阶段（避免上次任务的 "TTS 8/12" 留到新任务里）
    global _current_stage, _current_stage_at
    with _current_stage_lock:
        _current_stage = ""
        _current_stage_at = 0.0


def set_stage(stage: str) -> None:
    """Renderer 工作线程调用：写入当前高层阶段

    UI 端通过 get_stage() 在 1s 轮询里读出来显示在 Step 4 顶部的"当前活动"标签，
    这样即便长时间没有新日志行，标签也会随阶段切换变化，让用户知道程序还在工作。
    """
    global _current_stage, _current_stage_at
    with _current_stage_lock:
        _current_stage = stage
        _current_stage_at = time.time()


def get_stage() -> tuple:
    """UI 主线程调用：返回 (stage_text, age_seconds)

    age_seconds 是距上次写入的秒数；用于判断"是否卡在同一个阶段太久"。
    """
    with _current_stage_lock:
        if not _current_stage:
            return ("", 0.0)
        return (_current_stage, time.time() - _current_stage_at)
