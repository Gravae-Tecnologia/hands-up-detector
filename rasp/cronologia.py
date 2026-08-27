"""Mantem os resultados da nuvem em ordem cronologica, sem desperdicar quadro.

O PROBLEMA
    Hoje cada camera manda um quadro e fica parada esperando a resposta. Com
    p90 de 1.211 ms contra captura a cada 1.000 ms, ~1 em cada 10 quadros e
    capturado durante a espera e jogado fora - e o descarte se concentra em
    quadra cheia, que e justamente quando o sistema importa.

    Mandar varios ao mesmo tempo resolve o desperdicio e cria outro problema:
    as respostas voltam fora de ordem. O rastreio e uma maquina de estados
    temporal - alimentado fora de ordem, o timer do gesto anda para tras e a
    identidade das pessoas se embaralha.

O DESENHO

    _captura (fora daqui)      N x _envio (1..4)          _consumo (1)
          │                          │                         │
          ├─ le o pipe sempre        ├─ tira da fila           ├─ tira da janela
          ├─ oferece(img)            ├─ chama `enviar`         │  EM ORDEM
          └─ decimado pelo ritmo     └─ devolve na janela      └─ chama `consumir`

    O `seq` e atribuido SO a quadro que vai ser enviado. Quadro decimado pelo
    ritmo nunca recebe seq, entao a decimacao NAO cria buraco na sequencia -
    buraco so existe quando uma requisicao falha, e ai e explicito.

RESULTADO ATRASADO E DESCARTADO, NUNCA REINJETADO
    Se a resposta de um seq ja ultrapassado chegar, ela e contada e jogada
    fora. Melhor perder um quadro que corromper o estado temporal.

O RELOGIO E A CAPTURA
    O `consumir` recebe `t_captura`, nao o instante da resposta. Com latencia
    variando de 400 a 3.420 ms, usar a chegada faria o "segurando_s" do gesto
    carregar o jitter da rede em vez do tempo real da pessoa.
"""
from __future__ import annotations

import heapq
import queue
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Quadro:
    seq: int
    t_captura: float
    img: object = None
    resultado: object = None
    erro: str = None
    t_envio: float = 0.0
    t_resposta: float = 0.0
    meta: dict = field(default_factory=dict)
    fora_de_ordem: bool = False
    espera_janela_ms: float = 0.0


class JanelaReordenacao:
    """Entrega os quadros em ordem de `seq`, segurando os adiantados.

    A profundidade e limitada pela concorrencia: com N em voo, no maximo N-1
    resultados podem estar esperando o mais antigo. Com teto de 4, a janela
    nunca passa de 3 - nao ha como crescer sem limite.
    """

    def __init__(self, espera_max=2.5):
        self.espera_max = espera_max
        self.lock = threading.Lock()
        self.tem_algo = threading.Condition(self.lock)
        self._proximo = 1
        self._prontos = {}          # seq -> (Quadro, t_ficou_pronto)
        self._em_voo = set()
        self._heap = []
        self._max_visto = 0         # maior seq ja enviado; teto do avanco
        # metricas
        self.fora_de_ordem = 0
        self.tardios = 0
        self.abandonados = 0
        self.profundidade_max = 0

    def registra_envio(self, seq):
        with self.lock:
            self._em_voo.add(seq)
            self._max_visto = max(self._max_visto, seq)

    def pronto(self, quadro):
        """Chamado pela thread de envio quando a resposta chega."""
        with self.lock:
            self._em_voo.discard(quadro.seq)
            if quadro.seq < self._proximo:
                # o lugar dele na fila ja passou: descarta, nao reinjeta
                self.tardios += 1
                self.tem_algo.notify()
                return False
            if quadro.seq > self._proximo:
                quadro.fora_de_ordem = True
                self.fora_de_ordem += 1
            self._prontos[quadro.seq] = (quadro, time.time())
            heapq.heappush(self._heap, quadro.seq)
            self.profundidade_max = max(self.profundidade_max, len(self._prontos))
            self.tem_algo.notify()
            return True

    def falhou(self, seq):
        with self.lock:
            self._em_voo.discard(seq)
            self.abandonados += 1
            self.tem_algo.notify()

    def _cabeca_esperando_ha(self, agora):
        if not self._heap:
            return 0.0
        return agora - self._prontos[self._heap[0]][1]

    def drena(self, agora=None):
        """-> lista de Quadro em ordem crescente de seq, pronta para consumo.

        Tres regras, nesta ordem:
          1. o proximo esperado esta pronto -> emite;
          2. o proximo nao esta em voo nem pronto -> nunca vem, avanca;
          3. rede de seguranca: a cabeca esperou demais -> avanca mesmo assim.

        A regra 2 resolve falha SEM esperar timer, porque a janela conhece o
        conjunto em voo. O `espera_max` fica so para requisicao que nao
        retorna nem com erro.
        """
        agora = time.time() if agora is None else agora
        saida = []
        with self.lock:
            while self._proximo <= self._max_visto:
                # o teto `_max_visto` e o que impede o laco de andar para
                # sempre quando o proximo esperado nao existe e ainda ha algo
                # em voo mais atras - sem ele, drena() gira segurando o lock.
                if self._proximo in self._prontos:
                    q, t_pronto = self._prontos.pop(self._proximo)
                    heapq.heappop(self._heap)
                    q.espera_janela_ms = round((agora - t_pronto) * 1e3, 1)
                    saida.append(q)
                    self._proximo += 1
                    continue
                if self._proximo in self._em_voo:
                    # ainda pode chegar. So desiste se ha alguem pronto atras
                    # dele esperando ha mais que `espera_max` - requisicao que
                    # nao volta nem com erro nao pode travar a camera.
                    if (self._heap and
                            self._cabeca_esperando_ha(agora) > self.espera_max):
                        self.abandonados += 1
                        self._proximo += 1
                        continue
                    break
                # nao esta em voo nem pronto: falhou. Avanca so se ha algo
                # depois dele para entregar.
                if self._heap or self._em_voo:
                    self._proximo += 1
                    continue
                break
        return saida

    def espera(self, timeout=0.5):
        with self.lock:
            self.tem_algo.wait(timeout)

    def resumo(self):
        with self.lock:
            return {
                "proximo": self._proximo,
                "prontos_esperando": len(self._prontos),
                # `em_voo_agora` e nao `em_voo`: o Ritmo ja publica `em_voo`
                # como LIMITE, e duas chaves iguais no mesmo resumo faziam a
                # segunda apagar a primeira - o painel mostrava o limite como
                # se fosse 0 e parecia que o controlador nao subia.
                "em_voo_agora": len(self._em_voo),
                "janela_profundidade_max": self.profundidade_max,
                "fora_de_ordem": self.fora_de_ordem,
                "fora_de_ordem_tardios": self.tardios,
                "abandonados": self.abandonados,
            }


class Ritmo:
    """Decide quantas conversas abrir e de quanto em quanto tempo capturar.

    Duas etapas, nesta ordem: a prioridade e nao perder segundo de deteccao,
    entao a primeira resposta a saturacao e abrir mais uma conversa; reduzir o
    fps so entra quando nao ha mais o que abrir. Na volta, o inverso - devolve
    o fps antes de encolher a concorrencia.

    Lento de proposito: avalia a cada `ajuste_s` e move um passo por vez. O
    rastreio adapta a tolerancia ao intervalo medido, entao ritmo regular ele
    absorve mesmo mais lento; ritmo que oscila a cada quadro atrapalha mais.
    """

    PASSO = 0.2

    def __init__(self, fps=1.0, em_voo_min=1, em_voo_max=4, ajuste_s=10.0):
        # 0,85 e nao 1,0 de proposito: o ffmpeg ja entrega no ritmo pedido, com
        # jitter de alguns por cento. Um portao em exatamente 1/fps recusaria o
        # quadro que chegou a 0,98 s como "cedo demais" e descartaria ~40% a
        # toa. O portao existe so para THROTTLE ABAIXO do ritmo de captura -
        # no piso ele tem que deixar tudo passar.
        self.base = 0.85 / max(fps, 0.01)
        self.intervalo_alvo = self.base
        self.em_voo_min = max(1, em_voo_min)
        self.em_voo_max = max(self.em_voo_min, em_voo_max)
        self.em_voo = self.em_voo_min
        self.ajuste_s = ajuste_s
        self.lock = threading.Lock()
        self._ultimo_envio = 0.0
        # preenchido na primeira avaliacao: fixar `time.time()` aqui amarraria
        # o controlador ao relogio real e ele nao seria testavel com relogio
        # injetado - nem sobreviveria a um ajuste de hora do sistema
        self._ultima_avaliacao = None
        self._saturou_na_janela = 0
        self._enviados_na_janela = 0
        # metricas
        self.saturacoes = 0
        self.decimados = 0      # recusados pelo ritmo reduzido
        self.sem_vaga = 0       # recusados por concorrencia cheia
        self.em_voo_pico = self.em_voo

    def pode_enviar(self, agora, vagas_livres):
        """-> True se este quadro deve virar requisicao."""
        with self.lock:
            if agora - self._ultimo_envio < self.intervalo_alvo:
                self.decimados += 1    # ritmo reduzido; nem conta seq
                return False
            if vagas_livres <= 0:
                # sem vaga: a nuvem nao acompanha. Este e o sinal que faz o
                # controlador abrir mais uma conversa.
                self._saturou_na_janela += 1
                self.saturacoes += 1
                self.sem_vaga += 1
                return False
            self._ultimo_envio = agora
            self._enviados_na_janela += 1
            return True

    def avalia(self, agora=None):
        """Aplica a escada. Chamar de tempos em tempos; ele se auto-limita."""
        agora = time.time() if agora is None else agora
        with self.lock:
            if self._ultima_avaliacao is None:
                self._ultima_avaliacao = agora
                return None
            if agora - self._ultima_avaliacao < self.ajuste_s:
                return None
            saturou = self._saturou_na_janela > 0
            self._ultima_avaliacao = agora
            self._saturou_na_janela = 0
            self._enviados_na_janela = 0

            if saturou:
                if self.em_voo < self.em_voo_max:
                    self.em_voo += 1
                    self.em_voo_pico = max(self.em_voo_pico, self.em_voo)
                    return ("subiu_em_voo", self.em_voo)
                self.intervalo_alvo = round(self.intervalo_alvo + self.PASSO, 2)
                return ("subiu_intervalo", self.intervalo_alvo)

            if self.intervalo_alvo > self.base:
                self.intervalo_alvo = round(
                    max(self.base, self.intervalo_alvo - self.PASSO), 2)
                return ("baixou_intervalo", self.intervalo_alvo)
            if self.em_voo > self.em_voo_min:
                self.em_voo -= 1
                return ("baixou_em_voo", self.em_voo)
            return None

    def resumo(self):
        with self.lock:
            return {
                "intervalo_alvo_s": round(self.intervalo_alvo, 2),
                "em_voo": self.em_voo,
                "em_voo_pico": self.em_voo_pico,
                "em_voo_max": self.em_voo_max,
                "saturacoes": self.saturacoes,
                "decimados": self.decimados,
                "sem_vaga": self.sem_vaga,
            }


class Cronologia:
    """Junta captura, envio concorrente e consumo cronologico de uma camera.

      cron = Cronologia(enviar=fn_bloqueante, consumir=fn_em_ordem)
      cron.inicia()
      ...  cron.oferece(img)  a cada quadro lido do pipe
      cron.para()

    `enviar(quadro) -> (resultado, meta)` roda nas threads de envio e pode
    bloquear na rede. Levantar excecao marca o quadro como falho, e a janela
    avanca sem travar a camera.

    `consumir(quadro)` roda numa thread unica, SEMPRE em ordem de seq.
    """

    def __init__(self, enviar, consumir, fps=1.0, em_voo_min=1, em_voo_max=4,
                 ajuste_s=10.0, espera_max=2.5, nome=""):
        self.enviar = enviar
        self.consumir = consumir
        self.nome = nome
        self.ritmo = Ritmo(fps, em_voo_min, em_voo_max, ajuste_s)
        self.janela = JanelaReordenacao(espera_max)
        self.fila = queue.Queue()
        self.parar = threading.Event()
        self._seq = 0
        self._lock = threading.Lock()
        self._enviando = 0            # requisicoes abertas agora
        self._threads = []
        self.enviados = 0
        self.consumidos = 0
        self.falhas = 0

    # ------------------------------------------------------------- ciclo
    def inicia(self):
        self.parar.clear()
        for _ in range(self.ritmo.em_voo_max):
            t = threading.Thread(target=self._laco_envio, daemon=True)
            t.start()
            self._threads.append(t)
        t = threading.Thread(target=self._laco_consumo, daemon=True)
        t.start()
        self._threads.append(t)

    def para(self):
        self.parar.set()
        for _ in self._threads:
            self.fila.put(None)
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []

    # ------------------------------------------------------------- entrada
    def oferece(self, img, agora=None):
        """Chamado pela thread de captura a cada quadro lido do pipe.

        -> True se virou requisicao. False significa decimado pelo ritmo ou
        sem vaga - e nos dois casos o quadro NAO recebe seq, entao nao vira
        buraco na sequencia.
        """
        agora = time.time() if agora is None else agora
        with self._lock:
            vagas = self.ritmo.em_voo - self._enviando
        if not self.ritmo.pode_enviar(agora, vagas):
            return False
        with self._lock:
            self._seq += 1
            q = Quadro(seq=self._seq, t_captura=agora, img=img)
            self._enviando += 1
        self.janela.registra_envio(q.seq)
        self.fila.put(q)
        return True

    # ------------------------------------------------------------- envio
    def _laco_envio(self):
        while not self.parar.is_set():
            q = self.fila.get()
            if q is None:
                return
            # a thread so trabalha se ainda cabe na concorrencia atual; se o
            # controlador encolheu, ela devolve o quadro e dorme um pouco
            q.t_envio = time.time()
            try:
                q.resultado, q.meta = self.enviar(q)
                q.t_resposta = time.time()
                self.enviados += 1
                self.janela.pronto(q)
            except Exception as e:
                q.erro = f"{type(e).__name__}: {str(e)[:120]}"
                self.falhas += 1
                self.janela.falhou(q.seq)
            finally:
                with self._lock:
                    self._enviando -= 1
                # o quadro cru NAO e liberado aqui: o consumo ainda precisa
                # dele para desenhar o esqueleto e recortar a evidencia. Quem
                # libera e o `consumir`, no fim. No pior caso ficam
                # (em_voo + janela) quadros de 768 KB por camera - ~5 MB.

    # ------------------------------------------------------------- consumo
    def _laco_consumo(self):
        while not self.parar.is_set():
            for q in self.janela.drena():
                try:
                    self.consumir(q)
                    self.consumidos += 1
                except Exception as e:
                    print(f"[cronologia {self.nome}] consumo falhou: "
                          f"{type(e).__name__}: {e}", flush=True)
            self.ritmo.avalia()
            self.janela.espera(0.2)

    # ------------------------------------------------------------- metricas
    def resumo(self):
        d = {"enviados": self.enviados, "consumidos": self.consumidos,
             "falhas_envio": self.falhas}
        d.update(self.ritmo.resumo())
        d.update(self.janela.resumo())
        return d
