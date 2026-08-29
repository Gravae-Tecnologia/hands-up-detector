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

# O usuario do servico NAO e fixo. As Pis da Gravae tem `gravae`; as da
# Replayme tem `replayme` e NAO tem `gravae` — medido em 29/08/2026: 188 dos
# 893 dispositivos da frota. Com `set -e`, um `chown` num usuario inexistente
# aborta o instalador inteiro, entao o parque inteiro da Replayme falharia na
# primeira linha que toca dono de arquivo.
USUARIO=""
for u in gravae replayme "${SUDO_USER:-}"; do
  if [ -n "$u" ] && id "$u" >/dev/null 2>&1; then USUARIO="$u"; break; fi
done
if [ -z "$USUARIO" ]; then
  echo "ERRO: nenhum usuario de servico encontrado (gravae, replayme ou SUDO_USER)" >&2
  exit 1
fi
echo "==> usuario do servico: $USUARIO"

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
sudo cp "$DIR"/servico.py "$DIR"/motor.py "$DIR"/config.py "$DESTINO/"
[ "$MODO" = local ] && sudo cp -r "$DIR/modelos" "$DESTINO/"
sudo chown -R "$USUARIO:$USUARIO" "$DESTINO"

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
sudo chown "$USUARIO:$USUARIO" /etc/gravae/hands-up.json
# o servico roda como $USUARIO e a config e salva por troca atomica, o que
# exige criar um .tmp no diretorio. Dono continua root (o agente escreve o
# device.json por la); so o grupo ganha escrita.
sudo chgrp "$USUARIO" /etc/gravae && sudo chmod g+w /etc/gravae
sudo touch /var/log/gravae-hands-up.log
sudo chown "$USUARIO:$USUARIO" /var/log/gravae-hands-up.log

echo "==> servico"
# `User=` sai do arquivo do repo e vira o usuario detectado: unit com dono
# inexistente sobe e morre em loop, sem erro no instalador.
sudo sed "s/^User=.*/User=$USUARIO/" "$DIR/gravae-hands-up.service" \
  | sudo tee /etc/systemd/system/gravae-hands-up.service >/dev/null
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
