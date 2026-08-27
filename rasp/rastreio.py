"""Rastreia pessoas entre quadros e confirma o gesto por duracao.

POR QUE PRECISA DE RASTREIO
    Confirmar "bracos levantados por 2 segundos" exige saber que a pessoa do
    quadro de agora e a MESMA do anterior. A nuvem devolve keypoints sem
    identidade - ela ve um quadro por vez. O rastreio mora aqui, do lado da
    Pi, que e quem tem a sequencia.

ASSOCIACAO EM DUAS ETAPAS (ideia do ByteTrack)
    Primeiro casa por IoU das caixas, que e forte para quem esta parado ou se
    movendo pouco - o caso de quem levanta a mao. So o que sobra tenta casar
    por distancia de centro, que resolve quem correu entre os quadros.

    O ByteTrack completo usa Kalman para prever a posicao, e a 1 fps isso
    atrapalha mais que ajuda: em um segundo um jogador muda de direcao varias
    vezes e a previsao linear erra mais que a ultima posicao conhecida. Ficam
    as duas ideias que valem aqui: cascata de associacao e sobrevida da
    trilha perdida.

DISTANCIA NORMALIZADA PELA LARGURA DE OMBROS
    Um jogador perto anda 80 px entre quadros; um no fundo anda 8. Um limiar
    em pixels serviria para um e nao para o outro - e era justamente a pessoa
    do fundo que se perdia. Em larguras de ombro o mesmo numero vale para os
    dois, e e a mesma regua do criterio do gesto.

TOLERANCIA ADAPTATIVA - o bug que isto conserta
    A tolerancia era fixa em 1,2 s. Medido na CTF Marcelinho, o intervalo
    MEDIANO entre quadros processados numa camera era **2,5 s** (a latencia
    passou de 1 s e o servico comecou a descartar). Resultado: toda sequencia
    de gesto zerava antes de completar - uma pessoa segurou 4 s e apareceu
    como tres deteccoes instantaneas.

    Agora a tolerancia acompanha o ritmo real: `2,5 x` o intervalo mediano
    observado, com piso no valor configurado. Se a camera acelerar, a
    tolerancia encolhe junto e o criterio nao afrouxa a toa.

A TRILHA EXPIRA MESMO COM O QUADRO VAZIO
    Bug real ja visto neste projeto: quando ninguem aparece, o `passo` nao era
    chamado, as trilhas nao expiravam e a proxima pessoa herdava o estado
    `confirmado` da anterior - e nunca mais disparava.
"""
from __future__ import annotations

import statistics
import time

import numpy as np


def _iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    aa = (a[2] - a[0]) * (a[3] - a[1])
    bb = (b[2] - b[0]) * (b[3] - b[1])
    return float(inter / (aa + bb - inter + 1e-9))


class Trilha:
    __slots__ = ("id", "centro", "escala", "caixa", "visto_em", "gesto_desde",
                 "gesto_ate", "confirmado_em", "picos", "vistas")

    def __init__(self, ident, centro, escala, caixa, agora):
        self.id = ident
        self.centro = centro
        self.escala = escala
        self.caixa = caixa
        self.visto_em = agora
        self.vistas = 1
        self.gesto_desde = None
        self.gesto_ate = None
        self.confirmado_em = None
        self.picos = []

    @property
    def segurando_s(self):
        if self.gesto_desde is None:
            return 0.0
        return (self.gesto_ate or self.gesto_desde) - self.gesto_desde


class Rastreador:
    def __init__(self, dur_s=2.0, tol_s=1.2, dist_max=2.5, iou_min=0.2,
                 expira_s=6.0):
        self.dur_s = dur_s
        self.tol_base = tol_s
        self.dist_max = dist_max      # em larguras de ombro
        self.iou_min = iou_min
        self.expira_s = expira_s
        self.trilhas = []
        self._proximo = 1
        self._intervalos = []
        self._ultimo_passo = None
        self.criadas = 0        # trilhas novas desde o inicio
        self.quadros = 0
        self._ids_ultimo = set()
        self.trocas = 0         # quadros em que uma pessoa ganhou id novo

    # ------------------------------------------------------------------ tol
    @property
    def intervalo(self):
        """Mediana do intervalo real entre quadros processados."""
        return statistics.median(self._intervalos) if self._intervalos else 1.0

    @property
    def tol_s(self):
        """Nunca menor que o configurado, nunca menor que 2,5 quadros reais.

        Com 2,5 s entre quadros e tolerancia de 1,2 s, um gesto continuo
        aparece como deteccoes isoladas - foi o que aconteceu na quadra03.
        """
        return max(self.tol_base, self.intervalo * 2.5)

    def _marca_intervalo(self, agora):
        if self._ultimo_passo is not None:
            d = agora - self._ultimo_passo
            if 0 < d < 30:            # ignora pausa de camera desligada
                self._intervalos.append(d)
                del self._intervalos[:-40]
        self._ultimo_passo = agora

    # ------------------------------------------------------------- passo
    def passo(self, kpts, margens, limiar, caixas=None, agora=None):
        """Um quadro. Devolve (por_pessoa, confirmados)."""
        agora = time.time() if agora is None else agora
        self._marca_intervalo(agora)
        tol = self.tol_s

        atuais = []
        for i, k in enumerate(kpts):
            ombro = k[[5, 6], :2]
            larg = float(np.linalg.norm(ombro[0] - ombro[1]))
            cx = caixas[i] if (caixas is not None and i < len(caixas)) else None
            atuais.append((ombro.mean(axis=0), max(larg, 1.0), cx))

        livres_i = set(range(len(atuais)))
        livres_t = {id(t): t for t in self.trilhas}
        casado = {}

        # etapa 1: IoU das caixas. Forte para quem esta parado - o caso de
        # quem levanta a mao e espera.
        if caixas is not None:
            cand = []
            for i in livres_i:
                if atuais[i][2] is None:
                    continue
                for t in livres_t.values():
                    if t.caixa is None:
                        continue
                    v = _iou(atuais[i][2], t.caixa)
                    if v >= self.iou_min:
                        cand.append((-v, i, t))
            for _, i, t in sorted(cand, key=lambda z: z[0]):
                if i in casado or id(t) not in livres_t:
                    continue
                casado[i] = t
                livres_t.pop(id(t))
            livres_i -= set(casado)

        # etapa 2: distancia de centro, normalizada pela largura de ombros
        cand = []
        for i in livres_i:
            c, e, _ = atuais[i]
            for t in livres_t.values():
                d = float(np.linalg.norm(c - t.centro)) / max(e, t.escala)
                if d <= self.dist_max:
                    cand.append((d, i, t))
        for _, i, t in sorted(cand, key=lambda z: z[0]):
            if i in casado or id(t) not in livres_t:
                continue
            casado[i] = t
            livres_t.pop(id(t))

        por_pessoa, confirmados = [], []
        for i, (c, e, cx) in enumerate(atuais):
            t = casado.get(i)
            if t is None:
                t = Trilha(self._proximo, c, e, cx, agora)
                self._proximo += 1
                self.criadas += 1
                self.trilhas.append(t)
            else:
                t.vistas += 1
            t.centro, t.escala, t.caixa, t.visto_em = c, e, cx, agora

            m = margens[i]
            tem = (m is not None and m >= limiar)
            if tem:
                if t.gesto_desde is None or (t.gesto_ate is not None and
                                             agora - t.gesto_ate > tol):
                    t.gesto_desde = agora
                    t.confirmado_em = None
                    t.picos = []
                t.gesto_ate = agora
                t.picos.append(float(m))
                if t.confirmado_em is None and t.segurando_s >= self.dur_s:
                    t.confirmado_em = agora
                    confirmados.append(t)
            elif (t.gesto_ate is not None and agora - t.gesto_ate > tol):
                t.gesto_desde = t.gesto_ate = None
                t.confirmado_em = None
                t.picos = []

            por_pessoa.append({
                "id": t.id,
                "margem": m,
                "instantaneo": tem,
                "segurando_s": round(t.segurando_s, 2),
                "confirmado": t.confirmado_em is not None,
                "vistas": t.vistas,
                "pico": round(max(t.picos), 3) if t.picos else None,
            })

        # expira: roda mesmo com o quadro vazio. A sobrevida e generosa de
        # proposito - trilha perdida por uma oclusao vale mais viva que morta,
        # e quem some de vez sai em `expira_s` de qualquer jeito.
        limite = max(self.expira_s, tol * 2)
        self.trilhas = [t for t in self.trilhas if agora - t.visto_em <= limite]

        # churn: quantas pessoas deste quadro sao id novo. Se o numero de
        # pessoas nao mudou mas ha id novo, o rastreio perdeu alguem - e essa
        # e a unica forma de enxergar isso sem olhar video.
        ids = {p["id"] for p in por_pessoa}
        novas = len(ids - self._ids_ultimo)
        if novas and len(ids) <= len(self._ids_ultimo):
            self.trocas += 1
        self._ids_ultimo = ids
        self.quadros += 1
        return por_pessoa, confirmados

    def resumo(self):
        vistas = [t.vistas for t in self.trilhas]
        return {
            "trilhas_vivas": len(self.trilhas),
            "criadas": self.criadas,
            "quadros": self.quadros,
            # trilhas por quadro: 1,0 significa uma pessoa nova a cada quadro,
            # ou seja, rastreio nao esta segurando ninguem
            "criadas_por_quadro": round(self.criadas / max(self.quadros, 1), 2),
            "trocas": self.trocas,
            "pct_quadros_com_troca": round(
                self.trocas / max(self.quadros, 1) * 100, 1),
            "vistas_media": round(sum(vistas) / max(len(vistas), 1), 1),
            "vistas_max": max(vistas) if vistas else 0,
            "intervalo_s": round(self.intervalo, 2),
            "tolerancia_s": round(self.tol_s, 2),
            "dur_s": self.dur_s,
        }
