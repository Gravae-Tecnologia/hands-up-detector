# Medições

Todos os números vieram de medição, não de estimativa. Onde há estimativa, está
marcado. Ambientes:

| ambiente | o que é |
|---|---|
| **Pi 4 (bancada)** | Raspberry Pi 4B, 4 GB, Debian 13, sem outra carga, partindo fria (<65 °C) |
| **Pi 4 (arena)** | a mesma coisa, mas com Shinobi rodando 8 monitores — **72 °C parada** |
| **i5-11400** | 6 núcleos @ 2,6 GHz, usado como referência de CPU x86 |
| **Cloud Run** | southamerica-east1, 4 vCPU, 2 GiB — **um vCPU é ~meio núcleo físico** |
| **RTX 3060** | usada só para gerar a referência de precisão |

---

## 1. Detectores — quantas pessoas você acha

Precisão contra referência `yolo11x`+`rtmpose-x`, 1.420 quadros de dois vídeos
de padel (o terceiro ficou fora: o polígono dele admite gente além do vidro).

| detector | params | entrada | px | **ms (Pi)** | recall | precisão | **F1** |
|---|---|---|---|---|---|---|---|
| RTMDet-Nano | 0,98 M | 320×320 | 102k | **155** | 77,5% | 74,2% | 75,8 |
| NanoDet-Plus-m | 1,17 M | 320×320 | 102k | 158 | 83,8% | 77,6% | 80,6 |
| **YOLO11n-224** | 2,62 M | 224×352 | 79k | **164** | 72,1% | 95,6% | **82,2** |
| **YOLO11n-256** | 2,62 M | 256×416 | 106k | **193** | 84,9% | 95,9% | **90,1** |
| YOLO11n-320 | 2,62 M | 320×512 | 164k | 253 | 86,0% | 94,7% | 90,2 |
| YOLO11n | 2,62 M | 416×640 | 266k | 377 | 94,7% | 87,7% | 91,0 |
| YOLOX-tiny | 5,06 M | 416×416 | 173k | 396 | 88,9% | 90,4% | 89,6 |
| **YOLO11s** | 9,46 M | 416×640 | 266k | **794** | 98,1% | 91,5% | **94,7** |
| RTMDet-Tiny | 4,88 M | 640×640 | 410k | 998 | 97,3% | 91,0% | 94,0 |
| YOLOX-m | 24,7 M | 640×640 | 410k | 3.301 | 97,6% | 97,6% | 97,6 |
| RTMDet-m-person | 24,7 M | 640×640 | 410k | 3.508 | 98,7% | 97,2% | 97,9 |
| *yolo11n-pose* | *2,87 M* | *one-stage* | — | *não medido* | *36,0%* | *98,9%* | *52,8* |
| *yolo11s-pose* | *9,92 M* | *one-stage* | — | *não medido* | *43,8%* | *99,6%* | *60,8* |

**O custo segue pixels, não parâmetros.** O YOLO11n processa 54% mais pixels
que o YOLOX-tiny em 5% menos tempo — é a arquitetura mais eficiente por pixel
da tabela (142 ms/100k px contra 229 do YOLOX-tiny e 805 do YOLOX-m).

**O joelho da curva está em 256×416.** Cortar de 266k para 106k px custa
**0,9 ponto de F1** e economiza 184 ms. Cortar mais (224×352) perde 7,9 pontos
por só 29 ms.

**Três modelos saem dominados** (piores nos dois eixos):
`YOLOX-tiny` < `YOLO11n-256` · `RTMDet-Tiny` < `YOLO11s` · `RTMDet-Nano` <
`YOLO11n-224`.

**One-stage não serve.** `yolo11n-pose` tem precisão 98,9% e recall 36% — acha
pouca gente e nunca erra. E o punho dele erra **1,015** larguras de ombro,
quase 3× a margem do gesto.

---

## 2. Poses — precisão do punho

Detector fixo, para o conjunto de pessoas casadas ser o mesmo.

| pose | params | **ms/pessoa (Pi)** | ms/pessoa (i5) | ombro | cotovelo | **punho** | **PCK@0,2** |
|---|---|---|---|---|---|---|---|
| rtmpose-s | 5,5 M | **112** | 8,9 | 0,238 | 0,394 | **0,565** | 52,0% |
| rtmpose-m | 13,3 M | **226** | 17,6 | 0,175 | 0,297 | **0,412** | 66,4% |

O erro de punho quase não muda com o detector (0,533–0,622 com `-s`;
0,366–0,431 com `-m`), o que confirma a independência dos eixos.

> **A margem do gesto é 0,35 e os dois erram mais que isso.** O `-s` em 1,6× a
> margem, o `-m` em 1,18×. Pela média, nenhuma das cabeças é precisa o bastante
> para o critério como está escrito. Ver "em aberto" no `CLAUDE.md`.

---

## 3. Threads e paralelismo (Pi 4, `YOLO11n-256+rtmpose-s`, 2 pessoas)

| threads | ms | ganho vs 1 | eficiência |
|---|---|---|---|
| 1 | 692 | — | — |
| **2** | **399** | **1,73×** | **86%** |
| 4 | 472 | 1,47× | 37% |

**4 threads é 18% mais lento que 2.** São 4 núcleos, mas o sistema usa parte
deles e a sincronização do 3º e 4º custa mais do que entrega.

E por isso a arquitetura é **fila + pool**, nunca um worker serial:

| arquitetura | throughput agregado |
|---|---|
| **4 workers × 1 thread** | **5,78 inf/s** |
| 2 workers × 2 threads | 5,01 inf/s |
| 1 worker × 4 threads | **2,12 inf/s** |

---

## 4. Térmico (Pi 4 de arena)

| situação | temperatura | throttle |
|---|---|---|
| parada, só Shinobi | **72 °C** | limpo |
| 4 capturas locais + inferência | **84,7 °C** | **`0xe0008` — limitando agora** |
| 4 câmeras em modo nuvem | **76–79 °C** | limpo |
| 1 câmera local | 82–84 °C | limite atingido |

**Efeito no tempo:** o `RTMDet-Nano` mediu 1.086 ms num vídeo e 3.830 ms em
outro na mesma bateria — variação de 3,5× que era só calor acumulado. E o
detector que faz 193 ms frio chegou a **1.948 ms** quente.

> Toda medição de tempo na Pi tem que partir de **<65 °C** e com o BMO parado.
> Sem isso você mede calor, não modelo.

---

## 5. Latência fim a fim (arena real → Cloud Run São Paulo)

4 câmeras a 1 fps, `yolo11s+rtmpose-m`, **zero falhas**:

| perna | tempo | quem controla |
|---|---|---|
| encode JPEG na Pi | **4–8 ms** | qualidade do JPEG |
| rede (ida + volta) | **207–275 ms** | link da arena |
| servidor (decode + inferência) | **64–264 ms** | vCPU e nº de pessoas |
| **total** | **285–506 ms** | |

**Sem cold start perceptível**: 991 ms na primeira requisição contra 786 das
seguintes. O ONNX puro carrega em **0,24 s** — foi por isso que torch e
ultralytics ficaram fora da imagem.

Comparação com o endpoint em GPU (RTX, via túnel), `yolo11x+rtmpose-x`:

| | GPU RTX (túnel) | **Cloud Run CPU (SP)** |
|---|---|---|
| total | 336–765 ms | **285–506 ms** |
| servidor | 150–630 ms | **64–264 ms** |
| F1 | 97,9 | 94,7 |

**A CPU ficou mais rápida no total** — o `rtmpose-x` é muito mais pesado que o
`-m`, e a GPU não compensa. Não vale GPU para ganhar 3 pontos de F1.

### Sobre os 207–275 ms de rede

Esperava 20–50 ms de Salvador a São Paulo. Duas explicações possíveis e **não
separadas ainda**: o TLS do Cloud Run (antes era HTTP puro) ou a **subida dos
70 KB** — a 2 Mbit/s de upload, 70 KB levam 280 ms, que bate quase exato. Se
for o segundo, a alavanca é reduzir o quadro, não trocar de região.

---

## 6. Banda

| | tamanho | por câmera a 1 fps |
|---|---|---|
| quadro 640×400 JPEG q75 | **70–79 KB** | **0,6 Mbit/s** |
| recortes de 2 pessoas | **11,8 KB** | 0,1 Mbit/s |

**Recorte é 6× menor que o quadro.** Mandar só as pessoas em vez do quadro
inteiro é a maior alavanca de banda que existe — mas move o detector para a
Pi, que custa 164 ms e calor. Hoje não compensa; num cenário de muitas arenas,
compensa.

### Projeção para escala (estimativa)

50 arenas × 8 câmeras = 400 câmeras a 1 fps:

| | ingênuo | com região + portão de movimento |
|---|---|---|
| requisições | 400/s | 160/s |
| banda | 226 Mbit/s | 90 Mbit/s |
| vCPU | ~27 | ~11 |

Alavancas em ordem de retorno: **região** (grátis, só uma flag),
**portão de movimento na Pi** (~3 ms, corta quadra vazia), **recortes**
(6× banda, mas move o detector para a Pi).

---

## 7. Quantização INT8 — não funciona na Pi 4

Os modelos quantizaram bem (3,4× menores) e no i5 deram ganho modesto
(`rtmpose-s` de 7,8 → 5,1 ms). Mas na Pi 4:

```
Features : fp asimd evtstrm crc32 cpuid
```

**Falta `asimddp`.** O Cortex-A72 é ARMv8.0-A e as instruções de produto
escalar int8 (SDOT/UDOT) só chegaram no ARMv8.2-A. Sem elas o ganho teórico
não aparece — pode até piorar, porque quantizar e desquantizar a cada camada
tem custo real.

**Só vale em Pi 5.** Os arquivos `*_int8.onnx` ficam no repositório para quando
isso acontecer.

Detalhe de export: quantização por canal exige **opset ≥ 13** (o atributo
`axis` do `DequantizeLinear`). O YOLO saía em 12 e o RTMPose do mmdeploy em 11.

---

## 8. Classificador binário — tentado e não funcionou (ainda)

A ideia: trocar 112 ms de pose (17 keypoints) por ~5 ms de classificador
binário, já que o produto precisa de **1 bit**.

| | AUC | melhor F1 |
|---|---|---|
| validação COCO | **0,817** | 45,2 |
| **teste nos vídeos da quadra** | **0,636** | 26,1 |

O AUC de 0,817 prova que **o conceito é aprendível**. Os 0,636 no domínio real
são quase moeda ao ar. A causa não é arquitetura: o modelo nunca viu um
jogador de padel com braços levantados. COCO tem gente grande, nítida e
centrada; os recortes da quadra são regiões de ~40×80 px esticadas, com
desfoque de movimento, vistas de cima.

Adicionar 2.208 negativos do domínio corrigiu os 1.123 falsos positivos, mas
sem **positivos** do domínio não há o que aprender. O conjunto de teste tem 17
positivos — dois acertos a mais mudam o F1 em 10 pontos.

**Bloqueado por dados, não por engenharia.**
