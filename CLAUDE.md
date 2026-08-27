# Contexto para quem for mexer neste repositório

Leia isto antes de propor mudança. Quase toda decisão aqui tem um número
medido por trás, e várias contrariam a intuição — inclusive uma conclusão que
já se inverteu no meio do projeto.

## O que o produto faz

Detecta **braços levantados** nas câmeras de uma arena e avisa a plataforma na
hora. A Raspberry da arena já roda Shinobi com 4 a 8 câmeras; este serviço vive
ao lado dela.

O critério é geométrico, em `motor.gesto_bracos`:

```
punho.y    < ombro.y − 0,35 × largura_ombros    nos dois lados
cotovelo.y < ombro.y                            nos dois lados
```

Exigir o **cotovelo** é o que separa braço levantado de aceno na altura da
cabeça. A régua de escala é a largura de ombros, não o torso — torso não
existe em enquadramento de meio corpo.

## As quatro invariantes que não se quebram

**1. O gargalo é térmico, não CPU.** A Pi de arena fica em **72 °C parada**,
com o Shinobi rodando. O throttling começa aos 80 °C. Com quatro capturas
locais chegou a **84,7 °C** e o *mesmo modelo* passou de 193 para **1.948 ms**
— 10× mais lento por calor. Antes de propor "roda mais coisa na Pi", olhe a
temperatura.

**2. A captura é a segunda coisa mais cara.** O stream principal das câmeras é
**1280×720@30**, mesmo quando o Shinobi reporta 640×480 (campo diferente). Com
`-vf fps=1` o ffmpeg decodifica tudo e joga 29 de cada 30 fora: **~27% de um
núcleo por câmera**. Por isso a captura só roda em câmera ligada.

**3. Nada nasce ligado.** Uma atualização que chega em centenas de Raspberries
não pode começar a consumir CPU e banda sozinha. O operador liga pelo OPS.

**4. O relógio do rastreio é a CAPTURA, nunca a resposta.** A inferência
mora na nuvem e a latência varia de **400 a 3.420 ms**. Se o rastreio for
alimentado com o instante em que a resposta chegou, o `segurando_s` do gesto
mede jitter de rede em vez do tempo real da pessoa — e com várias requisições
em voo ele chega a andar para trás. `rast.passo(..., agora=q.t_captura)`. Pela
mesma razão, resultado que volta fora de ordem é **reordenado antes** de tocar
o rastreio (`cronologia.py`), e resultado que chega depois do seu lugar é
descartado: melhor perder um quadro que corromper o estado temporal.

## Dois eixos independentes (isto foi medido, não suposto)

- **O detector define quantas pessoas você acha** → recall/precisão/F1
- **A pose define a precisão do punho** → PCK, que é o que decide o gesto

Trocar `rtmpose-s` por `-m` **não muda o F1** — a cabeça de pose não cria nem
descarta pessoa. E trocar de detector quase não muda o erro de punho. Escolha
um de cada eixo separadamente.

Custo de qualquer combinação (reproduz as medições em ~2%):

```
ms_total = ms_detector + ms_por_pessoa × pessoas_em_cena
```

Só o termo do detector é fixo. **Dimensione pelo pior caso de pessoas**, não
pela média.

## A conclusão que se inverteu

O `RTMDet-Nano` foi escolhido primeiro por acertar a média de jogadores em
quadra (3,49 contra 3,36 da referência) e ter 99% das caixas dentro do
polígono. Com uma referência de verdade, ele tem **1.279 falsos positivos e
1.076 jogadores perdidos** — os dois erros se cancelam na média. F1 real:
**75,8**, o pior de doze testados.

> **Nunca decida entre detectores por média de contagem nem por "% dentro".**
> Essas métricas premiam erro que se cancela. Precisa de referência
> (pseudo-GT), casamento por IoU e recall/precisão/F1 separados.

E a referência roda **no PC**: precisão não depende de hardware, o mesmo ONNX
dá a mesma saída na Pi e numa RTX. Reserve a Pi para latência e térmico, que é
o que só ela mede.

## Armadilhas que já custaram tempo

| armadilha | sintoma | causa |
|---|---|---|
| API do Shinobi omite credencial | RTSP responde `401` | `muser`/`mpass` voltam vazios pela API; estão no JSON de `details` **no MariaDB** |
| `host`/`port`/`path` não estão em `details` | `Port missing in uri` | são **colunas** da tabela `Monitors`; precisa juntar as duas fontes |
| CRLF do Windows | `env: 'bash\r': No such file` | `.gitattributes` com `eol=lf` resolve |
| `/etc/gravae` é do root | serviço morre com `PermissionError` no `.tmp` | escrita atômica precisa criar arquivo no diretório; instalador dá escrita ao grupo |
| `pkill -f painel.py` | a própria sessão SSH morre | o padrão casa com o comando que o contém; use `[p]ainel[.]py` |
| `nohup ... &` por paramiko | `exec_command` nunca retorna | o canal só fecha quando os fds fecham; use `setsid` + `</dev/null` e **não leia** a saída |
| Phoenix enfileira alertas | aviso do gesto chega até 5 min depois | `WEBHOOK_SEND_INTERVAL = 300`; por isso o POST do gesto é **direto** |
| Quantização INT8 na Pi 4 | ganho não aparece | Cortex-A72 é ARMv8.0 e **não tem `asimddp`** (SDOT/UDOT chegaram no ARMv8.2). Só vale em Pi 5 |
| `*-pose` one-stage como referência | acha metade das pessoas | `yolo11x-pose` dá 1,97 onde `yolo11x` dá 4,39 — a cabeça de pose suprime quem não consegue posar |

## Acesso às Raspberries

Não têm IP alcançável. O `cloudflared` de cada arena publica três rotas:

```
{sub}.gravae.io        -> Shinobi   :8080
{sub}-agent.gravae.io  -> agente    :8888
{sub}-ssh.gravae.io    -> sshd      :22
```

O túnel é **gerenciado remotamente** (`cloudflared run --token ...`), então a
lista de rotas **não está em arquivo na Pi** — está no painel da Cloudflare.
Adicionar rota é lá.

Use `ferramentas/arena.py` (sobe um proxy TCP do cloudflared e conecta o
paramiko nele). A senha vem de `ARENA_PASS` no ambiente, nunca do código.

## Como testar de verdade

1. **Precisão** → no PC, contra referência. `comparar.py` do projeto de
   pesquisa. Não gaste Pi com isso.
2. **Latência e térmico** → só na Pi, partindo **fria** (<65 °C) e com o BMO
   parado (`pkill -f bmo_os/main.py`; religa com `~/religa-bmo.sh`). Sem isso
   a medição varia 3,5× e você mede calor, não modelo.
3. **Fim a fim** → `--registro`, que grava uma linha JSONL por quadro com os
   instantes absolutos. Sem os instantes não dá para reconstruir a ordem dos
   eventos entre câmeras.

## O que ainda está em aberto

**O critério do gesto não foi validado.** O erro médio de punho é **0,565**
(rtmpose-s) e **0,412** (rtmpose-m) larguras de ombro, contra uma margem de
decisão de **0,35** — os dois erram mais que a própria distância que decide.
Falta filmagem com o gesto acontecendo: nos três vídeos de bancada a taxa-base
era ~0,5%, e no COCO só há 850 positivos, que não transferem para o domínio
(AUC 0,64 na quadra contra 0,82 no COCO).

**Estimativa do que falta:** ~600 positivos do domínio, o que sai de poucos
minutos de filmagem (10 repetições × 2 pessoas × 1 s × 30 fps).

**A URL do webhook** não está definida. O disparo está implementado e testado;
falta apontar.

Ver `MEDICOES.md` para todos os números.
