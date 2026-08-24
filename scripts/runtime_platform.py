#!/usr/bin/env python3
"""Platform-aware runtime bundle decisions."""
import os
import platform


def platform_tag():
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return "darwin-arm64" if machine in ("arm64", "aarch64") else "darwin-x64"
    if system == "windows":
        return "win-arm64" if machine in ("arm64", "aarch64") else "win-x64"
    return "linux-arm64" if machine in ("arm64", "aarch64") else "linux-x64"


def bundled_node_modules_usable(engine_dir):
    """Bundled node_modules is only usable on its recorded platform."""
    expected = "darwin-arm64"
    return platform_tag() == expected and os.path.exists(
        os.path.join(engine_dir, "node_modules", ".bin", "remotion"))
