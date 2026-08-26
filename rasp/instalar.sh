#!/usr/bin/env bash
# Instala o detector na Raspberry de uma arena.
#
# A meta e nao trazer peso: o OpenCV vem do apt (build da distro, sem
# compilar) e so o onnxruntime entra por pip, num venv com
# --system-site-packages para reaproveitar numpy e cv2 do sistema. Da 73 MB
# de venv. Nada de torch, ultralytics ou rtmlib - o pre e o pos dos modelos
# estao reimplementados em motor.py justamente para nao precisar deles.
#
#   ./instalar.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESTINO="${DESTINO:-$HOME/hands-up}"

echo "==> destino: $DESTINO"
mkdir -p "$DESTINO"
cp -r "$DIR"/*.py "$DIR/modelos" "$DESTINO/"

echo "==> opencv pelo apt (nao compila nada)"
sudo apt-get update -qq
sudo apt-get install -y -q python3-opencv ffmpeg

echo "==> venv com onnxruntime"
python3 -m venv --system-site-packages "$DESTINO/venv"
"$DESTINO/venv/bin/pip" install -q --disable-pip-version-check onnxruntime

echo "==> verificacao"
"$DESTINO/venv/bin/python" - <<'EOF'
import cv2, numpy, onnxruntime as ort
print(f"  numpy {numpy.__version__} | cv2 {cv2.__version__} | ort {ort.__version__}")
EOF

cat <<EOF

pronto. para subir o painel:

  # inferencia LOCAL na propria Pi
  $DESTINO/venv/bin/python $DESTINO/painel.py --porta 8090 --filtro campo01

  # inferencia na NUVEM (recomendado: libera a CPU da Pi)
  $DESTINO/venv/bin/python $DESTINO/painel.py --porta 8090 \\
      --nuvem http://SUA-VM:8091 --webhook https://SUA-PLATAFORMA/alerta

o painel fica em http://<ip-da-pi>:8090
EOF
