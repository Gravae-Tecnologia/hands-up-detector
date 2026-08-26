"""Endpoint de inferencia remota: recebe um JPEG, devolve esqueletos.

Roda na maquina com GPU. A Pi manda o quadro e recebe as caixas, keypoints e
o veredito do gesto - a ideia e descobrir se vale trocar 500 ms de CPU na Pi
por (encode + subida + inferencia + volta).

MEDE CADA PERNA SEPARADAMENTE
    Um numero unico de RTT nao diz onde esta o custo, e as pernas tem donos
    diferentes: encode e da Pi, subida e da internet da arena, inferencia e
    do servidor. So separando da para saber o que otimizar - e se o gargalo
    for a subida, trocar de GPU nao muda nada.

    O servidor devolve `ms_infer` e `bytes_recebidos` no proprio JSON; o
    cliente calcula o resto por diferenca.

MODELOS GRANDES DE PROPOSITO
    Se a nuvem for rodar o mesmo modelo pequeno da Pi, nao ha por que sair da
    Pi. O ganho de ir para a nuvem e poder usar o `yolo11x` + `rtmpose-x`, que
    na bancada deram F1 97,9 contra 90,1 do `yolo11n-256`.

  python servidor.py --porta 8091
  python servidor.py --det yolo11n --pose rtmpose-s   # paridade com a Pi
"""
from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

DIR = os.path.dirname(os.path.abspath(__file__))
MODELOS = os.environ.get("MODELOS", os.path.join(DIR, "modelos"))

SHO, ELB, WRI = (5, 6), (7, 8), (9, 10)
GESTO_KPTS = [5, 6, 7, 8, 9, 10]


class Motor:
    def __init__(self, det, pose, imgsz, device):
        import torch
        # Windows nao acha as DLLs de CUDA do onnxruntime sozinho; no Linux da
        # VM o loader resolve pelo rpath e a chamada nao existe.
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(
                os.path.join(os.path.dirname(torch.__file__), "lib"))
        from rtmlib import RTMPose
        from ultralytics import YOLO

        self.det = YOLO(os.path.join(MODELOS, f"{det}.pt"))
        self.imgsz = imgsz
        self.dev = "0" if device == "cuda" else "cpu"
        arq = {"rtmpose-x": "rtmpose-x.onnx",
               "rtmpose-m": "rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504.onnx",
               "rtmpose-s": "rtmpose-s_simcc-body7_pt-body7_420e-256x192-acd4a1ef_20230504.onnx"}[pose]
        cam = (288, 384) if pose == "rtmpose-x" else (192, 256)
        caminho = os.path.join(MODELOS, arq)
        if not os.path.exists(caminho):
            raise SystemExit(f"modelo ausente: {caminho} -- "
                             f"rode ./baixar_modelos.sh")
        self.pose = RTMPose(caminho, model_input_size=cam,
                            backend="onnxruntime", device=device)
        self.nome = f"{det}+{pose}"
        self.prov = self.pose.session.get_providers()[0]

    def __call__(self, bgr):
        r = self.det.predict(bgr, imgsz=self.imgsz, conf=0.25, iou=0.7,
                             classes=[0], device=self.dev, verbose=False)[0]
        if r.boxes is None or not len(r.boxes):
            return [], []
        caixas = r.boxes.xyxy.cpu().numpy()
        kk, ks = self.pose(bgr, bboxes=caixas)
        kpts = [np.hstack([np.asarray(kk[j], np.float32),
                           np.asarray(ks[j], np.float32).reshape(-1, 1)])
                for j in range(len(caixas))]
        return caixas, kpts


def gesto(k, escala, margem=0.35, conf_min=0.3):
    c = k[:, 2] / max(escala, 1e-6)
    if min(c[GESTO_KPTS]) < conf_min:
        return False
    larg = float(np.linalg.norm(k[5, :2] - k[6, :2]))
    if larg < 1.0:
        return False
    m = margem * larg
    return bool(k[9, 1] < k[5, 1] - m and k[10, 1] < k[6, 1] - m and
                k[7, 1] < k[5, 1] and k[8, 1] < k[6, 1])


class H(BaseHTTPRequestHandler):
    motor = None
    confs = []
    protocol_version = "HTTP/1.1"     # keep-alive: sem isto cada quadro paga
    #                                   um TCP handshake novo, que na arena
    #                                   pesa mais que a propria inferencia

    def log_message(self, *a):
        pass

    def do_POST(self):
        t_recebido = time.perf_counter()
        n = int(self.headers.get("Content-Length", 0))
        bruto = self.rfile.read(n)

        a = time.perf_counter()
        img = cv2.imdecode(np.frombuffer(bruto, np.uint8), cv2.IMREAD_COLOR)
        ms_decode = (time.perf_counter() - a) * 1e3
        if img is None:
            return self._resp({"erro": "jpeg invalido"}, 400)

        a = time.perf_counter()
        caixas, kpts = H.motor(img)
        ms_infer = (time.perf_counter() - a) * 1e3

        for k in kpts:
            H.confs.extend(k[:, 2].tolist())
        del H.confs[:-4000]
        esc = float(np.percentile(H.confs, 90)) if len(H.confs) > 200 else 1.0
        gestos = [gesto(k, esc) for k in kpts]

        return self._resp({
            "pessoas": len(kpts),
            "gestos": int(sum(gestos)),
            "caixas": [[round(float(v), 1) for v in c] for c in caixas],
            "kpts": [[[round(float(x), 1), round(float(y), 1), round(float(c), 3)]
                      for x, y, c in k] for k in kpts],
            "modelo": H.motor.nome,
            "bytes_recebidos": n,
            "ms_decode": round(ms_decode, 1),
            "ms_infer": round(ms_infer, 1),
            "ms_servidor": round((time.perf_counter() - t_recebido) * 1e3, 1),
        })

    def do_GET(self):
        return self._resp({"ok": True, "modelo": H.motor.nome,
                           "provider": H.motor.prov})

    def _resp(self, obj, cod=200):
        b = json.dumps(obj).encode()
        self.send_response(cod)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=8091)
    ap.add_argument("--det", default="yolo11x")
    ap.add_argument("--pose", default="rtmpose-x")
    ap.add_argument("--imgsz", type=int, default=1280)
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args()

    H.motor = Motor(args.det, args.pose, args.imgsz, args.device)
    print(f"modelo: {H.motor.nome} | provider {H.motor.prov} | "
          f"imgsz {args.imgsz}", flush=True)
    print(f"endpoint em http://0.0.0.0:{args.porta}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.porta), H).serve_forever()


if __name__ == "__main__":
    main()
