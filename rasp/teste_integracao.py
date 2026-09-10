"""Integra Camera + Cronologia + rastreio, sem rede nem camera.

Simula o caso extremo que motivou a estrutura: latencia alternando entre
400 ms e 3.400 ms com captura a 1 fps. Antes, o gesto continuo se fragmentava
em deteccoes isoladas; agora ele tem que confirmar.

  python teste_integracao.py
"""
from __future__ import annotations

import threading
import time

import numpy as np

import cronologia
import motor
import rastreio as rastmod


def pessoa(x, y, cima, larg=20):
    k = np.zeros((17, 3), np.float32)
    k[:, 2] = 1.0
    k[5] = [x - larg, y, 1]
    k[6] = [x + larg, y, 1]
    dy = -30 if cima else 40
    k[7] = [x - larg, y + dy * 0.5, 1]
    k[8] = [x + larg, y + dy * 0.5, 1]
    k[9] = [x - larg, y + dy * 1.5, 1]
    k[10] = [x + larg, y + dy * 1.5, 1]
    return k, [x - larg * 1.5, y - 20, x + larg * 1.5, y + 90]


def ok(cond, msg):
    print(f"  {'ok  ' if cond else 'FALHOU'} {msg}")
    if not cond:
        raise SystemExit(1)


def main():
    print("integracao: gesto continuo com latencia alternada 0,4 s / 3,4 s")

    rast = rastmod.Rastreador(dur_s=2.0)
    vistos = []        # (t_captura, segurando_s, confirmado)
    confirmou = []

    def enviar(q):
        # a resposta e sempre a mesma pessoa com bracos levantados; o que
        # varia e QUANTO ela demora a voltar
        lat = 3.4 if q.seq % 2 else 0.4
        time.sleep(lat)
        k, cx = pessoa(300 + q.seq * 2, 120, True)
        return ({"kpts": [k.tolist()], "caixas": [cx], "pessoas": 1,
                 "gestos": 0, "ms_servidor": lat * 1000}, {})

    def consumir(q):
        k = np.array(q.resultado["kpts"][0], np.float32)
        cx = q.resultado["caixas"]
        m = motor.gesto_margem(k)
        # O RELOGIO E A CAPTURA: e isto que faz o teste passar. Com t_resposta
        # o `segurando_s` carregaria o jitter de 3 s da rede.
        pp, cf = rast.passo([k], [m], 0.35, caixas=cx, agora=q.t_captura)
        vistos.append((q.t_captura, pp[0]["segurando_s"], pp[0]["confirmado"],
                       q.seq))
        if cf:
            confirmou.append(q.seq)

    cron = cronologia.Cronologia(enviar=enviar, consumir=consumir, fps=1.0,
                                 em_voo_min=4, em_voo_max=4, nome="int")
    cron.inicia()

    t0 = time.time()
    for i in range(8):                    # 8 quadros a 1 fps
        cron.oferece(np.zeros((4, 4, 3), np.uint8), agora=t0 + i * 1.0)
        time.sleep(1.0)
    time.sleep(5.0)
    cron.para()

    seqs = [v[3] for v in vistos]
    ok(seqs == sorted(seqs), f"consumo em ordem: {seqs}")
    ts = [v[0] for v in vistos]
    ok(ts == sorted(ts), "t_captura monotonico")
    seg = [round(v[1], 1) for v in vistos]
    print(f"     segurando: {seg}")
    ok(max(seg) >= 2.0, f"acumulou ate {max(seg)} s")
    ok(confirmou, f"confirmou nos seq {confirmou}")
    ok(len(confirmou) == 1, "confirmou UMA vez, nao a cada quadro")

    r = cron.resumo()
    print(f"     fora_de_ordem={r['fora_de_ordem']} "
          f"profundidade_max={r['janela_profundidade_max']} "
          f"consumidos={r['consumidos']}")
    ok(r["fora_de_ordem"] > 0, "houve resposta adiantada (era o risco)")
    ok(r["janela_profundidade_max"] <= 3, "janela nunca passou de N-1")
    print("\npassou")


if __name__ == "__main__":
    main()
