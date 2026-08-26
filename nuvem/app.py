"""Endpoint de inferencia para Cloud Run: recebe um JPEG, devolve esqueletos.

ONNX PURO, SEM TORCH NEM ULTRALYTICS
    O mesmo `motor.py` que roda na Raspberry, so que com modelos maiores. Nao
    e economia estetica: com escala-a-zero o *cold start* entra no caminho da
    primeira deteccao, e uma imagem com torch passa de 3 GB e leva dezenas de
    segundos para subir. Assim a imagem fica em ~400 MB.

CPU, NAO GPU
    Medido em CPU x86: `yolo11s` custa 68 ms e `rtmpose-m` 17,6 ms por pessoa.
    Com 5 pessoas da ~157 ms, folgado dentro do orcamento de 1 s por quadro.
    A GPU compraria 3 pontos de F1 (97,9 contra 94,7) por varias vezes o
    preco e um cold start muito pior.

MULTI-TENANT DESDE JA
    Cada requisicao carrega `arena` e `camera` nos cabecalhos, e eles voltam
    na resposta e vao para o log estruturado. Sem isso, no dia em que houver
    varias arenas nao da para saber de quem e a latencia ruim.

  GET  /            saude e modelo carregado
  POST /            corpo = JPEG; devolve caixas, keypoints e gesto
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

import motor


class H(BaseHTTPRequestHandler):
    pipe = None
    confs = {}
    # keep-alive: sem isto cada quadro paga TCP handshake, que a partir de uma
    # arena passa de 200 ms - mais caro que a propria inferencia
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _resp(self, obj, cod=200):
        b = json.dumps(obj).encode()
        self.send_response(cod)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        return self._resp({"ok": True, "modelo": H.pipe.nome,
                           "revisao": os.environ.get("K_REVISION", "local")})

    def do_POST(self):
        t0 = time.perf_counter()
        arena = self.headers.get("X-Arena", "?")
        camera = self.headers.get("X-Camera", "?")
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return self._resp({"erro": "corpo vazio"}, 400)
        bruto = self.rfile.read(n)

        a = time.perf_counter()
        img = cv2.imdecode(np.frombuffer(bruto, np.uint8), cv2.IMREAD_COLOR)
        ms_decode = (time.perf_counter() - a) * 1e3
        if img is None:
            return self._resp({"erro": "jpeg invalido"}, 400)

        a = time.perf_counter()
        caixas, kpts = H.pipe(img)
        ms_infer = (time.perf_counter() - a) * 1e3

        # o SimCC do RTMPose nao sai em [0,1]: normaliza pelo p90 do proprio
        # modelo. A escala e POR CAMERA - cada enquadramento tem sua propria
        # distribuicao, e misturar arenas distorceria o limiar de todas.
        chave = f"{arena}/{camera}"
        buf = H.confs.setdefault(chave, [])
        for k in kpts:
            buf.extend(k[:, 2].tolist())
        del buf[:-4000]
        esc = float(np.percentile(buf, 90)) if len(buf) > 200 else 1.0
        gestos = [motor.gesto_bracos(k, escala_conf=esc) for k in kpts]

        saida = {
            "arena": arena, "camera": camera,
            "pessoas": len(kpts), "gestos": int(sum(gestos)),
            "quem": [i for i, g in enumerate(gestos) if g],
            "caixas": [[round(float(v), 1) for v in c] for c in caixas],
            "kpts": [[[round(float(x), 1), round(float(y), 1), round(float(c), 3)]
                      for x, y, c in k] for k in kpts],
            "modelo": H.pipe.nome,
            "bytes_recebidos": n,
            "ms_decode": round(ms_decode, 1),
            "ms_infer": round(ms_infer, 1),
            "ms_det": round(H.pipe.ms_det, 1),
            "ms_pose": round(H.pipe.ms_pose, 1),
            "ms_servidor": round((time.perf_counter() - t0) * 1e3, 1),
        }
        # log estruturado: o Cloud Logging indexa o JSON e da para filtrar por
        # arena sem precisar de outra ferramenta
        print(json.dumps({"severity": "INFO", **saida, "kpts": None,
                          "caixas": None}), flush=True)
        return self._resp(saida)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--det", default=os.environ.get("DET", "yolo11s"))
    ap.add_argument("--pose", default=os.environ.get("POSE", "rtmpose-m"))
    ap.add_argument("--threads", type=int,
                    default=int(os.environ.get("THREADS", "0")) or (os.cpu_count() or 2))
    args = ap.parse_args()

    t0 = time.perf_counter()
    H.pipe = motor.Pipeline(args.det, args.pose, args.threads)
    print(json.dumps({"severity": "INFO",
                      "msg": "modelos carregados",
                      "modelo": H.pipe.nome,
                      "threads": args.threads,
                      "carga_s": round(time.perf_counter() - t0, 2)}), flush=True)
    print(json.dumps({"severity": "INFO",
                      "msg": f"ouvindo em :{args.porta}"}), flush=True)
    sys.stdout.flush()
    ThreadingHTTPServer(("0.0.0.0", args.porta), H).serve_forever()


if __name__ == "__main__":
    main()
