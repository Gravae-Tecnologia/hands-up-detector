#!/usr/bin/env bash
# Instala o detector de bracos levantados numa Raspberry de arena.
#
# RAPIDO PORQUE INSTALA O MINIMO
#   Em modo NUVEM (padrao) a Pi so captura, encoda JPEG e desenha o resultado
#   que o servidor devolve - nada disso precisa de onnxruntime nem dos modelos.
#   Sao ~10 s de instalacao contra varios minutos do modo local.
#
#   Passe --local para instalar tambem o onnxruntime (73 MB) e os modelos
#   (51 MB), para arenas sem link bom.
#
# IDEMPOTENTE
#   Pode rodar de novo em cima: nao duplica, nao reinstala o que ja esta, e
#   NAO sobrescreve /etc/gravae/hands-up.json. Uma atualizacao de parque nao
#   pode apagar quais quadras o operador ligou.
#
#   ./instalar.sh [--local] [--nuvem URL] [--webhook URL]
set -euo pipefail

DESTINO=/opt/gravae-hands-up
MODO=nuvem; NUVEM=""; WEBHOOK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --local) MODO=local; shift ;;
    --nuvem) NUVEM="$2"; shift 2 ;;
    --webhook) WEBHOOK="$2"; shift 2 ;;
    *) echo "opcao desconhecida: $1"; exit 1 ;;
  esac
done
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
t0=$(date +%s)

echo "==> dependencias do sistema"
NEED=""
python3 -c "import cv2" 2>/dev/null || NEED="$NEED python3-opencv"
command -v ffmpeg >/dev/null || NEED="$NEED ffmpeg"
if [ -n "$NEED" ]; then
  sudo apt-get update -qq && sudo apt-get install -y -q $NEED
else
  echo "    ja instaladas"
fi

echo "==> codigo em $DESTINO"
sudo mkdir -p "$DESTINO"
# copia todos os modulos: esquecer um so aparece no boot do servico
sudo cp "$DIR"/*.py "$DESTINO/"
[ "$MODO" = local ] && sudo cp -r "$DIR/modelos" "$DESTINO/"
sudo chown -R gravae:gravae "$DESTINO"

echo "==> venv"
if [ ! -x "$DESTINO/venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$DESTINO/venv"
fi
if [ "$MODO" = local ]; then
  "$DESTINO/venv/bin/pip" install -q --disable-pip-version-check onnxruntime
else
  echo "    modo nuvem: sem onnxruntime (a inferencia e remota)"
fi

echo "==> configuracao"
sudo mkdir -p /etc/gravae
if [ ! -f /etc/gravae/hands-up.json ]; then
  # tudo DESLIGADO: uma atualizacao de parque nao pode comecar a consumir
  # CPU e banda sozinha. O operador liga pelo OPS.
  printf '{\n  "ativo": false,\n  "nuvem": "%s",\n  "webhook": "%s",\n  "fps": 1.0,\n  "quadras": {},\n  "cameras": {}\n}\n' \
    "$NUVEM" "$WEBHOOK" | sudo tee /etc/gravae/hands-up.json >/dev/null
  echo "    criada (tudo desligado)"
else
  echo "    ja existe, preservada"
  [ -n "$NUVEM" ] && sudo python3 - "$NUVEM" "$WEBHOOK" <<'PY'
import json, sys
p = "/etc/gravae/hands-up.json"
d = json.load(open(p))
if sys.argv[1]: d["nuvem"] = sys.argv[1]
if len(sys.argv) > 2 and sys.argv[2]: d["webhook"] = sys.argv[2]
json.dump(d, open(p, "w"), indent=2)
print("    nuvem/webhook atualizados")
PY
fi
sudo chown gravae:gravae /etc/gravae/hands-up.json
# o servico roda como `gravae` e a config e salva por troca atomica, o que
# exige criar um .tmp no diretorio. Dono continua root (o agente escreve o
# device.json por la); so o grupo ganha escrita.
sudo chgrp gravae /etc/gravae && sudo chmod g+w /etc/gravae
sudo touch /var/log/gravae-hands-up.log
sudo chown gravae:gravae /var/log/gravae-hands-up.log

echo "==> servico"
sudo cp "$DIR/gravae-hands-up.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable -q gravae-hands-up
sudo systemctl restart gravae-hands-up
sleep 4
systemctl is-active --quiet gravae-hands-up && echo "    ativo" || {
  echo "    FALHOU:"; sudo journalctl -u gravae-hands-up -n 15 --no-pager; exit 1; }

echo
echo "pronto em $(( $(date +%s) - t0 ))s  |  modo $MODO"
echo "  status:   http://$(hostname -I | awk '{print $1}'):8090/api/config"
echo "  ligar:    curl -XPOST localhost:8090/api/config -d '{\"ativo\":true}'"
echo "  quadra:   curl -XPOST localhost:8090/api/config -d '{\"quadra\":\"campo01\",\"valor\":true}'"
