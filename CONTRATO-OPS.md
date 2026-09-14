# Contrato OPS ↔ hands-up: ativação pela câmera de IA

Este documento descreve o que o painel do OPS (setup.gravae.io, modo hands-up)
lê e escreve para oferecer as duas ativações:

- **Manual**: a câmera ligada fica sempre ativa. É o comportamento de sempre e
  o padrão.
- **Automática pela câmera de IA**: a câmera ligada só captura enquanto há gente
  em quadra. Quem avisa é a IA da própria câmera, e a captura pausa
  `espera_ia_s` depois da última pessoa vista (padrão 10 min).

A Pi descobre sozinha quais câmeras têm IA. O painel só mostra o resultado e
deixa o operador escolher. Vale para **Intelbras/Dahua** (`SmartMotionHuman`) e
**Hikvision** (movimento com alvo humano, a MD 2.0, e eventos inteligentes da
AcuSense). O selo e o `modo` são os mesmos para os dois; só `ia.fabricante` e
`ia.eventos` dizem de onde vem.

## Caminho

```
OPS ──HTTPS──> agent da arena (:8888, pelo túnel) ──localhost──> detector (:8090)
     GET  /hands-up/status   → devolve o /api/config do detector inteiro em `config`
     POST /hands-up/apply    → {"config": {...}}  (um POST por chave no detector)
     POST /hands-up/install  → {"revision": <SHA de 40>, ...}: busca essa revisão e roda o instalador
```

Desde o OPS #428/#429 o OPS fala com o agent pelo serviço de suporte privado
(por `deviceId`, transporte LEGACY ou VPN), e não direto pelo túnel.

| peça | versão mínima | por quê |
|---|---|---|
| detector | `d035a78` (hands-up-detector#21) | gatilho, sonda multi-fabricante, `versao`, estado em `/var/lib/gravae-hands-up` (#19/#20) e o painel local que diz por que cada câmera analisa (#21) |
| agent | **4.0.5** (patch pendente, ver o fim) | a 4.0.4 instala por revisão fixa, mas não repassa `gatilho`/`espera_ia_s`/`sondar_ia` no `apply` |

A **leitura** já funciona com o agent 4.0.4 (e com a ponte da 3.7.x): o status
repassa o `/api/config` inteiro. Só a **escrita** do gatilho precisa da 4.0.5.
Agent 3.6.x não tem a ponte do hands-up: `/hands-up/status` responde 404.

## Leitura: `GET /hands-up/status` → `config`

Campos da arena:

| campo | tipo | uso no painel |
|---|---|---|
| `gatilho` | `"manual"` \| `"ia"` | posição do seletor de ativação |
| `espera_ia_s` | int (60–3600) | "pausa depois de N min sem ninguém" |
| `ia_resumo.com_ia` / `.cameras` | int | "3 de 3 câmeras com IA" |
| `ia_resumo.sem_resposta` | int | câmeras que não responderam à sonda (tenta de novo sozinha a cada 5 min) |
| `ia_resumo.sondando` | bool | a sonda está rodando agora |
| `versao` | str (commit) | conferir que a atualização chegou a esta Pi |
| `modos` | `{modo: descrição}` | legenda pronta dos estados |

Por câmera, em `quadras[].cameras[]` (mesma lista de sempre, com campos novos). A lista traz **todas** as
câmeras do Shinobi mesmo com tudo desligado, como numa instalação nova: `ligada` e `processando` vêm
`false`, `modo` vem `desligada`, `presenca` vem `null`, e `ia` vem `null` só até a sonda do arranque
terminar.

| campo | exemplo real (Fit Club, 10/09) | uso |
|---|---|---|
| `mid`, `ligada`, `processando` | `quadra01_camera01`, `true`, `true` | como antes |
| `ia_usavel` | `true` | selo **IA** na câmera |
| `ia.fabricante` | `"intelbras/dahua"`, `"hikvision"` ou `null` | de quem é a API que respondeu |
| `ia.eventos` | `["SmartMotionHuman"]`, `["VMD"]`, `["fielddetection"]` | o que a Pi ouve quando a IA está ligada; vazio se não está |
| `ia.modelo` | `"VIP-3430-D-IA"` | texto do selo |
| `ia.motivo` | `null`, ou `"deteccao de humano desligada na camera (...)"` | por que **não** dá para usar a IA |
| `modo` | `"gente"` | estado atual, ver a tabela abaixo |
| `presenca.ha_s` | `132` | "última pessoa há 2 min" (`null` = ninguém desde que começou a ouvir) |
| `presenca.restante_s` | `468` | "pausa em 7 min se ninguém aparecer" |
| `presenca.em_curso` | `false` | a câmera está vendo gente se mexer agora |
| `presenca.conectado` | `true` | a Pi está ouvindo a câmera |
| `evento_erro` | `null` | motivo da queda da conexão com a câmera |
| `gatilho_ia_s` | `{"ativa": 103, "pausada": 0, "pct_pausada": 0.0}` | economia desde que o serviço subiu |

### Estados (`modo`)

| modo | captura? | texto sugerido | cor |
|---|---|---|---|
| `desligada` | não | Desligada | cinza |
| `manual` | sim | Sempre ativa | azul |
| `sem_ia` | sim | Sempre ativa: câmera sem IA (`ia.motivo`) | azul |
| `gente` | sim | Ativa: gente em quadra (pausa em `restante_s`) | verde |
| `sem_sinal` | sim | Ativa: sem contato com a IA da câmera | amarelo |
| `pausada` | **não** | Pausada: ninguém há mais de `espera_ia_s` | cinza claro |
| `forcada` | depende | Forçada no painel local da Pi (depuração) | roxo |

> **`processando: false` com `modo: "pausada"` é o normal no modo automático,
> não é erro.** Se o painel ou o medidor de custo usam `ligada` ou
> `processando` para colorir ou estimar gasto, passe a considerar o `modo`:
> custo real é o tempo `ativa` em `gatilho_ia_s`.

## Escrita: `POST /hands-up/apply`

```json
{"config": {"gatilho": "ia"}}
{"config": {"gatilho": "manual"}}
{"config": {"espera_ia_s": 600}}
{"config": {"sondar_ia": true}}
```

- Pode ir junto com as chaves de sempre (`ativo`, `quadras`, `cameras`, `nuvem`,
  `webhook`). A ponte manda o gatilho **antes** do `ativo`.
- **Valor inválido** (`gatilho` fora de manual/ia, `espera_ia_s` fora de
  60–3600): o passo volta `ok: false` com `erro`, e nada é gravado.
- **Detector antigo** (anterior ao #9): o passo volta `ok: false`, `erro:
  "detector desatualizado..."`. A ponte confere o eco em vez de fingir que
  aplicou. Nesse caso, ofereça "Reinstalar".
- **`sondar_ia`** ("Testar câmeras"): responde na hora. O resultado aparece no
  próximo status, em poucos segundos (`ia_resumo.sondando` volta a `false`).

## Instalação (`POST /hands-up/install`)

Agent 4.0.4+: o corpo leva `revision` (SHA completo de 40 caracteres), `nuvem`
e `webhook`. O agent busca só essa revisão (`fetch --depth 1` + `checkout
--detach`), confere que o HEAD é o pedido e **recusa clone com alteração
local** (`etapa: "revision"`, `erro: "checkout possui alteracoes locais"`). A
Pi passa a responder `versao` = os 7 primeiros caracteres da revisão.

Responde na hora: `{"ok": true, "iniciado": true, "instalacao": {"estado": "rodando", "revision": ..., ...}}`.
Se já houver uma instalação em curso, vem `{"ok": false, "ja_rodando": true, ...}`. Acompanhe
por `status().instalacao`:

| campo | valores |
|---|---|
| `estado` | `ocioso` → `rodando` → `concluido` \| `falhou` |
| `etapa` | `clone`, `revision` (falha ao buscar/conferir a revisão ou clone sujo), `instalar.sh` |
| `ok`, `erro`, `saida` | preenchidos no fim; `saida` traz o fim do log do instalador |

**Já instalado:** o mesmo botão atualiza o código e roda o instalador de novo, que preserva a
config (nada liga, nada desliga). O serviço reinicia, então o detector fica ~2 s sem responder.

Medido na Costa Verde (Pi 5, Debian 13, 8 câmeras, 10/09):

| caso | tempo |
|---|---|
| do zero, sem OpenCV na Pi | 181 s: clone ~35 s + `instalar.sh` 146 s (apt do `python3-opencv`) |
| já instalado | 9 s |
| do serviço subir até `ia` preenchido nas 8 câmeras | ~3 s (sondas em paralelo) |

Logo depois de instalar, `quadras` e `cameras` **não** vêm `{}`: o serviço cria todas como
`false` ao subir.

## Tela sugerida

```
Hands-up · Arena exemplo (parque misto)                 versão d035a78
Ativação  ( ) Manual: sempre ativa
          (•) Automática: só com gente em quadra        3 de 3 câmeras com IA
              pausa depois de [10] min sem ninguém       [Testar câmeras]

Quadra 01  cam01  [IA · VIP-3430-D-IA]  ● Ativa, gente em quadra, pausa em 7 min
Quadra 02  cam01  [IA · VIP-3430-D-IA]  ○ Pausada: ninguém há 10 min
Quadra 03  cam01  [sem IA]              ● Sempre ativa: câmera sem IA
Economia hoje: 64% do tempo pausado
```

- Com `ia_resumo.com_ia == 0`, desabilite "Automática" e explique: "nenhuma
  câmera desta arena tem IA de humano".
- Num parque misto, "Automática" continua valendo: as câmeras sem IA ficam
  sempre ativas (`sem_ia`) e as com IA pausam.

## O que a Pi garante

- **Na dúvida, fica ativa.** Só pausa câmera que tem IA, com a detecção de
  humano ligada nela, conectada e sem ninguém há `espera_ia_s`. Câmera que não
  respondeu à sonda, conexão caída há mais de 30 s ou serviço recém-reiniciado:
  fica ativa.
- **Câmera vendo gente acorda na hora.** O evento chega por uma conexão HTTP
  que a Pi mantém aberta. Do evento até o primeiro quadro analisado, ~6 s
  (medido no Fit Club: 4,6 s até o primeiro quadro capturado, 5,3 s até o
  primeiro analisado, mais até 1 s do laço).
- **A câmera só vê movimento.** Quem fica parado não gera evento. As pessoas
  que o detector do hands-up enxerga também renovam o prazo, desde que
  apareçam de forma consistente (3 quadros em 10 s). Um falso positivo
  isolado não segura a quadra.

## Pendente para o painel funcionar ponta a ponta

1. **Agent 4.0.5.** O porte em cima da 4.0.4 está pronto
   (`hands_up_module.py`: repassa as três chaves antes do `ativo`, confere o
   eco e devolve o motivo quando o detector recusa com 400). Sem push no
   `gravae-arena-agent-python`, ele segue como patch para quem mantém o repo.
   O painel deve confirmar o gatilho pela leitura de volta
   (`estado.config.gatilho`), e não pelo número da versão.
2. **Agents antigos nas arenas:** Fit Club 3.6.9, CTF Marcelinho 3.7.6 e
   Costa Verde 3.7.8 precisam da 4.0.4+ antes de instalar por revisão.
