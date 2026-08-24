#!/usr/bin/env python3
"""One graphical form for text/image/video proxy base URLs."""
import shutil
import subprocess
import sys


class EndpointInputError(RuntimeError):
    pass


class EndpointInputUnavailable(EndpointInputError):
    pass


class EndpointInputCancelled(EndpointInputError):
    pass


def _tk_form(initial=None):
    try:
        import tkinter as tk
        from tkinter import messagebox
    except ImportError as exc:
        raise EndpointInputUnavailable("当前 Python 缺少图形对话框组件。") from exc
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise EndpointInputUnavailable("当前桌面环境无法打开 API 地址对话框。") from exc

    initial = initial or {}
    result = {}
    root.title("营销视频 Agent · API 中转地址初始化")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    frame = tk.Frame(root, padx=22, pady=18)
    frame.pack()
    tk.Label(
        frame, text="请一次填写三类 API 的中转基础地址（不要包含具体接口路径）",
        anchor="w").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
    fields = {}
    for row, (kind, label) in enumerate(
            (("text", "文本 API"), ("image", "图片 API"), ("video", "视频 API")), 1):
        tk.Label(frame, text=label + "：", width=10, anchor="e").grid(
            row=row, column=0, sticky="e", pady=5)
        entry = tk.Entry(frame, width=58)
        entry.insert(0, initial.get(kind, ""))
        entry.grid(row=row, column=1, sticky="we", pady=5)
        fields[kind] = entry
    cancelled = {"value": True}

    def submit():
        values = {kind: field.get().strip() for kind, field in fields.items()}
        if not all(values.values()):
            messagebox.showerror("填写不完整", "文本、图片、视频三个 API 地址都必须填写。", parent=root)
            return
        if len(set(value.rstrip("/") for value in values.values())) != 3:
            messagebox.showerror("地址需分别填写", "三类 API 必须使用三个不同的中转地址。", parent=root)
            return
        result.update(values)
        cancelled["value"] = False
        root.destroy()

    buttons = tk.Frame(frame)
    buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(16, 0))
    tk.Button(buttons, text="取消", width=10, command=root.destroy).pack(side="left", padx=(0, 8))
    tk.Button(buttons, text="保存", width=10, command=submit, default="active").pack(side="left")
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.bind("<Return>", lambda _event: submit())
    root.bind("<Escape>", lambda _event: root.destroy())
    fields["text"].focus_set()
    root.update_idletasks()
    root.geometry("+%d+%d" % (
        max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2),
        max(0, (root.winfo_screenheight() - root.winfo_height()) // 3)))
    root.mainloop()
    if cancelled["value"]:
        raise EndpointInputCancelled("已取消 API 中转地址初始化。")
    return result


_MACOS_FIELD_SCRIPT = r'''
on run argv
    set fieldName to item 1 of argv
    set defaultValue to item 2 of argv
    try
        set response to display dialog ("请输入" & fieldName & "中转基础地址（不要包含具体接口路径）：") default answer defaultValue with title "营销视频 Agent · API 地址初始化" buttons {"取消", "下一步"} default button "下一步" cancel button "取消"
        return text returned of response
    on error number -128
        error number -128
    end try
end run
'''.strip()


def _macos_form(initial=None):
    executable = shutil.which("osascript")
    if not executable:
        raise EndpointInputUnavailable("当前 macOS 缺少系统对话框组件。")
    initial = initial or {}
    result = {}
    for kind, label in (("text", "文本 API"), ("image", "图片 API"), ("video", "视频 API")):
        completed = subprocess.run(
            [executable, "-l", "AppleScript", "-e", _MACOS_FIELD_SCRIPT,
             "--", label, initial.get(kind, "")],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True)
        if completed.returncode != 0:
            raise EndpointInputCancelled("已取消 API 中转地址初始化。")
        value = (completed.stdout or "").strip()
        if not value:
            raise EndpointInputCancelled("未填写完整，API 中转地址初始化已取消。")
        result[kind] = value
    return result


def read_endpoints(initial=None):
    try:
        return _tk_form(initial=initial)
    except EndpointInputUnavailable:
        if sys.platform == "darwin":
            return _macos_form(initial=initial)
        raise
