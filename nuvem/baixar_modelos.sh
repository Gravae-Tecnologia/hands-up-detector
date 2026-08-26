#!/usr/bin/env bash
# Baixa os modelos grandes usados pelo endpoint da nuvem.
# Ficam fora do git: sao 300+ MB e o repositorio nao e lugar de peso morto.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/modelos"
mkdir -p "$DIR"

BASE=https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk
declare -A POSE=(
  [rtmpose-x]="rtmpose-x_simcc-body7_pt-body7_700e-384x288-71d7b7e9_20230629"
  [rtmpose-m]="rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504"
)
for nome in "${!POSE[@]}"; do
  [ -f "$DIR/$nome.onnx" ] && { echo "ja tem $nome"; continue; }
  echo "==> $nome"
  curl -fL "$BASE/${POSE[$nome]}.zip" -o /tmp/p.zip
  unzip -o -q /tmp/p.zip -d /tmp/p && find /tmp/p -name 'end2end.onnx' -exec mv {} "$DIR/$nome.onnx" \;
  rm -rf /tmp/p /tmp/p.zip
done

# o detector vem do ultralytics; o .pt e baixado na primeira execucao
VENV="$(dirname "$DIR")/venv/bin/python"
[ -x "$VENV" ] && "$VENV" -c "
from ultralytics import YOLO
import shutil, os
for n in ('yolo11x','yolo11n'):
    m = YOLO(n + '.pt')
    d = os.path.join('$DIR', n + '.pt')
    if not os.path.exists(d): shutil.copy(m.ckpt_path, d)
    print('ok', d)"
