"""Zotero 进程管理：检测、关闭、重启。"""
import os
import platform
import subprocess
import sys
import time


def is_zotero_running() -> bool:
    """检测 Zotero 是否正在运行。"""
    try:
        if platform.system() == "Windows":
            r = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq zotero.exe"],
                capture_output=True, text=True, timeout=5,
            )
            return "zotero.exe" in r.stdout.lower()
        else:
            r = subprocess.run(["pgrep", "-x", "zotero"], capture_output=True, timeout=5)
            return r.returncode == 0
    except Exception:
        return False


def close_zotero(wait_secs: int = 30) -> bool:
    """向 Zotero 发送退出信号并等待其关闭。

    Returns:
        True  — Zotero 已关闭（或原本未运行）
        False — 超时，Zotero 仍在运行
    """
    if not is_zotero_running():
        return True
    try:
        if platform.system() == "Darwin":
            subprocess.run(
                ["osascript", "-e", 'tell application "Zotero" to quit'],
                capture_output=True, timeout=10,
            )
        elif platform.system() == "Windows":
            subprocess.run(["taskkill", "/IM", "zotero.exe"], capture_output=True, timeout=10)
        else:
            subprocess.run(["pkill", "-TERM", "-x", "zotero"], capture_output=True, timeout=10)
    except Exception as e:
        print(f"close_zotero: 发送退出信号失败: {e}", file=sys.stderr)
        return False

    deadline = time.time() + wait_secs
    while time.time() < deadline:
        if not is_zotero_running():
            return True
        time.sleep(1)
    return False


def reopen_zotero() -> None:
    """后台重新启动 Zotero（非阻塞）。"""
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", "-a", "Zotero"])
        elif platform.system() == "Windows":
            for path in [
                r"C:\Program Files\Zotero\zotero.exe",
                r"C:\Program Files (x86)\Zotero\zotero.exe",
            ]:
                if os.path.exists(path):
                    subprocess.Popen([path])
                    return
        else:
            subprocess.Popen(["zotero"])
    except Exception as e:
        print(f"reopen_zotero: 启动失败: {e}", file=sys.stderr)


def ensure_zotero_closed(step_name: str) -> bool:
    """确保 Zotero 已关闭再执行 DB 写入；若无法关闭则提示手动处理并返回 False。"""
    if not is_zotero_running():
        return True
    print(f"{step_name}: Zotero 正在运行，尝试优雅关闭…", file=sys.stderr)
    if close_zotero():
        print(f"{step_name}: Zotero 已关闭。", file=sys.stderr)
        return True
    print(
        f"{step_name}: 关闭 Zotero 超时，跳过数据库写入。\n"
        f"  请手动关闭 Zotero 后重新运行 --repair-only 或 --apply-abstracts。",
        file=sys.stderr,
    )
    return False
