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
  # `update` com erro NAO quer dizer listas inuteis. O Debian 11 (bullseye)
  # saiu do LTS em 31/08/2026 e o Release do bullseye-security expirou: o
  # `apt-get update` sai com erro, mas o repositorio principal atualiza e o
  # python3-opencv esta nele. O `update && install` de antes pulava o install
  # em silencio - `set -e` nao pega falha no meio de um `&&` - e o servico
  # subia e morria em loop sem cv2. Foi a primeira instalacao no Fit Club,
  # 10/09/2026.
  sudo apt-get update -qq || echo "    aviso: apt-get update com erro (repositorio expirado?); seguindo com as listas que ha"
  if ! sudo apt-get install -y -q $NEED; then
    # Mesma causa, segundo sintoma: as listas velhas do *-security ainda
    # apontam para versoes que sairam do servidor (404 em libpq5 e
    # libgdcm3.0, dependencias do opencv, no Fit Club). Mirando o repositorio
    # principal da mesma versao, o apt escolhe o que ainda existe - simulado
    # la antes: 62 pacotes novos, nenhum removido, nenhum rebaixado.
    CODINOME=$(. /etc/os-release && echo "${VERSION_CODENAME:-}")
    echo "    install falhou; tentando so o repositorio principal (-t $CODINOME)"
    [ -n "$CODINOME" ] && sudo apt-get install -y -q -t "$CODINOME" $NEED
  fi
else
  echo "    ja instaladas"
fi
# confere de verdade: dependencia faltando tem de aparecer AQUI, com motivo,
# e nao como um servico reiniciando a cada 10 s
python3 -c "import cv2" 2>/dev/null || { echo "ERRO: python3-opencv nao ficou instalado" >&2; exit 1; }
command -v ffmpeg >/dev/null || { echo "ERRO: ffmpeg nao ficou instalado" >&2; exit 1; }

echo "==> codigo em $DESTINO"
sudo mkdir -p "$DESTINO"
# copia todos os modulos: esquecer um so aparece no boot do servico
sudo cp "$DIR"/*.py "$DESTINO/"
[ "$MODO" = local ] && sudo cp -r "$DIR/modelos" "$DESTINO/"
# Qual commit esta rodando. Sem isto, conferir se uma atualizacao chegou a
# uma Pi era comparar hash de arquivo na mao - e a ponte do agente ja
# reinstalou codigo velho respondendo ok. O servico devolve em /api/config.
( git -C "$DIR" rev-parse --short HEAD 2>/dev/null || echo desconhecida ) \
  | sudo tee "$DESTINO/VERSAO" >/dev/null
echo "    versao $(cat "$DESTINO/VERSAO")"
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
# Only the detector state directory is writable by its service user.
# Preserve existing LEGACY switches by migrating once, without modifying the source.
sudo python3 "$DIR/install_state.py" "$USUARIO" "$NUVEM" "$WEBHOOK"
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
