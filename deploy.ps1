# 数字员工一键部署 — Windows (PowerShell)
# 用法：解压交付包后，右键 deploy.ps1 → 用 PowerShell 运行；
#       或在包目录 PowerShell 里执行：  powershell -ExecutionPolicy Bypass -File .\deploy.ps1
$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Here

$arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLower()
$offlineTag = if ($arch -eq "arm64") { "win-arm64" } else { "win-x64" }
$Inline = $env:AGENT_INLINE_BOOTSTRAP

function Log($m){ Write-Host "`n> $m" -ForegroundColor Cyan }
function Ok($m){ Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m){ Write-Host "  [!] $m" -ForegroundColor Yellow }
function Err($m){ Write-Host "  [X] $m" -ForegroundColor Red }

Write-Host "===============================================" 
Write-Host " 数字员工部署向导 (Windows)"
Write-Host " 目录: $Here"
Write-Host "==============================================="

# ---------- 1. Python3 ----------
Log "1/8 检测 Python3"
$py = $null
foreach ($c in @("python","python3","py")) {
  if (Get-Command $c -ErrorAction SilentlyContinue) {
    $v = & $c --version 2>&1
    if ($v -match "Python 3") { $py = $c; Ok "已找到 $v ($c)"; break }
  }
}
if (-not $py) {
  Warn "未找到 Python3，尝试用 winget 自动安装…"
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    Warn "安装后请【关闭并重开】PowerShell 再重跑本脚本（让 PATH 生效）。"
    exit 1
  } else {
    Err "未装 Python3 且无 winget。请手动安装 Python 3：https://www.python.org/downloads/ （安装时勾选 Add to PATH），然后重跑。"
    exit 1
  }
}

Log "1b/8 检查本地初始化加速包"
& $py scripts\offline_setup.py hydrate 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
  Ok "本地初始化加速包已就绪"
} else {
  Warn "本地初始化加速包预热失败，继续常规安装路径"
}

if (Test-Path "offline-assets\node-runtime\bin\node.exe") {
  $env:Path = "$Here\offline-assets\node-runtime\bin;" + $env:Path
}

$OfflineMode = ((Test-Path "offline-assets\python-wheels") -and
                (Test-Path "offline-assets\chrome\chrome-headless-shell-$offlineTag.zip") -and
                (Test-Path "offline-assets\node-runtime\bin\node.exe"))

# ---------- 2. venv ----------
if ($Inline) {
  $pybin = $py
  Log "2/8 [inline] 跳过 venv，依赖装入当前 Agent 使用的 Python"
} else {
  Log "2/8 创建隔离虚拟环境 (.venv)"
  if (-not (Test-Path ".venv")) { & $py -m venv .venv }
  $pybin = $py
  if (Test-Path ".venv\Scripts\python.exe") {
    $pybin = ".venv\Scripts\python.exe"
    Ok "虚拟环境已就绪"
  } else {
    Warn "未用 venv，改用系统 Python + --user"
  }
}

# ---------- 3. 依赖 ----------
Log "3/8 安装运行依赖 (requirements.lock.txt)"
if (Test-Path "requirements.lock.txt") { $reqFile = "requirements.lock.txt" } else { $reqFile = "requirements.txt" }
if ($OfflineMode) {
  $pythonCommand = Get-Command $pybin -ErrorAction SilentlyContinue
  $env:PYTHON = if ($pythonCommand) { $pythonCommand.Source } else { $pybin }
  & $pybin scripts\offline_setup.py install
  if ($LASTEXITCODE -ne 0) { Err "离线资源与当前平台不匹配或不完整。"; exit 1 }
  Ok "离线 Python wheels 与本地运行时就绪"
} else {
  & $pybin -m pip install --upgrade pip -q 2>$null
  & $pybin -m pip install -q -r $reqFile
  if ($LASTEXITCODE -ne 0) {
    Warn "常规安装失败，重试 --user…"
    & $pybin -m pip install -q --user -r $reqFile
    if ($LASTEXITCODE -ne 0) { Err "依赖安装失败，请检查网络（能否访问 PyPI）。"; exit 1 }
  }
}
Ok "依赖安装完成"

# ---------- 4. Node.js（HyperFrames 字幕/动效引擎需要）----------
Log "4/8 检测并安装 Node.js"
if ((Get-Command node -ErrorAction SilentlyContinue) -and (Get-Command npm -ErrorAction SilentlyContinue)) {
  Ok "已找到 Node $(node -v) / npm $(npm -v)"
} else {
  Warn "未找到 Node.js，尝试用 winget 自动安装…"
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install -e --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
    Warn "Node 安装后 PATH 可能需要【关闭并重开 PowerShell】才生效。若下一步报错，请重开后重跑本脚本。"
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
  } else {
    Err "无 winget 无法自动装 Node.js。请手动安装 LTS：https://nodejs.org/ 后重跑。"
  }
}

# ---------- 5. 宿主 Agent ----------
Log "5/8 保留当前宿主 Agent"
Ok "部署脚本仅安装业务运行依赖，不安装或切换 Kilo、Codex、Hermes 等宿主 Agent"

# ---------- 6. HyperFrames 字幕/动效引擎 + 外部 ffmpeg 检查 ----------
Log "6/8 检查 HyperFrames 与外部 ffmpeg/ffprobe"
if (Get-Command npx -ErrorAction SilentlyContinue) {
  $ffdir = & $pybin -c "import os,sys; sys.path.insert(0,'scripts'); import media_tools; ff,fp=media_tools.resolve_ffmpeg_pair(); print(os.path.dirname(ff) if ff and fp else '')" 2>$null | Select-Object -Last 1
  if (-not $ffdir) { Err "未找到外部 ffmpeg/ffprobe。请单独安装并放入 PATH，或设置 BASICROUTER_FFMPEG_DIR 后重跑。"; exit 1 }
  $env:Path = "$ffdir;" + $env:Path; Ok "外部 ffmpeg+ffprobe 就绪 ($ffdir)"
   if (Test-Path "$Here\offline-assets\node-runtime\bin\node.exe") {
     npx --offline --prefix "$Here\offline-assets\node-runtime" hyperframes doctor 2>$null | Out-Null
   } else {
     npx --yes hyperframes doctor 2>$null | Out-Null
   }
  if ($LASTEXITCODE -eq 0) { Ok "HyperFrames 就绪" } else { Warn "HyperFrames 预热未完全成功，首次出片时会自动补齐（需联网）。" }
} else {
  Warn "无 npx，HyperFrames 不可用；动态文字将回退到本地 libass 兜底引擎。"
}

# ---------- 7. Remotion 运镜引擎预热 ----------
Log "7/8 预热 Remotion 运镜引擎（首次 npm install + 下载 Chrome，较慢）"
if ((Get-Command npm -ErrorAction SilentlyContinue) -and (Test-Path "remotion_engine")) {
  Push-Location remotion_engine
  # The delivery bundle may contain macOS ARM node_modules. Windows must
  # always rebuild platform-specific packages instead of executing them.
  npm ci --include=dev --no-audit --no-fund 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { Ok "Remotion 依赖安装完成" } else { Warn "Remotion 依赖安装未完全成功，首次出片会自动重试（需联网）。" }
  Pop-Location
  & $pybin scripts\offline_setup.py install-chrome 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { Ok "Remotion Chrome 已从离线包安装" }
  & $pybin scripts\remotion_engine.py doctor 2>$null | Out-Null
  if ($LASTEXITCODE -eq 0) { Ok "Remotion 运镜引擎就绪" } else { Warn "Remotion 预热未完全成功，首次运镜出片时会自动补齐 Chrome（需联网）。" }
} else {
  Warn "无 npm 或缺 remotion_engine\，运镜功能暂不可用（补齐 Node 后重跑本脚本）。"
}

# ---------- 8. 自检 ----------
Log "8/8 环境自检"
& $pybin scripts\setup_env.py full-check
if ($LASTEXITCODE -ne 0) {
  Err "环境自检仍有缺项，部署未完成。请按上方提示修复后重跑。"
  exit 1
}

Write-Host ""
Write-Host "==============================================="
if ($Inline) {
  Ok "环境自举完成（inline）"
} else {
  Ok "部署完成"
  Write-Host "  下一步："
  Write-Host "   1) 用你选择的兼容 Agent 打开本目录，并确保它读取 AGENTS.md 与对应宿主适配入口"
  Write-Host "   2) 启动 /video-auto-maker（或该宿主的同名工作流）"
  Write-Host "  * 如用了虚拟环境，之后每次先执行:  .venv\Scripts\Activate.ps1"
}
Write-Host "==============================================="
