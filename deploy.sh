#!/usr/bin/env bash
# 数字员工一键部署 — macOS / Linux 裸机
# 用法：解压交付包后，在包目录里执行  ./deploy.sh
# 目标：具备系统 CPython 3.9 的 macOS ARM 客户机可离线自动就绪。
# Node、Python wheels、浏览器与媒体引擎均由包内提供，全程无需管理员权限。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
echo "==============================================="
echo " 数字员工部署向导 (macOS/Linux)"
echo " 目录: $HERE"
echo "==============================================="

log() { printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }
ok()  { printf "\033[1;32m  ✅ %s\033[0m\n" "$1"; }
warn(){ printf "\033[1;33m  ⚠ %s\033[0m\n" "$1"; }
err() { printf "\033[1;31m  ❌ %s\033[0m\n" "$1"; }

OS="$(uname -s)"
ARCH="$(uname -m)"

# INLINE 模式：由任意兼容 Agent 的主入口或 setup 工作流首次自举调用。
# 与独立终端部署的区别：不建 venv，依赖装到当前 Agent 调用的同一个 python3，
# 否则后续 python3 scripts/*.py 可能看不到依赖。部署脚本不安装或切换宿主 Agent。
# 触发：AGENT_INLINE_BOOTSTRAP=1 bash deploy.sh
INLINE="${AGENT_INLINE_BOOTSTRAP:-${MOMAX_INLINE_BOOTSTRAP:-}}"

# ---------- 1. Python3 ----------
log "1/8 检测 Python3"
if command -v python3 >/dev/null 2>&1; then
  PY=python3
  ok "已找到 $(python3 --version 2>&1)"
else
  if [ -f "$HERE/offline-assets/artifact-manifest.json" ]; then
    err "当前是严格离线包，但系统没有 Python3；请先在有网环境安装 Python 3.9 后再离线部署。本脚本不会联网安装。"
    exit 1
  fi
  warn "未找到 python3，尝试自动安装…"
  if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      brew install python3 && PY=python3
    else
      err "macOS 未装 python3 也无 Homebrew。请先安装 Python 3：https://www.python.org/downloads/ 然后重跑本脚本。"
      exit 1
    fi
  else
    # Linux：尝试常见包管理器
    if command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip && PY=python3
    elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y python3 python3-pip && PY=python3
    elif command -v yum >/dev/null 2>&1; then sudo yum install -y python3 python3-pip && PY=python3
    else err "无法自动装 Python3，请手动安装后重跑。"; exit 1; fi
  fi
fi
command -v "$PY" >/dev/null 2>&1 || { err "Python3 仍不可用，终止。"; exit 1; }

log "1b/8 检查本地初始化加速包"
if "$PY" scripts/offline_setup.py hydrate >/dev/null 2>&1; then
  ok "本地初始化加速包已就绪"
else
  warn "本地初始化加速包预热失败，继续常规安装路径"
fi

if [ -x "$HERE/offline-assets/node-runtime/bin/node" ]; then
  export PATH="$HERE/offline-assets/node-runtime/bin:$PATH"
fi

OFFLINE_MODE=0
if [ "$OS" = "Darwin" ] && { [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; } \
   && [ -d "$HERE/offline-assets/python-wheels" ] \
   && [ -f "$HERE/offline-assets/chrome/chrome-headless-shell-darwin-arm64.zip" ]; then
  if "$PY" -c 'import sys; raise SystemExit(0 if sys.implementation.name == "cpython" and sys.version_info[:2] == (3,9) else 1)' 2>/dev/null; then
    OFFLINE_MODE=1
  else
    err "离线包要求 CPython 3.9，当前 Python ABI 不匹配；严格离线模式禁止改走在线安装。"
    exit 1
  fi
fi

if [ "$OFFLINE_MODE" -eq 1 ]; then
  export PIP_NO_INDEX=1
  export PIP_DISABLE_PIP_VERSION_CHECK=1
  export npm_config_offline=true
  export npm_config_audit=false
  export npm_config_fund=false
  ok "严格离线模式已启用：禁止 PyPI/npm/Chrome 网络下载"
fi

# ---------- 2. venv ----------
if [ -n "$INLINE" ]; then
  # inline：不建 venv，依赖装到当前 Agent 使用的 python3，保证 scripts/*.py 能 import
  PYBIN="$PY"
  log "2/8 [inline] 跳过 venv，依赖装入当前 Agent 使用的 Python"
else
  log "2/8 创建隔离虚拟环境 (.venv)"
  if [ ! -d .venv ]; then
    "$PY" -m venv .venv 2>/dev/null || warn "venv 创建失败，将用用户级 pip 安装"
  fi
  if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    . .venv/bin/activate
    PYBIN=python
    ok "虚拟环境已激活"
  else
    PYBIN="$PY"
    warn "未用 venv，改用系统 Python + --user"
  fi
fi

if [ "$OFFLINE_MODE" -eq 1 ]; then
  log "3/8 [offline] 安装包内 Python wheels（无网络）"
  if PYTHON="$PYBIN" "$PYBIN" scripts/offline_setup.py install; then
    ok "包内 Python wheels 与本地运行时就绪"
  else
    err "离线资源校验或安装失败；严格离线模式不会切换网络安装。"
    exit 1
  fi
fi

# ---------- 3. 依赖 ----------
log "3/8 安装运行依赖 (requirements.lock.txt)"
if [ "$OFFLINE_MODE" -eq 1 ]; then
  ok "离线模式跳过 pip 网络安装"
else
"$PYBIN" -m pip install --upgrade pip -q 2>/dev/null || true
REQ_FILE="requirements.lock.txt"
[ -f "$REQ_FILE" ] || REQ_FILE="requirements.txt"
if "$PYBIN" -m pip install -q -r "$REQ_FILE"; then
  ok "依赖安装完成"
else
  warn "常规安装失败，重试 --user…"
  "$PYBIN" -m pip install -q --user -r "$REQ_FILE" || { err "依赖安装失败，请检查网络（能否访问 PyPI）。"; exit 1; }
fi
# 在线路径装后断言：与离线路径的 SHA-256 树校验"殊途同验"——
# 逐模块 import + 版本下限断言，装错版本/缺包立即失败，不把坏环境带到运行时。
log "3b/8 在线安装后版本断言"
"$PYBIN" - <<'PYEOF' || { err "依赖版本断言失败：安装结果与 requirements 不符"; exit 1; }
import sys
REQUIRED = [
    ("pptx", "python-pptx", (0, 6, 21)),
    ("docx", "python-docx", (0, 8, 11)),
    ("pypdf", "pypdf", (6, 0, 0)),
    ("openpyxl", "openpyxl", (3, 0, 0)),
]
failed = []
for module, dist, minimum in REQUIRED:
    try:
        mod = __import__(module)
    except ImportError as exc:
        failed.append("%s 不可导入 (%s)" % (dist, exc))
        continue
    raw = getattr(mod, "__version__", None)
    if raw:
        try:
            version = tuple(int(part) for part in raw.split(".")[:3] if part.isdigit())
            if version and version < minimum:
                failed.append("%s 版本 %s 低于下限 %s" % (dist, raw, ".".join(map(str, minimum))))
        except (ValueError, AttributeError):
            pass  # 版本号不可解析时不误判
if failed:
    for item in failed:
        print("ASSERT-FAIL: %s" % item, file=sys.stderr)
    sys.exit(1)
print("依赖版本断言通过（%d 项）" % len(REQUIRED))
PYEOF
fi

# ---------- 4. Node.js（HyperFrames 字幕/动效引擎需要）----------
log "4/8 检测并安装 Node.js"
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  ok "已找到 Node $(node -v 2>/dev/null) / npm $(npm -v 2>/dev/null)"
else
  if [ "$OFFLINE_MODE" -eq 1 ]; then
    err "离线 Node runtime 缺失或不可执行；严格离线模式不会安装 Homebrew/Node。"
    exit 1
  fi
  warn "未找到 Node.js，尝试自动安装…"
  if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      brew install node && ok "Node.js 安装完成"
    else
      warn "macOS 无 Homebrew，尝试安装 Homebrew（可能需要输入密码）…"
      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/null 2>/dev/null || true
      for BP in /opt/homebrew/bin /usr/local/bin; do [ -x "$BP/brew" ] && eval "$("$BP/brew" shellenv)"; done
      if command -v brew >/dev/null 2>&1; then brew install node && ok "Node.js 安装完成"
      else err "无法自动装 Node.js。请手动安装：https://nodejs.org/ （LTS 版）后重跑本脚本。"; fi
    fi
  else
    if command -v apt-get >/dev/null 2>&1; then
      curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash - 2>/dev/null && sudo apt-get install -y nodejs && ok "Node.js 安装完成"
    elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y nodejs npm && ok "Node.js 安装完成"
    elif command -v yum >/dev/null 2>&1; then sudo yum install -y nodejs npm && ok "Node.js 安装完成"
    else err "无法自动装 Node.js，请手动安装 LTS 版后重跑。"; fi
  fi
fi

# ---------- 5. 宿主 Agent ----------
log "5/8 保留当前宿主 Agent"
ok "部署脚本仅安装业务运行依赖，不安装或切换 Kilo、Codex、Hermes 等宿主 Agent"

# ---------- 6. HyperFrames 字幕/动效引擎 + 外部 ffmpeg 检查 ----------
log "6/8 检查 HyperFrames 与外部 ffmpeg/ffprobe"
if command -v node >/dev/null 2>&1; then
  FFDIR="$("$PYBIN" -c 'import os,sys; sys.path.insert(0,"scripts"); import media_tools; ff,fp=media_tools.resolve_ffmpeg_pair(); print(os.path.dirname(ff) if ff and fp else "")' 2>/dev/null | tail -1)"
  if [ -z "$FFDIR" ]; then
    err "未找到外部 ffmpeg/ffprobe。请由用户单独安装并放入 PATH，或设置 BASICROUTER_FFMPEG_DIR后重跑。"
    exit 1
  fi
  export PATH="$FFDIR:$PATH" && ok "外部 ffmpeg+ffprobe 就绪 ($FFDIR)"
   # Probe the same HyperFrames command that hf_engine.py uses for rendering.
   # A separately audited private runtime may provide local Node/CLI/Chrome;
   # community source installations normally use npm plus a system browser.
   # FFmpeg remains external in both cases.
   ( cd "$HERE" && "$PYBIN" scripts/hf_engine.py doctor >/dev/null 2>&1 )
   if [ "$?" -eq 0 ]; then
     ok "HyperFrames 就绪"
   elif [ "$OFFLINE_MODE" -eq 1 ]; then
     err "包内 HyperFrames runtime 自检失败；严格离线模式不会联网补齐。"
     exit 1
   else
     warn "HyperFrames 预热未完全成功，首次出片时会自动补齐（需联网）。"
   fi
else
  if [ "$OFFLINE_MODE" -eq 1 ]; then
    err "离线 Node runtime 不可用；严格离线模式终止。"
    exit 1
  fi
  warn "无 Node.js，HyperFrames 不可用；动态文字将回退到本地 libass 兜底引擎。"
fi

# ---------- 7. Remotion 运镜引擎预热（npm install + Chrome Headless Shell）----------
log "7/8 预热 Remotion 运镜引擎（首次 npm install + 下载 Chrome，较慢）"
if command -v npm >/dev/null 2>&1 && [ -d remotion_engine ]; then
   NODE_PLATFORM="$(uname -s)-$(uname -m)"
   if [ "$NODE_PLATFORM" = "Darwin-arm64" ] && [ -x remotion_engine/node_modules/.bin/remotion ]; then
     ok "复用包内 macOS ARM Remotion 依赖"
     NPM_READY=0
   elif [ "$OFFLINE_MODE" -eq 1 ]; then
     err "包内 Remotion node_modules 不完整；严格离线模式不会执行 npm ci。"
     exit 1
   else
     ( cd remotion_engine && npm ci --include=dev --no-audit --no-fund >/dev/null 2>&1 )
     NPM_READY=$?
   fi
  if [ "$NPM_READY" -eq 0 ]; then
     ok "Remotion 依赖安装完成"
  else
     warn "Remotion 依赖安装未完全成功，首次出片会自动重试（需联网）。"
  fi
  if "$PYBIN" scripts/offline_setup.py install-chrome >/dev/null 2>&1; then
    ok "Remotion Chrome 已从离线包安装"
  elif [ "$OFFLINE_MODE" -eq 1 ]; then
    err "包内 Chrome 安装失败；严格离线模式不会下载 Chrome。"
    exit 1
  fi
  # doctor 会下载并修复 Chrome Headless Shell（本机自带解压有 bug，脚本会手动 unzip+chmod 兜底）
  "$PYBIN" scripts/remotion_engine.py doctor >/dev/null 2>&1 \
    && ok "Remotion 运镜引擎就绪" \
    || { [ "$OFFLINE_MODE" -eq 1 ] \
      && { err "Remotion 离线自检失败；严格离线模式终止。"; exit 1; } \
      || warn "Remotion 预热未完全成功，首次运镜出片时会自动补齐 Chrome（需联网）。"; }
else
  if [ "$OFFLINE_MODE" -eq 1 ]; then
    err "离线 npm 或 remotion_engine 缺失；严格离线模式终止。"
    exit 1
  fi
  warn "无 npm 或缺 remotion_engine/，运镜功能暂不可用（补齐 Node 后重跑本脚本）。"
fi

# ---------- 8. 自检 ----------
log "8/8 环境自检"
if ! "$PYBIN" scripts/setup_env.py full-check; then
  err "环境自检仍有缺项，部署未完成。请按上方提示修复后重跑。"
  exit 1
fi

echo ""
echo "==============================================="
if [ -n "$INLINE" ]; then
  ok "环境自举完成（inline）"
  # inline 模式由当前 Agent 的工作流继续往下走，不打印终端操作指引
else
  ok "部署完成"
  echo "  下一步："
  echo "   1) 用你选择的兼容 Agent 打开本目录，并确保它读取 AGENTS.md 与对应宿主适配入口"
  echo "   2) 启动 /video-auto-maker（或该宿主的同名工作流）——总入口，自动配 Key + 引导做视频"
  echo "  * 如用了虚拟环境，之后每次先执行:  source .venv/bin/activate"
fi
echo "==============================================="
