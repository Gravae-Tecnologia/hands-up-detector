"""Foto de camera parada: renovada so enquanto o painel olha, uma de cada vez,
e com a hora no canto.

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


def camera():
    c = S.Camera.__new__(S.Camera)
    c.mid, c.ativa, c.saida, c.erro = "quadra01_camera01", False, None, None
    c.t_foto, c.tirando = 0.0, False
    c.larg, c.alt, c.fonte = 64, 48, "rtsp://x"
    c._geometria = lambda: True
    return c


def teste_ritmo():
    print("1. renova_foto: so parada, uma de cada vez, a cada FOTO_PARADA_S")
    fotos, libera = [], threading.Event()

    def foto_falsa(self, *a, **k):
        fotos.append(time.time())
        libera.wait(5)
        self.tirando = False

    orig = S.Camera.foto
    S.Camera.foto = foto_falsa
    try:
        c = camera()
        ok(c.renova_foto() is True and c.tirando, "parada e sem foto recente: tira")
        ok(c.renova_foto() is False, "com um ffmpeg de foto rodando: nao abre outro")
        libera.set()
        t0 = time.time()
        while c.tirando and time.time() - t0 < 5:
            time.sleep(0.01)
        ok(c.renova_foto() is False, "foto recente (< FOTO_PARADA_S): nao tira")
        c.t_foto -= S.FOTO_PARADA_S + 1
        ok(c.renova_foto() is True, "passou FOTO_PARADA_S: tira de novo")
        t0 = time.time()
        while c.tirando and time.time() - t0 < 5:
            time.sleep(0.01)
        c.t_foto, c.ativa, c.saida = 0.0, True, b"jpeg anotado"
        ok(c.renova_foto() is False, "capturando: nao tira (o quadro anotado ja vem a 1 fps)")
        c.saida = None
        ok(c.renova_foto() is True, "capturando mas sem nenhuma imagem ainda: tira")
        ok(len(fotos) == 3, f"ffmpeg de foto chamados: {len(fotos)}")
    finally:
        libera.set()
        S.Camera.foto = orig


def teste_carimbo():
    print("2. foto leva a hora no canto e solta o `tirando`")
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
    ok(img is not None and img[h - 25:, :].max() > 200,
       "quadro preto ganhou texto claro no rodape (a hora)")
    ok(img is not None and img[:h // 3, :].max() < 40, "resto da imagem intacto")
    ok(c.tirando is False, "tirando volta a False no fim")


if __name__ == "__main__":
    teste_ritmo()
    teste_carimbo()
    print()
    print("FALHAS:" if falhas else "tudo ok", *falhas, sep="\n  ")
    raise SystemExit(1 if falhas else 0)
