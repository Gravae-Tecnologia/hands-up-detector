# Contrato OPS ↔ hands-up: ativação pela câmera de IA

Este documento descreve o que o painel do OPS (setup.gravae.io, modo hands-up)
lê e escreve para oferecer as duas ativações:

- **Manual**: a câmera ligada fica sempre ativa. É o comportamento de sempre e
  o padrão.
- **Automática pela câmera de IA**: a câmera ligada só captura enquanto há gente
  em quadra. Quem avisa é a IA da própria câmera, e a captura pausa
  `espera_ia_s` depois da última pessoa vista (padrão 10 min).

A Pi descobre sozinha quais câmeras têm IA. O painel só mostra o resultado e
deixa o operador escolher.

## Caminho

```
OPS ──HTTPS──> agent da arena (:8888, pelo túnel) ──localhost──> detector (:8090)
     GET  /hands-up/status   → devolve o /api/config do detector inteiro em `config`
     POST /hands-up/apply    → {"config": {...}}  (um POST por chave no detector)
     POST /hands-up/install  → clona/atualiza e roda o instalador
```

| peça | versão mínima | por quê |
|---|---|---|
| detector | `c5c4be8` (hands-up-detector#9, #10, #11) | gatilho, sonda de IA, `versao`, ffprobe do Debian 11 |
| agent | **3.7.9** (patch pendente, ver o fim) | repassar `gatilho`/`espera_ia_s`/`sondar_ia` no `apply` |

A **leitura** já funciona com o agent atual (3.7.x): o status repassa o
`/api/config` inteiro. Só a **escrita** do gatilho precisa do 3.7.9. Agent
3.6.x (o Fit Club está no 3.6.9) não tem a ponte do hands-up:
`/hands-up/status` responde 404. Atualize pelo `/update/perform`.

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

Por câmera, em `quadras[].cameras[]` (mesma lista de sempre, com campos novos):

| campo | exemplo real (Fit Club, 10/09) | uso |
|---|---|---|
| `mid`, `ligada`, `processando` | `quadra01_camera01`, `true`, `true` | como antes |
| `ia_usavel` | `true` | selo **IA** na câmera |
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

## Tela sugerida

```
Hands-up · Arena exemplo (parque misto)                 versão c5c4be8
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
  que a Pi mantém aberta. Do evento até o primeiro quadro analisado, a
  captura leva alguns segundos para subir.
- **A câmera só vê movimento.** Quem fica parado não gera evento. As pessoas
  que o detector do hands-up enxerga também renovam o prazo, desde que
  apareçam de forma consistente (3 quadros em 10 s). Um falso positivo
  isolado não segura a quadra.

## Pendente para o painel funcionar ponta a ponta

1. **Agent 3.7.9.** O patch está pronto (`hands_up_module.py`: repassa as três
   chaves, devolve o motivo quando o detector recusa e corrige a atualização
   que reinstalava código velho em silêncio). Sem push no
   `gravae-arena-agent-python`, ele segue como patch para quem mantém o repo.
   Não conflita com o #36.
2. **Fit Club:** agent 3.6.9 → atualizar pelo OPS para aparecer no painel.
