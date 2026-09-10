"""Exporta o YOLO para ONNX nas duas orientacoes que o `motor.Detector` usa.

Roda na maquina de desenvolvimento: precisa de `ultralytics`, que NAO vai
para a imagem da nuvem nem para a Pi (sao ~3 GB de pilha de treino).

    python exportar_detector.py                       # yolo11s -> nuvem/modelos
    python exportar_detector.py --modelo yolo11n --curto 256 --longo 416 \\
        --destino ../rasp/modelos                     # modo local da Pi

Gera, com o padrao de nome que o motor ja usa (`<modelo>_<altura>x<largura>`):

    yolo11s_416x640.onnx   deitado  - camera 16:9
    yolo11s_640x416.onnx   em pe    - camera em modo "story", 9:16

Saida crua (1, 4+80, A), SEM NMS embutido: o `motor.Detector` faz o NMS e le
a classe 0 como pessoa. Exportar com `nms=True` mudaria o formato da saida e
quebraria o decode.

Por que existir: ate aqui o `yolo11s_416x640.onnx` da imagem da nuvem era
exportado a mao e ninguem sabia com que parametros. Exportar os dois pelo
mesmo script garante que a variante em pe e a deitada so diferem na forma.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil

DIR = os.path.dirname(os.path.abspath(__file__))


def exporta(modelo, altura, largura, destino):
    from ultralytics import YOLO

    saida = YOLO(f"{modelo}.pt").export(format="onnx", imgsz=(altura, largura),
                                        opset=17, simplify=True, dynamic=False)
    final = os.path.join(destino, f"{modelo}_{altura}x{largura}.onnx")
    shutil.move(saida, final)
    return final


def confere(caminho, altura, largura):
    """A forma de entrada e de saida sao as que o motor espera?"""
    import onnxruntime as ort

    s = ort.InferenceSession(caminho, providers=["CPUExecutionProvider"])
    ent = s.get_inputs()[0].shape
    sai = s.get_outputs()[0].shape
    assert list(ent) == [1, 3, altura, largura], f"entrada {ent}"
    assert sai[1] == 84, f"saida {sai}: esperava (1, 84, A) sem NMS"
    return ent, sai


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--modelo", default="yolo11s")
    ap.add_argument("--curto", type=int, default=416, help="lado curto da entrada")
    ap.add_argument("--longo", type=int, default=640, help="lado longo da entrada")
    ap.add_argument("--destino", default=os.path.join(DIR, "modelos"))
    a = ap.parse_args()
    os.makedirs(a.destino, exist_ok=True)

    for rotulo, (h, w) in (("deitado", (a.curto, a.longo)),
                           ("em pe", (a.longo, a.curto))):
        arq = exporta(a.modelo, h, w, a.destino)
        ent, sai = confere(arq, h, w)
        sha = hashlib.sha256(open(arq, "rb").read()).hexdigest()[:16]
        print(f"{rotulo:<8} {os.path.basename(arq):<26} entrada {ent} "
              f"saida {sai}  sha256 {sha}...")


if __name__ == "__main__":
    main()
