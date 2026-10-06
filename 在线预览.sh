#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ "${CODESPACES:-}" != "true" ]]; then
  echo "这个入口专门用于 GitHub Codespaces 在线预览。"
  echo "Windows 本机请运行“一键安装并启动.bat”或“启动照片筛选.bat”。"
  exit 2
fi

find_python() {
  local candidate
  for candidate in python3.11 python3.12 python3.10 python3.9 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" - <<'PY' >/dev/null 2>&1
import struct, sys
ok = (3, 9) <= sys.version_info[:2] <= (3, 12) and struct.calcsize("P") * 8 == 64
raise SystemExit(0 if ok else 1)
PY
      then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

BASE_PY="$(find_python || true)"
if [[ -z "$BASE_PY" ]]; then
  echo
  echo "[无法启动] 当前 Codespace 没有 Python 3.9–3.12 64 位环境。"
  echo "请按 Ctrl+Shift+P，运行：Codespaces: Rebuild Container"
  exit 3
fi

VENV="$ROOT/.codespaces-venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[1/4] 创建 Codespaces 独立 Python 环境..."
  rm -rf "$VENV"

  if ! "$BASE_PY" -m venv "$VENV"; then
    PY_MM="$("$BASE_PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    echo
    echo "      当前 Python 缺少 venv/ensurepip，正在自动修复 python${PY_MM}-venv..."

    if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update -qq
      sudo apt-get install -y "python${PY_MM}-venv"
      rm -rf "$VENV"
      "$BASE_PY" -m venv "$VENV"
    else
      echo
      echo "[无法自动修复] 当前 Codespace 不允许安装 python${PY_MM}-venv。"
      echo "请按 Ctrl+Shift+P，运行：Codespaces: Rebuild Container"
      exit 4
    fi
  fi
fi

PY="$VENV/bin/python"
echo "[2/4] 检查运行依赖..."
if ! "$PY" - <<'PY' >/dev/null 2>&1
import cv2, flask, numpy, PIL
PY
then
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt
fi

if [[ -f requirements-optional.txt ]]; then
  if ! "$PY" - <<'PY' >/dev/null 2>&1
import rawpy
from pillow_heif import register_heif_opener
PY
  then
    echo "      正在补充 RAW / HEIC 支持（失败不会阻止在线预览）..."
    "$PY" -m pip install -r requirements-optional.txt || true
  fi
fi

DEMO_DIR="$ROOT/.codespaces_demo"
mkdir -p "$DEMO_DIR"
if ! compgen -G "$DEMO_DIR/*.jpg" >/dev/null; then
  echo "[3/4] 生成在线样例照片..."
  DEMO_DIR="$DEMO_DIR" "$PY" - <<'PY'
import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

out = Path(os.environ["DEMO_DIR"])
out.mkdir(parents=True, exist_ok=True)

for i in range(12):
    w, h = 960, 640
    img = Image.new("RGB", (w, h), (235, 238, 244))
    d = ImageDraw.Draw(img)

    step = 32 + (i % 3) * 8
    for x in range(0, w, step):
        d.line((x, 0, w - x // 2, h), width=2 + i % 4,
               fill=(35 + i * 8, 65, 120 + i * 6))
    for y in range(0, h, step):
        d.line((0, y, w, h - y // 2), width=1 + (i % 3),
               fill=(110, 70 + i * 7, 60))
    d.ellipse((180 + i * 8, 120, 560 + i * 8, 500),
              outline=(25, 25, 25), width=10)
    d.rectangle((620, 120 + i * 7, 860, 420 + i * 4),
                outline=(20, 110, 80), width=8)

    if i >= 8:
        img = img.filter(ImageFilter.GaussianBlur(radius=5.0))
        kind = "blurry"
    elif i >= 4:
        img = img.filter(ImageFilter.GaussianBlur(radius=1.4))
        kind = "soft"
    else:
        kind = "sharp"

    img.save(out / f"sample_{i+1:02d}_{kind}.jpg", quality=92)
PY
fi

export PHOTOCURATOR_PORT="${PHOTOCURATOR_PORT:-5014}"
export PHOTOCURATOR_DEMO_DIR="$DEMO_DIR"

PID_FILE="$ROOT/.codespaces_preview.pid"
LOG_FILE="$ROOT/.codespaces_preview.log"
DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
PREVIEW_URL=""
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  PREVIEW_URL="https://${CODESPACE_NAME}-${PHOTOCURATOR_PORT}.${DOMAIN}"
fi

show_ready() {
  echo
  echo "在线预览已就绪。"
  [[ -n "$PREVIEW_URL" ]] && echo "地址：$PREVIEW_URL"
  echo "端口：$PHOTOCURATOR_PORT（建议保持 Private）"
  echo "服务已在后台常驻，终端返回提示符是正常现象。"
  echo "停止服务：bash 在线预览.sh stop"
  echo
}

if [[ "${1:-}" == "stop" ]]; then
  if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" >/dev/null 2>&1; then
      kill "$PID" >/dev/null 2>&1 || true
      sleep 0.4
      kill -9 "$PID" >/dev/null 2>&1 || true
      echo "已停止在线预览服务（PID $PID）。"
    fi
    rm -f "$PID_FILE"
  else
    echo "当前没有记录到正在运行的在线预览服务。"
  fi
  exit 0
fi

# Reuse any healthy process already serving the preview port.
if curl -fsS --max-time 2 "http://127.0.0.1:${PHOTOCURATOR_PORT}/" >/dev/null 2>&1; then
  echo "[4/4] 检测到在线预览已经在运行。"
  show_ready
  exit 0
fi

# Remove stale PID metadata from an earlier stopped session.
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" >/dev/null 2>&1; then
    kill "$OLD_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$PID_FILE"
fi

echo "[4/4] 正在后台启动在线预览..."
: > "$LOG_FILE"

nohup env   CODESPACES=true   CODESPACE_NAME="${CODESPACE_NAME:-}"   GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="$DOMAIN"   PHOTOCURATOR_PORT="$PHOTOCURATOR_PORT"   PHOTOCURATOR_DEMO_DIR="$PHOTOCURATOR_DEMO_DIR"   "$PY" "$ROOT/photo_curator.py"   >"$LOG_FILE" 2>&1 < /dev/null &

SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"

READY=0
for _ in {1..80}; do
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    break
  fi
  if curl -fsS --max-time 2 "http://127.0.0.1:${PHOTOCURATOR_PORT}/" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.25
done

if [[ "$READY" != "1" ]]; then
  echo
  echo "[启动失败] 后台服务没有通过健康检查。"
  echo "日志最后 80 行："
  echo "----------------------------------------"
  tail -n 80 "$LOG_FILE" || true
  echo "----------------------------------------"
  rm -f "$PID_FILE"
  exit 5
fi

show_ready
