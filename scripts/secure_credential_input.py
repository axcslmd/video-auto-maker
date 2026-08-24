#!/usr/bin/env python3
"""Read a credential from an OS password prompt without chat or shell-history exposure.

This module is intentionally UI-only. Validation, session binding, quarantine,
and persistence remain owned by :mod:`key_setup`. Providers must return the
secret through captured process output; callers must never print that value.
"""
import getpass
import os
import shutil
import subprocess
import sys


class SecureInputError(RuntimeError):
    """Base error whose message is safe to show to a customer."""


class SecureInputUnavailable(SecureInputError):
    """No supported non-chat credential prompt is available."""


class SecureInputCancelled(SecureInputError):
    """The customer closed the prompt without submitting a credential."""


_MACOS_SCRIPT = r'''
on run argv
    set dialogTitle to item 1 of argv
    set dialogMessage to item 2 of argv
    try
        set response to display dialog dialogMessage default answer "" with title dialogTitle buttons {"取消", "安全保存"} default button "安全保存" cancel button "取消" with hidden answer
        return text returned of response
    on error number -128
        error number -128
    end try
end run
'''.strip()


_WINDOWS_SCRIPT = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = 'BasicRouter 安全密钥录入'
$form.StartPosition = 'CenterScreen'
$form.Size = New-Object System.Drawing.Size(520,190)
$form.TopMost = $true
$label = New-Object System.Windows.Forms.Label
$label.Text = '请输入新密钥。内容不会进入聊天记录或命令历史。'
$label.AutoSize = $true
$label.Location = New-Object System.Drawing.Point(18,20)
$box = New-Object System.Windows.Forms.TextBox
$box.UseSystemPasswordChar = $true
$box.Size = New-Object System.Drawing.Size(465,25)
$box.Location = New-Object System.Drawing.Point(18,55)
$ok = New-Object System.Windows.Forms.Button
$ok.Text = '安全保存'
$ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
$ok.Location = New-Object System.Drawing.Point(310,98)
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = '取消'
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$cancel.Location = New-Object System.Drawing.Point(405,98)
$form.Controls.AddRange(@($label,$box,$ok,$cancel))
$form.AcceptButton = $ok
$form.CancelButton = $cancel
$form.Add_Shown({$box.Select()})
if ($form.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($box.Text)
    exit 0
}
exit 2
'''.strip()


def _completed_secret(command):
    """Run a constant prompt command and return captured stdout only on success."""
    try:
        result = subprocess.run(
            command, check=False, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
    except OSError as exc:
        raise SecureInputUnavailable("本机安全输入组件无法启动。") from exc
    if result.returncode != 0:
        raise SecureInputCancelled("已取消安全密钥录入。")
    secret = (result.stdout or "").strip()
    if not secret:
        raise SecureInputCancelled("未输入密钥，安全录入已取消。")
    return secret


def _macos_prompt():
    executable = shutil.which("osascript")
    if not executable:
        raise SecureInputUnavailable("当前 macOS 缺少系统安全输入组件。")
    return _completed_secret([
        executable, "-l", "AppleScript", "-e", _MACOS_SCRIPT, "--",
        "BasicRouter 安全密钥录入",
        "请输入新密钥。内容不会进入聊天记录、模型上下文或命令历史。",
    ])


def _windows_prompt():
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        raise SecureInputUnavailable("当前 Windows 缺少系统安全输入组件。")
    return _completed_secret([
        executable, "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", _WINDOWS_SCRIPT,
    ])


def _linux_prompt():
    executable = shutil.which("zenity")
    if not executable:
        raise SecureInputUnavailable("当前 Linux 宿主未提供安全凭证框，也未安装 zenity。")
    return _completed_secret([
        executable, "--password", "--title=BasicRouter 安全密钥录入",
        "--text=请输入新密钥；内容不会进入聊天记录或命令历史。",
    ])


def _tty_prompt():
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise SecureInputUnavailable("当前宿主没有可用的安全凭证输入通道。")
    try:
        secret = getpass.getpass("BasicRouter 密钥（输入不回显）：")
    except (EOFError, KeyboardInterrupt) as exc:
        raise SecureInputCancelled("已取消安全密钥录入。") from exc
    if not secret.strip():
        raise SecureInputCancelled("未输入密钥，安全录入已取消。")
    return secret.strip()


def read_secret(provider="auto"):
    """Return a secret from a supported password UI, never from ordinary chat.

    ``auto`` prefers a native graphical prompt and uses a non-echoing terminal
    only when the process already owns an interactive TTY.
    """
    selected = str(provider or "auto").strip().lower()
    if selected not in {"auto", "macos", "windows", "linux", "tty"}:
        raise SecureInputUnavailable("未知的安全输入方式。")
    if selected == "macos" or (selected == "auto" and sys.platform == "darwin"):
        return _macos_prompt()
    if selected == "windows" or (selected == "auto" and os.name == "nt"):
        return _windows_prompt()
    if selected == "linux" or (selected == "auto" and sys.platform.startswith("linux")):
        try:
            return _linux_prompt()
        except SecureInputUnavailable:
            if selected != "auto":
                raise
    return _tty_prompt()
