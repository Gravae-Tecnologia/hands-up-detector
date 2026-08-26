#!/usr/bin/env bash
# Instala o endpoint de inferencia numa VM com GPU.
#
# Aqui o peso nao importa (a VM tem disco e GPU), entao usa-se ultralytics +
# rtmlib direto. O ganho de sair da Pi so existe se a nuvem rodar modelo
# GRANDE: yolo11x+rtmpose-x deu F1 97,9 contra 90,1 do yolo11n-256 local.
#
#   ./instalar.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
# CUDA 12: o onnxruntime-gpu 1.29 exige CUDA 13, que o torch cu124 nao traz.
"$DIR/venv/bin/pip" install -q torch torchvision --index-url https://download.pytorch.org/whl/cu124
"$DIR/venv/bin/pip" install -q ultralytics rtmlib "onnxruntime-gpu==1.22.0"

bash "$DIR/baixar_modelos.sh"

"$DIR/venv/bin/python" -c "
import torch, onnxruntime as ort
print(f'torch {torch.__version__} cuda={torch.cuda.is_available()}')
print('ort providers:', ort.get_available_providers())"

cat <<MSG

pronto. para subir o endpoint:

  $DIR/venv/bin/python $DIR/servidor.py --porta 8091 --det yolo11x --pose rtmpose-x

exponha a 8091 para as Raspberries (firewall/VPC) e aponte o painel com
--nuvem http://<ip-da-vm>:8091
MSG
