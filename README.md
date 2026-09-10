# hands-up-detector

Detecta o gesto de **braços levantados** nas câmeras de uma arena, a partir da
Raspberry que já roda o Shinobi, e avisa a plataforma no instante em que
acontece.

Duas formas de rodar, com a mesma interface:

| modo | onde infere | quando usar |
|---|---|---|
| **local** | na própria Pi | arena sem link bom; 1 câmera por vez |
| **nuvem** | Cloud Run (CPU) | recomendado — libera a CPU da Pi e usa modelo maior |

📄 **[CLAUDE.md](CLAUDE.md)** — contexto para quem for mexer: invariantes,
armadilhas já pagas e o que está em aberto.
📊 **[MEDICOES.md](MEDICOES.md)** — todos os números, com o ambiente de cada um.
🎥 **[Playbook de vídeo ao vivo + IA](#playbook-vídeo-ao-vivo--ia-numa-raspberry)**
— a parte reaproveitável: vale para qualquer projeto de análise ao vivo em Pi,
não só para este gesto.

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

**Na Raspberry da arena — 9 segundos, modo nuvem:**

```bash
git clone https://github.com/Gravae-Tecnologia/hands-up-detector
cd hands-up-detector/rasp
./instalar.sh --nuvem https://hands-up-detector-....run.app
```

É rápido porque instala o mínimo: em modo nuvem a Pi só captura, encoda JPEG e
desenha o resultado que o servidor devolve — **nada disso precisa de
onnxruntime nem dos modelos**. O `import onnxruntime` é preguiçoso justamente
para isso. Use `--local` só em arena sem link bom.

O script é **idempotente** e **não sobrescreve** `/etc/gravae/hands-up.json` —
uma atualização de parque não pode apagar quais quadras o operador ligou.

### Switches (integração com o OPS)

Tudo nasce **desligado**. Uma atualização que chega em todas as Raspberries não
pode começar a consumir CPU e banda sozinha.

```bash
curl localhost:8090/api/config                                    # estado + switches
curl -XPOST localhost:8090/api/config -d '{"ativo":true}'         # liga o serviço
curl -XPOST localhost:8090/api/config -d '{"quadra":"campo01","valor":true}'
curl -XPOST localhost:8090/api/config -d '{"camera":"campo01_camera02","valor":false}'
```

Dois níveis, e não são redundantes: **quadra** desliga tudo dela de uma vez (o
caso comum — reforma, fora de horário); **câmera** é o refinamento para um
ângulo ruim ou defeito. Uma câmera só processa se a quadra dela **e** ela
mesma estiverem ligadas.

O `GET /api/config` devolve `quadras_detalhe`, já no formato que o OPS precisa
para desenhar os switches.

### Só com gente em quadra (gatilho pela IA da câmera)

Câmera ligada captura o dia inteiro, com a quadra vazia ou não. As Intelbras da
linha **-IA** (ex. VIP-3430-D-IA) detectam humano sozinhas; com o gatilho `ia`,
a Pi só captura — e só paga a nuvem — enquanto há gente:

```bash
curl -XPOST localhost:8090/api/config -d '{"gatilho":"ia"}'        # ou "manual" (padrão)
curl -XPOST localhost:8090/api/config -d '{"espera_ia_s":600}'     # 60..3600, padrão 10 min
curl -XPOST localhost:8090/api/config -d '{"sondar_ia":true}'      # pergunta às câmeras de novo
```

- **A Pi descobre sozinha quais câmeras têm IA.** No arranque, sem nada ligado,
  ela pergunta a cada câmera (`getDeviceType`, `getExposureEvents`,
  `SmartMotionDetect`); o resultado vai em `quadras_detalhe[].cameras[].ia` e o
  total em `ia_resumo`. Refaz a cada 6 h (5 min se a câmera não respondeu).
- **A câmera avisa, a Pi não fica perguntando.** Uma conexão HTTP longa
  (`eventManager.cgi?action=attach`) por câmera; o evento `SmartMotionHuman`
  chega na hora, com uma batida a cada ~5 s.
- **Pausa 10 min depois da última pessoa**, vista pela câmera ou pelo nosso
  detector (a câmera só vê *movimento* humano; quem está parado ela não avisa).
  Do detector só vale gente **consistente** — 3 quadros em 10 s: numa quadra
  vazia do Fit Club ele deu 6 quadros isolados com "1 pessoa" em 3 min.
- **Na dúvida, fica ativa:** câmera sem IA, com a detecção de humano desligada
  nela, que não respondeu, ou com a conexão caída há mais de 30 s.

Cada câmera traz `modo` — `desligada`, `manual`, `sem_ia`, `gente`,
`sem_sinal`, `pausada` (legenda em `modos`) — e `gatilho_ia_s`, o tempo ativa ×
pausada desde que o serviço subiu.

### Serviço

Roda sob systemd (`gravae-hands-up.service`), com `Restart=always`,
`Nice=5` e `CPUWeight=50` — numa disputa, a captura do Shinobi, que é o
produto, ganha a CPU.

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

Cloud Run CPU em `southamerica-east1`, três câmeras a 1 fps. Base de
**8.697 quadros**:

| perna | tempo | fatia | quem controla |
|---|---|---|---|
| encode JPEG na Pi | 4–8 ms | 1% | qualidade do JPEG |
| rede (ida + volta) | **228 ms** | **31%** | uplink da arena |
| servidor (decode + inferência) | 189 + 65 × pessoas | **58%** | vCPU e nº de pessoas |
| **total fim a fim** | **média 590 ms, p90 1.211 ms** | | |

O custo do servidor é linear no número de pessoas, e a fórmula reproduz as
medições dentro de ~2%:

```
ms_servidor = 189 + 65 x pessoas
```

Cada quadro custa ~28 KB → **0,67 Mbit/s com três câmeras**. Medido em teste
real com gente em quadra: latência de detecção média de **590 ms**, máxima de
**796 ms**, 18 alertas e nenhuma falha de webhook.

**A CPU bateu a GPU.** Comparando com o endpoint em GPU (RTX, via túnel) usando
`yolo11x+rtmpose-x`: total 336–765 ms contra 285–506 ms do Cloud Run CPU, com
F1 97,9 contra 94,7. Não vale GPU para ganhar 3 pontos de F1.

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

**O critério do gesto ainda não foi validado com dados suficientes.** O erro
médio de punho é 0,565 (rtmpose-s) e 0,412 (rtmpose-m) larguras de ombro,
contra uma margem de decisão de 0,35 — os dois erram mais que a própria
distância que decide. Em compensação, em campo as margens observadas ficaram
entre **0,29 e 24,21**, com só 2 ocorrências em 400 na zona duvidosa: braço
levantado de verdade não passa perto da fronteira.

**O conjunto de revisão tem um ponto cego.** O `revisao.py` só guarda quadros
com margem acima de 0,15. Uma falha de verdade — pessoa com a mão levantada em
que o modelo errou o punho, ou em que o detector não achou ninguém — dá margem
perto de zero e **é descartada sem deixar rastro**. O conjunto responde "quem
levantou e não segurou", não "quem levantou e o modelo não viu". Para a segunda
pergunta é preciso gravar quadros crus numa janela de teste.

**Um vídeo por câmera a cada 15 s**, não um por pessoa. `TRIGGER_COOLDOWN`
suprime gravações próximas, porque seriam do mesmo trecho. O alerta e o webhook
saem sempre; só a gravação é suprimida, e o motivo vai no payload. Confirmado em
campo: 18 alertas produziram 12 vídeos e 6 supressões.

**O cooldown é por câmera.** Quadras diferentes não se atrapalham.

---

## Playbook: vídeo ao vivo + IA numa Raspberry

Esta seção não é sobre braços levantados. É o que aprendemos, medindo, sobre
rodar **análise de vídeo ao vivo numa Raspberry Pi 4 de arena** — e vale para
qualquer projeto do tipo. Tudo aqui foi medido em produção, na CTF Marcelinho,
com o Shinobi rodando junto.

### 1. A captura custa mais que a inferência, e quase tudo é desperdício

O erro que custou mais tempo: assumir que o gargalo seria o modelo. Não era.
Com a inferência na nuvem, a Pi só captura — e mesmo assim:

| processo | CPU |
|---|---|
| **nossos 3 ffmpeg de captura** | **56–64% de um núcleo cada → ~1,9 núcleo** |
| 3 ffmpeg do Shinobi (HLS, `-c:v copy`) | 3,7% cada → 0,11 núcleo |
| resto do sistema | ~0,25 núcleo |

O systemd assinou embaixo: `Consumed 1h 42min CPU time` para um serviço no ar
há 76 minutos — **1,35 núcleo de média**.

**A causa é o `-vf fps=1`.** Filtro em ffmpeg opera sobre quadro **já
decodificado**. A ordem é: recebe o pacote, decodifica, e só então o filtro
decide se joga fora. Ele decodifica os 30 quadros por segundo para usar 1.

A conta fecha: 63% de um núcleo = 630 ms de CPU por segundo, dividido por 30
quadros = **21 ms por quadro**, que é exatamente o custo de decodificar um
H.264 1080p num Cortex-A72.

### 2. Como decodificar só o que você vai usar

Não dá para decodificar "um quadro qualquer": num GOP, só o primeiro (I-frame) é
autossuficiente; os demais guardam a diferença em relação ao anterior. Mas dá
para decodificar **só os keyframes** — e se a câmera manda ~1 keyframe por
segundo, é exatamente o que um pipeline de 1 fps precisa.

Medido na mesma câmera (1920×1080, GOP 27, ou seja 1 keyframe a cada 0,90 s):

| variante | CPU | quadros/s | únicos | nitidez |
|---|---|---|---|---|
| `-vf fps=1` (a armadilha) | **69,9%** | 1,15 | 26/26 | 262 |
| **`-skip_frame nokey -vf fps=1`** | **14,2%** | 1,13 | 25/25 | **200** |
| substream (704×480) `-vf fps=1` | 14,7% | 1,16 | 26/26 | 80 |

**`-skip_frame nokey` junto com `-vf fps=1` é 4,9× mais barato e mantém 76% da
nitidez.** O substream custa o mesmo e entrega 31% — a rede e o fundo saem
visivelmente borrados.

Três armadilhas dentro desta armadilha:

**`-discard nokey` não é a mesma coisa.** Ela age no demuxer; nessas câmeras o
ffmpeg simplesmente ignora e entrega os 30 fps. A que funciona é
`-skip_frame nokey`, que age no decodificador. Testar a errada e concluir que o
caminho estava fechado custou meio dia.

**`-skip_frame nokey` sozinho engana.** Sem `fps=1` ele mostra 34 quadros/s e
23% de CPU, e parece que não pulou nada. Pulou: o decodificador devolve o
**mesmo keyframe repetido** — 8 leituras seguidas deram hashes idênticos. Os 23%
não eram decodificação, eram copiar e redimensionar 26 MB/s de repetição. Com
`fps=1` na frente, o filtro descarta as repetições antes do `scale` e sobra
14,2%.

**Meça o GOP antes de confiar nisso.** Se a câmera mandar 1 keyframe a cada 2 s,
o pipeline de 1 fps perde metade dos quadros. Sem decodificar nada:

```bash
ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
  -show_entries packet=flags -of csv=p=0 -read_intervals "%+8" "rtsp://..."
```

Conte as linhas que começam com `K` — são os keyframes.

### 3. O decodificador de hardware não escala

A Pi 4 tem H.264 em silício (`h264_v4l2m2m`), e com **uma** câmera é ótimo:
21,2% de núcleo a 1080p, sem perda de qualidade. Com **três simultâneas** ele
quebra:

```
.191   13.0% nucleo  0.52 quadro/s
.192    0.1% nucleo  0.00 quadro/s   ERRO: V4L2 capture poll unexpected
.193   11.4% nucleo  0.52 quadro/s
```

Uma câmera morreu e as outras entregaram **metade** da taxa. O VideoCore dá
conta de ~1080p60 no total; três streams de 1080p30 pedem o dobro. Serve para
uma câmera, não para um NVR.

### 4. O gargalo é térmico, não CPU

Este é o ponto que mais inverte intuição. A Pi de arena não fica sem CPU — ela
esquenta e o **próprio SoC** reduz o clock.

| capturas rodando | temperatura | throttling |
|---|---|---|
| 0 | **57,9 °C** | `0xe0000` |
| 2 | 79,4 °C | `0xe0000` |
| 3 | 82,8 °C | **`0xe0008`** (limite brando ativo) |

Nossas capturas respondem por **~25 °C** sozinhas. A 80 °C entra o limite
brando (clock de 1,8 → 1,726 GHz); a 85 °C entra o throttle duro. Com quatro
capturas locais a placa chegou a 84,7 °C e **o mesmo modelo passou de 193 para
1.948 ms — 10× mais lento por causa do calor**.

Como ler o `vcgencmd get_throttled`:

| bit | significa |
|---|---|
| `0x1` / `0x10000` | sub-tensão agora / já houve |
| `0x4` / `0x40000` | throttle duro agora / já houve |
| `0x8` / `0x80000` | **limite térmico brando agora** / já houve |

`0xe0008` = limite brando **ativo agora**, e já capou clock e throttlou antes.

**Meça no regime permanente, não nos primeiros minutos.** Uma leitura feita com
168 quadros capturados (3 minutos) deu 77,9 °C e me fez reportar "sem
throttling". O regime real, com 48 mil quadros, era 82–83 °C. Massa térmica leva
dezenas de minutos para saturar.

### 5. Nuvem ou local: a conta que decide

A intuição diz "manda o vídeo para a nuvem decodificar e alivia a Pi". A conta
diz o contrário — **vídeo comprimido é maior que a imagem que você já manda**:

| o que a Pi manda | banda (3 câmeras) | CPU na Pi |
|---|---|---|
| **JPEG 640×400 a 1 fps (o que fazemos)** | **0,67 Mbit/s** | decode + encode |
| só os keyframes do substream | ~1,2 Mbit/s (estimado) | ~0 |
| substream H.264 contínuo | 3,5 Mbit/s | ~0 |
| stream principal contínuo | 14,3 Mbit/s | ~0 |

O uplink da arena entrega ~1 Mbit/s efetivo (é o que 28 KB em 270 ms indicam).
Nenhuma das opções de vídeo cabe. **Mande imagem decimada, não vídeo.**

E não adianta pegar do NVR: o Shinobi roda com `-c:v copy`, ou seja **não
decodifica nada** — por isso custa 3,7% de núcleo. Os segmentos em `/dev/shm`
continuam H.264. Não existe quadro decodificado guardado em lugar nenhum; quem
quiser pixel paga o decode.

A opção que elimina o decode de vez é **pedir o JPEG pronto à câmera**
(`/cgi-bin/snapshot.cgi` nas Dahua): a câmera já tem o codificador. Não testamos
o limite de taxa dela.

### 6. Concorrência: não espere a resposta parado

Com uma requisição em voo por câmera, a Pi manda um quadro e fica parada. Com
p90 de 1.211 ms contra captura a cada 1.000 ms, **~10% dos quadros eram
capturados durante a espera e descartados** — concentrados em quadra cheia, que
é quando o sistema importa.

Mandar vários ao mesmo tempo resolve o desperdício e cria o problema que
`cronologia.py` existe para resolver: **as respostas voltam fora de ordem**, e
um rastreio é máquina de estados temporal. Alimentado fora de ordem, o timer do
gesto anda para trás.

O desenho: captura → N envios concorrentes → consumo **em ordem**.

- O `seq` é atribuído **só a quadro que será enviado**, então decimação não cria
  buraco na sequência; buraco só existe quando uma requisição falha.
- A janela **conhece o conjunto em voo**, então resolve falha sem esperar timer:
  se o próximo esperado não está em voo nem pronto, não vem mais.
- Resposta que chega **depois do seu lugar é descartada**, nunca reinjetada.
  Melhor perder um quadro que corromper o estado temporal.
- Profundidade limitada pela concorrência: com N em voo, no máximo N−1 esperando.

O controlador sobe a concorrência **antes** de reduzir o fps (perder detecção é
pior que atrasar), e na volta devolve o fps antes de encolher a concorrência.
Avalia a cada 10 s e move um passo: ritmo regular o rastreio absorve, buraco
irregular quebra.

Resultado medido: aproveitamento de quadros de **~90% para 98–99%**, sem
throttling adicional — concorrência sobrepõe espera de rede, não cria trabalho
de CPU.

**Uma conexão keep-alive por thread.** Um lock em volta do request/response
serializa tudo e a concorrência deixa de existir. E sem keep-alive cada quadro
paga handshake TCP, que na arena passa de 200 ms — quase o tempo de rede inteiro.

Um detalhe que quase passou: o portão de ritmo estava em exatamente `1/fps`, e o
ffmpeg entrega a ~0,98 s. O quadro chegava "cedo" e era recusado — **38% dos
quadros**. O piso virou `0,85/fps`. Vale para qualquer portão de tempo: o
relógio da fonte não é o seu.

### 7. O relógio é a captura, nunca a resposta

A invariante mais importante de qualquer pipeline com inferência remota. A
latência varia de 400 a 3.420 ms; se o rastreio for alimentado com o instante em
que a **resposta** chegou, a duração medida do gesto carrega jitter de rede em
vez do tempo real da pessoa.

```python
rast.passo(kpts, margens, LIMIAR, caixas=caixas, agora=q.t_captura)
```

### 8. Rastreio a 1 fps

Confirmar "gesto sustentado por N segundos" exige saber que a pessoa deste
quadro é a mesma do anterior. O servidor devolve keypoints sem identidade — ele
vê um quadro por vez. O rastreio mora do lado da Pi, que é quem tem a sequência.

O que funcionou:

- **Associação em duas etapas** (ideia do ByteTrack): primeiro IoU das caixas,
  forte para quem está parado — o caso de quem levanta a mão; o que sobra tenta
  distância de centro.
- **Sem Kalman.** A 1 fps a previsão linear atrapalha: em um segundo um jogador
  muda de direção várias vezes e a previsão erra mais que a última posição.
- **Distância normalizada pela largura de ombros**, não em pixels. Um jogador
  perto anda 80 px entre quadros; um no fundo anda 8. Era justamente a pessoa do
  fundo que se perdia.
- **Tolerância adaptativa: 2,5 × o intervalo mediano observado.** Um bug real:
  tolerância fixa em 1,2 s com intervalo real de 2,5 s fazia toda sequência
  zerar antes de completar — uma pessoa segurou 4 s e apareceu como três
  detecções instantâneas.
- **A trilha precisa expirar mesmo com o quadro vazio.** Quando ninguém aparece
  e o `passo` não é chamado, as trilhas não expiram e a próxima pessoa herda o
  estado `confirmado` da anterior — e nunca mais dispara.

Confirmação dispara na **borda**, uma vez por sequência:

```python
if t.confirmado_em is None and t.segurando_s >= self.dur_s:
    t.confirmado_em = agora
    confirmados.append(t)
```

Em campo: uma trilha rendeu 17 imagens de evidência e **um** alerta.

### 9. Painel ao vivo: não use MJPEG

Custou três tentativas. Para mostrar 1 quadro por segundo, `<img>` apontando
para `multipart/x-mixed-replace` é a escolha errada, por dois motivos:

**O navegador só pinta uma parte quando chega o delimitador da seguinte.** Fonte
parada manda uma parte e cala — a imagem fica presa no buffer e o tile aparece
**preto**, com o JPEG certo do lado do servidor.

**`<img>` com MJPEG nunca reconecta.** É uma conexão permanente; se ela cai (e
por um túnel SSH ela cai), a imagem congela para sempre e não há retry.

A solução é banal e robusta: uma rota que devolve **um JPEG e fecha**, e o
navegador busca uma por segundo, carregando fora da tela e trocando o `src` só
quando o quadro está pronto (evita piscar). Se um GET falhar, o próximo tique
conserta.

```javascript
function pinta(mid){
  const im=new Image();
  im.onload=()=>{const el=document.getElementById("i_"+mid); if(el) el.src=im.src;};
  im.src="/foto/"+mid+".jpg?"+Date.now();
}
setInterval(()=>cams.forEach(c=>pinta(c.mid)),1000);
```

Ainda: uma miniatura que só é gerada **uma vez** e falha fica `None` para
sempre. Toda captura de imagem de apoio precisa de retentativa — abrir uma
sessão RTSP a mais numa câmera que já serve o NVR falha com frequência.

### 10. Nada nasce ligado, e tudo é contado

Uma atualização que chega em centenas de Raspberries **não pode** começar a
consumir CPU e banda sozinha. O operador liga pelo painel de operação; o
instalador é idempotente e não sobrescreve a configuração local.

E todo descarte é **contado e exibido**, separado por motivo (decimado por ritmo
× sem vaga de concorrência). Descarte silencioso vira "funciona" na demo e "não
pegou o gesto" em produção. Foi assim que a tolerância errada do rastreio passou
horas despercebida.

### 11. Pi 4 ou Pi 5

A Pi 4 de arena é ARMv8.0-A **sem `asimddp`** — sem dot product, quantização
INT8 não acelera nada. Foi por isso que a quantização não pagou quando testamos.
A Pi 5 (Cortex-A76, 2,4 GHz) tem, e aí o INT8 volta a valer.

Projeção a partir das medições da Pi 4 (`ms_total = detector + ms_por_pessoa ×
pessoas`), com 4 pessoas em quadra:

| | total | observação |
|---|---|---|
| Pi 4 hoje | **641 ms** | medido |
| Pi 5 FP32 (~2,5×) | ~257 ms | **projeção**, não medido |
| Pi 5 + INT8 | ~145 ms | **projeção**, não medido |
| nuvem hoje | **590 ms** | medido, ida e volta |

Uma Pi 5 provavelmente bate a nuvem — não por rodar mais rápido que o Cloud Run,
mas porque elimina os ~270 ms de rede. E o Cloud Run que usamos é 4 vCPU (~2
núcleos físicos), o que a Pi 5 tem folga para igualar.

**O bloqueio não é o processador, é o gabinete.** A Pi 5 dissipa mais que a Pi 4
e o fabricante especifica cooler ativo; as Pis de arena não têm **nenhum**
dispositivo de refrigeração. Sem resolver isso, inferência local corre para a
mesma parede térmica.

Duas considerações que não são técnicas: com a nuvem você melhora o modelo para
todas as arenas de uma vez; com processamento local, cada melhoria vira
atualização de frota. Em compensação, local funciona com o link da arena caído —
hoje, se a internet cai, não há detecção nenhuma.

### Checklist para o próximo projeto de live + IA numa Pi

1. Meça a **resolução real** do stream, não a que o NVR reporta.
2. Meça o **GOP** antes de decidir a estratégia de captura.
3. Use `-skip_frame nokey -vf fps=N`; nunca `-vf fps=N` sozinho.
4. Meça a temperatura **no regime permanente**, e leia o `get_throttled`.
5. Mande **imagem decimada** para a nuvem, nunca vídeo — a banda de subida é o
   recurso escasso.
6. Se houver inferência remota, o relógio de qualquer lógica temporal é a
   **captura**.
7. Concorrência com **reordenação**; resposta atrasada se descarta, não se
   reinjeta.
8. Painel ao vivo por **polling de imagem única**, não MJPEG.
9. **Nada nasce ligado**; todo descarte é contado por motivo.
10. Guarde os **negativos**, e saiba qual negativo você não está guardando.

---

## Estrutura

| pasta | roda onde | o que tem |
|---|---|---|
| `rasp/` | Raspberry da arena | `servico.py` (captura, painel, alerta), `ia_camera.py` (gatilho pela IA da câmera), `motor.py` (ONNX puro), `cronologia.py` (ordem), `rastreio.py`, `revisao.py`, `config.py` |
| `nuvem/` | Cloud Run (CPU, sem GPU) | `app.py` (endpoint), `Dockerfile` (~400 MB, sem torch) |
| `ferramentas/` | máquina do time | acesso via Cloudflare Tunnel, encaminhamento de porta |

As ferramentas leem a credencial de `ARENA_PASS` no ambiente — nunca do código.

Testes que rodam sem rede e sem câmera:

```bash
cd rasp
python teste_cronologia.py     # ordem, falha, resposta pendurada, controlador
python teste_integracao.py     # gesto contínuo com latência alternada 0,4/3,4 s
python teste_aspecto.py        # geometria da captura (câmera 9:16, substream)
python teste_gatilho.py        # gatilho pela IA, contra uma câmera falsa em localhost
```
