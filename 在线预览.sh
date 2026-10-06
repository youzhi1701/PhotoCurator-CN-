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

  for candidate in /opt/python/3.11*/bin/python3.11 /opt/python/3.12*/bin/python3.12 /opt/python/3.10*/bin/python3.10 /opt/python/3.9*/bin/python3.9; do
    [[ -x "$candidate" ]] || continue
    if "$candidate" - <<'PY' >/dev/null 2>&1
import struct, sys
ok = (3, 9) <= sys.version_info[:2] <= (3, 12) and struct.calcsize("P") * 8 == 64
raise SystemExit(0 if ok else 1)
PY
    then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

BASE_PY="$(find_python || true)"
if [[ -z "$BASE_PY" ]]; then
  echo
  echo "[无法启动] 当前 Codespace 没有 Python 3.9–3.12 64 位环境。"
  echo "请按 Ctrl+Shift+P，运行：Codespaces: Rebuild Container"
  echo "仓库已提供 .devcontainer/devcontainer.json，会重建为 Python 3.11。"
  exit 3
fi

VENV="$ROOT/.codespaces-venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "[1/4] 创建 Codespaces 独立 Python 环境..."
  "$BASE_PY" -m venv "$VENV"
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

    # Dense geometric detail gives the sharpness detector real structure.
    step = 32 + (i % 3) * 8
    for x in range(0, w, step):
        d.line((x, 0, w - x // 2, h), width=2 + i % 4, fill=(35 + i * 8, 65, 120 + i * 6))
    for y in range(0, h, step):
        d.line((0, y, w, h - y // 2), width=1 + (i % 3), fill=(110, 70 + i * 7, 60))
    d.ellipse((180 + i * 8, 120, 560 + i * 8, 500), outline=(25, 25, 25), width=10)
    d.rectangle((620, 120 + i * 7, 860, 420 + i * 4), outline=(20, 110, 80), width=8)

    # Four clear, four slightly soft, four intentionally blurry samples.
    if i >= 8:
        img = img.filter(ImageFilter.GaussianBlur(radius=5.0))
        kind = "blurry"
    elif i >= 4:
        img = img.filter(ImageFilter.GaussianBlur(radius=1.4))
        kind = "soft"
    else:
        kind = "sharp"

    img.save(out / f"sample_{i+1:02d}_{kind}.jpg", quality=92)

print(f"generated {len(list(out.glob('*.jpg')))} demo photos in {out}")
PY
fi

export PHOTOCURATOR_PORT="${PHOTOCURATOR_PORT:-5014}"
export PHOTOCURATOR_DEMO_DIR="$DEMO_DIR"

DOMAIN="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  PREVIEW_URL="https://${CODESPACE_NAME}-${PHOTOCURATOR_PORT}.${DOMAIN}"
  echo
  echo "[4/4] 在线预览正在启动"
  echo "地址：$PREVIEW_URL"
  echo "端口：$PHOTOCURATOR_PORT（建议保持 Private）"
  echo
else
  echo "[4/4] 在线预览正在启动，等待 Codespaces 转发端口..."
fi

exec "$PY" photo_curator.py
