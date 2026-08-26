"""Rastreia pessoas entre quadros e confirma o gesto por duracao.

POR QUE PRECISA DE RASTREIO
    Confirmar "bracos levantados por 2 segundos" exige saber que a pessoa do
    quadro de agora e a MESMA do quadro anterior. Sem isso, duas pessoas
    levantando o braco em quadros alternados pareceriam uma pessoa segurando
    o gesto.

    A nuvem devolve keypoints sem identidade - ela ve um quadro por vez. O
    rastreio mora aqui, do lado da Pi, que e quem tem a sequencia.

DISTANCIA NORMALIZADA PELA LARGURA DE OMBROS
    Um jogador perto anda 80 px entre quadros; um no fundo anda 8. Um limiar
    em pixels serviria para um e nao para o outro. Medindo em larguras de
    ombro, o mesmo numero vale para os dois - e a mesma regua que o criterio
    do gesto ja usa.

TOLERANCIA A BURACO
    A 1 fps, 2 segundos sao 2 ou 3 amostras. Uma deteccao perdida no meio
    (oclusao, jogador de costas) zeraria o timer e o gesto nunca confirmaria.
    Por isso a trilha sobrevive `tol_s` sem ser vista antes de considerar que
    o gesto foi interrompido.

A TRILHA EXPIRA MESMO COM O QUADRO VAZIO
    Bug real ja visto neste projeto: quando ninguem aparece, o `passo` nao era
    chamado, as trilhas nao expiravam e a proxima pessoa herdava o estado
    `ja_disparou` da anterior - e nunca mais disparava. Por isso `passo` roda
    todo quadro, com lista vazia ou nao.
"""
from __future__ import annotations

import time

import numpy as np


class Trilha:
    __slots__ = ("id", "centro", "escala", "visto_em", "gesto_desde",
                 "gesto_ate", "confirmado_em", "picos")

    def __init__(self, ident, centro, escala, agora):
        self.id = ident
        self.centro = centro
        self.escala = escala
        self.visto_em = agora
        self.gesto_desde = None     # quando o gesto comecou
        self.gesto_ate = None       # ultima vez que foi visto com gesto
        self.confirmado_em = None   # ja disparou nesta sequencia?
        self.picos = []

    @property
    def segurando_s(self):
        if self.gesto_desde is None:
            return 0.0
        return (self.gesto_ate or self.gesto_desde) - self.gesto_desde


class Rastreador:
    """Associacao por proximidade do centro dos ombros.

    Guloso e nao Hungaro de proposito: numa quadra ha 2 a 8 pessoas bem
    separadas, entao o otimo global e o guloso coincidem e o guloso nao traz
    dependencia nova.
    """

    def __init__(self, dur_s=2.0, tol_s=1.2, dist_max=1.8, expira_s=4.0):
        self.dur_s = dur_s
        self.tol_s = tol_s
        self.dist_max = dist_max      # em larguras de ombro
        self.expira_s = expira_s
        self.trilhas = []
        self._proximo = 1

    def passo(self, kpts, margens, limiar, agora=None):
        """Um quadro. Devolve (por_pessoa, confirmados).

        por_pessoa: lista de dicts alinhada com `kpts`
        confirmados: trilhas que ACABARAM de completar a duracao
        """
        agora = time.time() if agora is None else agora

        # posicao e escala de cada pessoa deste quadro
        atuais = []
        for k in kpts:
            ombro = k[[5, 6], :2]
            larg = float(np.linalg.norm(ombro[0] - ombro[1]))
            atuais.append((ombro.mean(axis=0), max(larg, 1.0)))

        # --- associacao gulosa -------------------------------------------
        pares = []
        for i, (c, e) in enumerate(atuais):
            for t in self.trilhas:
                d = float(np.linalg.norm(c - t.centro)) / max(e, t.escala)
                if d <= self.dist_max:
                    pares.append((d, i, t))
        pares.sort(key=lambda x: x[0])
        usados_i, usados_t = set(), set()
        casado = {}
        for d, i, t in pares:
            if i in usados_i or id(t) in usados_t:
                continue
            casado[i] = t
            usados_i.add(i)
            usados_t.add(id(t))

        por_pessoa, confirmados = [], []
        for i, (c, e) in enumerate(atuais):
            t = casado.get(i)
            if t is None:
                t = Trilha(self._proximo, c, e, agora)
                self._proximo += 1
                self.trilhas.append(t)
            t.centro, t.escala, t.visto_em = c, e, agora

            m = margens[i]
            tem = (m is not None and m >= limiar)
            if tem:
                # buraco maior que a tolerancia = sequencia nova
                if t.gesto_desde is None or (t.gesto_ate is not None and
                                             agora - t.gesto_ate > self.tol_s):
                    t.gesto_desde = agora
                    t.confirmado_em = None
                    t.picos = []
                t.gesto_ate = agora
                t.picos.append(float(m))
                if (t.confirmado_em is None and
                        t.segurando_s >= self.dur_s):
                    t.confirmado_em = agora
                    confirmados.append(t)
            elif (t.gesto_ate is not None and
                  agora - t.gesto_ate > self.tol_s):
                t.gesto_desde = t.gesto_ate = None
                t.confirmado_em = None
                t.picos = []

            por_pessoa.append({
                "id": t.id,
                "margem": m,
                "instantaneo": tem,
                "segurando_s": round(t.segurando_s, 2),
                "confirmado": t.confirmado_em is not None,
                "pico": round(max(t.picos), 3) if t.picos else None,
            })

        # --- expira trilhas velhas ---------------------------------------
        # roda mesmo com o quadro vazio: sem isto a proxima pessoa herdaria o
        # estado da anterior e nunca dispararia
        self.trilhas = [t for t in self.trilhas
                        if agora - t.visto_em <= self.expira_s]
        return por_pessoa, confirmados
