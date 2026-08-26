# hands-up-detector

Detecta o gesto de **braços levantados** nas câmeras de uma arena, a partir da
Raspberry que já roda o Shinobi, e avisa a plataforma no instante em que
acontece.

Duas formas de rodar, com a mesma interface:

| modo | onde infere | quando usar |
|---|---|---|
| **local** | na própria Pi | arena sem link bom; 1 câmera por vez |
| **nuvem** | numa VM com GPU | recomendado — libera a CPU da Pi e usa modelo maior |

---

## Como funciona

```
câmera RTSP ──► ffmpeg 1 fps ──► detector ──► pose por pessoa ──► critério do gesto
                  640x400         416x256      192x256              punho vs ombro
```

O critério é geométrico e está em `motor.gesto_bracos`:

```
punho.y    < ombro.y − 0,35 × largura_ombros     nos dois lados
cotovelo.y < ombro.y                             nos dois lados
```

Exigir o **cotovelo** acima do ombro é o que separa braço levantado de um aceno
com a mão na altura da cabeça.

A largura dos ombros é a régua de escala — não o torso, que não existe em
enquadramento de meio corpo.

---

## Instalação

**Na Raspberry da arena:**

```bash
git clone https://github.com/Gravae-Tecnologia/hands-up-detector
cd hands-up-detector/rasp && ./instalar.sh
```

**Na VM de inferência (opcional, modo nuvem):**

```bash
cd hands-up-detector/nuvem && ./instalar.sh
./venv/bin/python servidor.py --porta 8091 --det yolo11x --pose rtmpose-x
```

---

## Uso

```bash
# nuvem (recomendado): a Pi só captura, encoda e envia
venv/bin/python painel.py --porta 8090 \
    --nuvem http://IP-DA-VM:8091 \
    --webhook https://PLATAFORMA/alerta \
    --filtro campo01,campo02

# local: a Pi infere sozinha, uma câmera por vez
venv/bin/python painel.py --porta 8090 --filtro campo01
```

O painel fica em `http://<ip-da-pi>:8090`. Clique numa câmera para ligar a
análise.

---

## Por que as escolhas são estas

Cada número abaixo foi medido numa Raspberry Pi 4 de arena, partindo fria,
contra uma referência `yolo11x`+`rtmpose-x`.

### O detector define quantos você acha; a pose define o punho

São eixos independentes — trocar `rtmpose-s` por `-m` **não muda o F1**, porque
a cabeça de pose não cria nem descarta pessoa.

| detector | ms na Pi | F1 |
|---|---|---|
| RTMDet-Nano 320² | 155 | 75,8 |
| **YOLO11n 224×352** | **164** | **82,2** |
| **YOLO11n 256×416** | **193** | **90,1** |
| YOLO11n 416×640 | 377 | 91,0 |
| YOLOX-tiny 416² | 396 | 89,6 |
| YOLO11s 416×640 | 794 | 94,7 |
| YOLOX-m 640² | 3.301 | 97,6 |

| pose | ms **por pessoa** | PCK@0,2 punho |
|---|---|---|
| rtmpose-s | 112 | 52% |
| rtmpose-m | 226 | 66% |

**Custo total de qualquer combinação** (reproduz as medições em ~2%):

```
ms_total = ms_detector + ms_por_pessoa × pessoas_em_cena
```

Só o termo do detector é fixo. Um pipeline de 585 ms com 4 jogadores vira
809 ms com 6 — dimensione pelo pior caso.

### Entrada retangular, não quadrada

Os modelos são exportados em 256×416 e não 640×640: a fonte é 640×400, então
entrada quadrada gastaria 35% dos pixels em preenchimento cinza.

### 2 threads, não 4

Medido: 4 threads é **18% mais lento** que 2 (472 ms contra 399). São 4 núcleos,
mas o sistema usa parte deles e a sincronização do 3º e 4º custa mais do que
entrega.

E 4 workers de 1 thread rendem **5,78 inf/s** contra 2,12 de um worker serial de
4 threads — por isso a arquitetura é fila + pool, não um processo por câmera.

### Descarta quadro velho, não enfileira

Cada câmera guarda só o quadro mais recente. Para gesto, quadro velho não vale
nada, e fila FIFO faria a defasagem crescer sem limite. Os descartes são
**contados e exibidos** — descarte silencioso vira "funciona" na demo e "não
pegou o gesto" em produção.

### Sem torch, sem ultralytics, sem rtmlib na Pi

O decode do YOLO11 e o pré/pós do RTMPose (SimCC + warp afim com folga de 25%)
estão reimplementados em `rasp/motor.py`. São ~80 linhas contra ~2 GB de
dependência. O venv da Pi fica em **73 MB**.

### Aviso imediato, fora do Phoenix

O daemon Phoenix enfileira alertas em SQLite e só descarrega a cada 300 s — o
aviso do gesto chegaria até 5 minutos depois. Por isso o POST é direto, em
thread separada para não atrasar o próximo quadro.

O corpo segue o formato do webhook do Phoenix (`apiKey` + `alerts[]`) e usa o
tipo `gesture_detected`, que **não** dispara a maquinaria de notificação de
arena.

---

## Latência medida (modo nuvem, arena real)

Quatro câmeras a 1 fps, zero falhas:

| perna | tempo | quem controla |
|---|---|---|
| encode JPEG na Pi | 4–8 ms | qualidade do JPEG |
| rede (ida + volta) | 130–265 ms | link da arena |
| servidor (decode + inferência) | 150–630 ms | GPU e nº de pessoas |
| **total fim a fim** | **336–765 ms** | |

Cada quadro custa ~77 KB → **0,6 Mbit/s por câmera**, 2,4 Mbit/s com quatro.
Se o upload da arena for apertado, é esse o limite — não a GPU.

---

## Registro

Com `--registro`, cada quadro vira uma linha JSONL com os **instantes
absolutos** (captura, envio, resposta) além das durações por perna. Sem os
instantes não dá para reconstruir a ordem dos eventos entre câmeras nem cruzar
com o que aconteceu na quadra.

```json
{"cam":"campo01_camera01","evento":"quadro","pessoas":12,"gestos":0,
 "t_captura":1787754089.734,"t_envio":1787754089.735,"t_resposta":1787754090.499,
 "ms_encode":5.0,"ms_rede":130.9,"ms_servidor":627.9,"ms_infer":593.6,
 "ms_captura_ate_resposta":764.5,"kb":78.4,"temp":81.8}
```

---

## Limites conhecidos

**Térmico é o gargalo real, não CPU.** Uma Pi de arena com Shinobi rodando fica
em 72 °C parada e o throttling começa aos 80 °C. Com quatro capturas locais
chegou a 84,7 °C e o mesmo modelo passou de 193 para 1.948 ms — **10× mais
lento por causa do calor**. Em produção, cooler ativo não é opcional.

**Captura custa mais que se imagina.** O stream principal costuma ser
1280×720@30 mesmo quando o Shinobi reporta 640×480. Com `-vf fps=1` o ffmpeg
decodifica tudo e joga 29 de cada 30 fora: ~27% de um núcleo por câmera.

**O critério do gesto ainda não foi validado com dados suficientes.** O erro
médio de punho é 0,565 (rtmpose-s) e 0,412 (rtmpose-m) larguras de ombro,
contra uma margem de decisão de 0,35 — os dois erram mais que a própria
distância que decide. Falta filmagem com o gesto acontecendo para calibrar.

---

## Estrutura

| pasta | roda onde | o que tem |
|---|---|---|
| `rasp/` | Raspberry da arena | `motor.py` (ONNX puro), `painel.py` (web), modelos pequenos |
| `nuvem/` | VM com GPU | `servidor.py` (endpoint), instaladores |
| `ferramentas/` | máquina do time | acesso via Cloudflare Tunnel, encaminhamento de porta |

As ferramentas leem a credencial de `ARENA_PASS` no ambiente — nunca do código.
