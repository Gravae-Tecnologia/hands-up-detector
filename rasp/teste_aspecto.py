"""Testes da geometria de captura e da escolha de entrada do detector.
Sem rede, sem camera, sem modelo.

  python teste_aspecto.py

Os casos com numero sao os medidos na CTF Marcelinho (10/09/2026): stream
principal 1440x2560 H.264, substream 480x704 H.265 com a MESMA cena - pixels
esticados 21% na horizontal, sem `sample_aspect_ratio` declarado.
"""
from __future__ import annotations

import motor
from servico import dimensoes_analise, dims_do_stream, url_principal, url_substream


def ok(cond, msg):
    print(f"  {'ok  ' if cond else 'FALHOU'} {msg}")
    if not cond:
        raise SystemExit(1)


def _prop(wh):
    return wh[0] / wh[1]


# ------------------------------------------------------------- substream
def teste_url():
    print("1. url do substream e do principal")
    base = "rtsp://u:p@192.168.1.10:554/cam/realmonitor?channel=1"
    ok(url_substream(base + "&subtype=0") == base + "&subtype=1",
       "Intelbras/Dahua: subtype=0 -> 1")
    ok(url_substream(base + "&subtype=0&unicast=true") == base + "&subtype=1&unicast=true",
       "parametro no meio da query")
    ok(url_principal(base + "&subtype=1") == base + "&subtype=0",
       "Shinobi cadastrado no substream: a proporcao ainda sai do principal")
    hik = "rtsp://u:p@10.0.0.9:554/Streaming/Channels/"
    ok(url_substream(hik + "101") == hik + "102", "Hikvision: 101 -> 102")
    ok(url_substream(hik + "1101") == hik + "1102", "Hikvision canal 11: 1101 -> 1102")
    ok(url_principal(hik + "102") == hik + "101", "Hikvision: 102 -> 101")
    ok(url_substream("rtsp://x/stream1") is None, "padrao desconhecido fica no principal")


# ------------------------------------------------------------- geometria
def teste_ctf():
    print("2. CTF Marcelinho: principal 1440x2560, substream 480x704 anamorfico")
    w, h = dimensoes_analise((1440, 2560), (480, 704))
    ok((w, h) == (396, 704), f"quadro de analise {w}x{h} (esperado 396x704)")
    ok(abs(_prop((w, h)) - 9 / 16) < 0.01, f"proporcao {_prop((w, h)):.4f} ~ 9:16")
    ok(h == 704, "altura nativa inteira: nenhum pixel vertical perdido")
    # o que o codigo antigo fazia: scale=640:400 fixo
    achatamento = (640 / 1440) / (400 / 2560)
    ok(achatamento > 2.8, f"o antigo achatava a pessoa {achatamento:.2f}x na vertical")


def teste_sem_substream():
    print("3. so o principal: reduz mantendo a proporcao")
    w, h = dimensoes_analise((1440, 2560), (1440, 2560), lado_max=720)
    ok(h == 720 and w % 2 == 0, f"{w}x{h}, lado maior = 720, lados pares")
    ok(abs(_prop((w, h)) - 9 / 16) < 0.01, f"proporcao {_prop((w, h)):.4f} ~ 9:16")


def teste_paisagem():
    print("4. camera 16:9 continua funcionando como antes")
    ok(dimensoes_analise((1280, 720), (640, 360)) == (640, 360),
       "substream ja na proporcao: passa inteiro")
    w, h = dimensoes_analise((1280, 720), (704, 480))
    ok((w, h) == (704, 396), f"substream D1 anamorfico 704x480 -> {w}x{h}")


def teste_nunca_aumenta():
    print("5. nunca aumenta resolucao, sempre par")
    casos = [((1440, 2560), (480, 704)), ((1280, 720), (704, 480)),
             ((1920, 1080), (1920, 1080)), ((1080, 1920), (352, 288)),
             ((2560, 1440), (640, 480))]
    for real, nat in casos:
        w, h = dimensoes_analise(real, nat)
        ok(w <= nat[0] and h <= nat[1] and w % 2 == 0 and h % 2 == 0
           and abs(_prop((w, h)) - _prop(real)) < 0.02,
           f"real {real} lido {nat} -> {w}x{h}")


def teste_ffprobe():
    print("6. leitura do ffprobe: 4.x (Debian 11) e 5+")
    ok(dims_do_stream({"width": 1080, "height": 1920}) == (1080, 1920),
       "Fit Club, ffprobe 4.3: HEVC 1080x1920 sem rotacao")
    ok(dims_do_stream({"width": 1920, "height": 1080, "tags": {"rotate": "90"}})
       == (1080, 1920), "rotacao na tag `rotate` (formato do 4.x)")
    ok(dims_do_stream({"width": 1920, "height": 1080,
                       "side_data_list": [{"rotation": -90}]}) == (1080, 1920),
       "rotacao em side_data_list (formato do 5+)")
    ok(dims_do_stream({"width": 1920, "height": 1080, "tags": {"rotate": "180"}})
       == (1920, 1080), "180 graus nao troca os lados")
    ok(dims_do_stream({"width": 0, "height": 0}) is None and dims_do_stream(None) is None,
       "stream sem dimensao: None, nunca excecao")


# ------------------------------------------------------------- detector
def teste_orientacao():
    print("7. detector escolhe a entrada pela orientacao do quadro")
    d = motor.Detector.__new__(motor.Detector)
    d.variantes = {"paisagem": {}, "retrato": {}}
    ok(d.orientacao(704, 396) == "retrato", "quadro em pe -> variante em pe")
    ok(d.orientacao(360, 640) == "paisagem", "quadro deitado -> variante deitada")
    d.variantes = {"paisagem": {}}
    ok(d.orientacao(704, 396) == "paisagem",
       "sem o modelo em pe, cai na deitada (comportamento anterior)")


if __name__ == "__main__":
    for f in (teste_url, teste_ctf, teste_sem_substream, teste_paisagem,
              teste_nunca_aumenta, teste_ffprobe, teste_orientacao):
        f()
    print("\ntodos passaram")
