"""Camera parada no painel: previa ao vivo so enquanto alguem olha, nunca
junto com a captura, e com a hora no canto.

    python3 teste_foto.py
"""
import threading
import time
import types

import cv2
import numpy as np

import servico as S

falhas = []


def ok(cond, msg):
    print(("  ok   " if cond else "  FALHA ") + msg)
    if not cond:
        falhas.append(msg)


def espera(cond, s=5):
    t0 = time.time()
    while time.time() - t0 < s:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def camera():
    c = S.Camera.__new__(S.Camera)
    c.mid, c.ativa, c.saida, c.erro = "quadra01_camera01", False, None, None
    c.t_olhar, c.previa, c.p_previa = 0.0, None, None
    c.larg, c.alt, c.fonte = 64, 48, "rtsp://x"
    c._geometria = lambda: True
    return c


class FfmpegFalso:
    """Um quadro preto a cada 50 ms; `kill` corta o pipe como o de verdade."""
    abertos = 0

    def __init__(self, *a, **k):
        FfmpegFalso.abertos += 1
        self.morto = threading.Event()
        self.quadros = 0
        me = self

        class Saida:
            def read(self, n):
                if me.morto.wait(0.05):
                    return b""
                me.quadros += 1
                return bytes(n)
        self.stdout = Saida()

    def kill(self):
        self.morto.set()


def teste_previa():
    print("1. previa: so parada, so com o painel aberto, uma por camera")
    orig_popen, orig_ociosa = S.subprocess.Popen, S.PREVIA_OCIOSA_S
    S.subprocess.Popen = FfmpegFalso
    S.PREVIA_OCIOSA_S = 0.5
    try:
        c = camera()
        ok(c.olhado() is True, "parada e o painel pediu: abre a previa")
        ok(espera(lambda: c.saida is not None), "previa escreveu quadro")
        primeiro = c.saida
        ok(c.olhado() is False and FfmpegFalso.abertos == 1,
           "painel pedindo de novo: nao abre outra")
        ok(espera(lambda: c.saida is not primeiro), "quadro renova sozinho")
        img = cv2.imdecode(np.frombuffer(c.saida, np.uint8), cv2.IMREAD_COLOR)
        ok(img[-25:, :].max() > 200 and img[:16, :].max() < 40,
           "quadro leva a hora no rodape e o resto fica intacto")
        ok(espera(lambda: not c.previa.is_alive(), 3),
           "painel fechado (sem pedido por PREVIA_OCIOSA_S): previa para")
        ok(c.p_previa is None, "e mata o ffmpeg dela")

        c.olhado()
        ok(espera(lambda: c.p_previa is not None), "painel reaberto: previa volta")
        p = c.p_previa
        c.ativa = True                       # o que `liga` faz primeiro
        c.p_previa.kill()                    # e depois isto
        ok(espera(lambda: not c.previa.is_alive(), 2),
           "camera ligou: previa sai, a captura assume")
        ok(p.morto.is_set(), "ffmpeg da previa derrubado")
        c.saida = b"anotado"
        ok(c.olhado() is False and c.saida == b"anotado",
           "capturando: painel pedindo nao abre previa nem pisa no quadro anotado")
    finally:
        S.subprocess.Popen, S.PREVIA_OCIOSA_S = orig_popen, orig_ociosa


def teste_foto():
    print("2. foto de arranque leva a hora no canto")
    c = camera()
    w, h = c.larg, c.alt
    orig = S.subprocess.run
    S.subprocess.run = lambda *a, **k: types.SimpleNamespace(
        stdout=bytes(w * h * 3), stderr=b"", returncode=0)
    try:
        c.foto()
    finally:
        S.subprocess.run = orig
    ok(c.saida is not None, "gravou a foto")
    img = cv2.imdecode(np.frombuffer(c.saida, np.uint8), cv2.IMREAD_COLOR)
    ok(img is not None and img[-25:, :].max() > 200, "rodape com a hora")


if __name__ == "__main__":
    teste_previa()
    teste_foto()
    print()
    print("FALHAS:" if falhas else "tudo ok", *falhas, sep="\n  ")
    raise SystemExit(1 if falhas else 0)
