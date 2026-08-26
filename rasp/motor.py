"""Detector + pose em ONNX puro, sem rtmlib, sem torch, sem ultralytics.

Dependencias: onnxruntime, numpy, cv2. So isso. As tres coisas que as
bibliotecas faziam por nos estao reimplementadas aqui porque sao ~80 linhas
e valiam menos que 2 GB de dependencia numa Raspberry:

  1. decode do YOLO11  - saida (1, 4+nc, A): cxcywh em pixels da ENTRADA,
     classes ja com sigmoid, sem objectness. Precisa de NMS proprio.
  2. pre do RTMPose    - bbox -> centro/escala com folga 1.25 -> warpAffine
     direto do frame para 192x256. NAO e crop+resize: o warp corrige a
     proporcao e reamostra os pixels originais uma vez so. A folga de 25% e
     o que faz punho acima da cabeca caber na entrada da rede - sem ela o
     gesto fica fora do quadro que a pose ve.
  3. pos do RTMPose    - SimCC: dois vetores (x com 384 bins, y com 512) por
     articulacao; o argmax de cada um da a coordenada em resolucao sub-pixel.

Os limiares por modelo vem calibrados da pesquisa (tabela_final_21.csv); um
limiar unico para todos favoreceria os modelos mais confiantes.
"""
from __future__ import annotations

import os
import time

import cv2
import numpy as np

# onnxruntime NAO e importado aqui de proposito. Em modo nuvem a Pi so
# desenha o esqueleto que o servidor devolveu e aplica o criterio do gesto -
# nada disso precisa de ONNX. Deixando o import preguiçoso, a instalacao numa
# arena que usa a nuvem dispensa o pacote (73 MB) e os modelos (51 MB), e a
# `desenha`/`gesto_bracos` continuam disponiveis.

DIR = os.path.dirname(os.path.abspath(__file__))
MODELOS = os.environ.get("MODELOS", os.path.join(DIR, "modelos"))

DETECTORES = {
    "yolo11n-256": dict(arq="yolo11n_256x416.onnx", entrada=(256, 416), thr=0.25),
    "yolo11n-224": dict(arq="yolo11n_224x352.onnx", entrada=(224, 352), thr=0.25),
    "yolo11n-256-int8": dict(arq="yolo11n_256x416_int8.onnx", entrada=(256, 416), thr=0.25),
}
POSES = {
    "rtmpose-s": dict(arq="rtmpose-s.onnx", entrada=(192, 256)),
    "rtmpose-s-int8": dict(arq="rtmpose-s_int8.onnx", entrada=(192, 256)),
    "rtmpose-m": dict(arq="rtmpose-m.onnx", entrada=(192, 256)),
}

# COCO-17
SHO, ELB, WRI = (5, 6), (7, 8), (9, 10)
TORNOZELOS = (15, 16)
ESQUELETO = [(0, 1), (0, 2), (1, 3), (2, 4), (0, 5), (0, 6), (5, 7), (7, 9),
             (6, 8), (8, 10), (5, 6), (5, 11), (6, 12), (11, 12), (11, 13),
             (13, 15), (12, 14), (14, 16)]
MEAN = np.array([123.675, 116.28, 103.53], np.float32)
STD = np.array([58.395, 57.12, 57.375], np.float32)


def sessao(caminho, threads):
    import onnxruntime as ort

    """2 threads e nao 4 de proposito: medido na Pi 4, 4 threads e 18% MAIS
    LENTO que 2 (472 ms contra 399). Sao 4 nucleos, mas o sistema usa parte
    deles e o custo de sincronizacao supera o ganho no 3o e 4o."""

    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(caminho, sess_options=so,
                                providers=["CPUExecutionProvider"])


def nms(caixas, scores, thr=0.45):
    if not len(caixas):
        return []
    x1, y1, x2, y2 = caixas[:, 0], caixas[:, 1], caixas[:, 2], caixas[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    ordem = scores.argsort()[::-1]
    fica = []
    while ordem.size:
        i = ordem[0]
        fica.append(i)
        if ordem.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[ordem[1:]])
        yy1 = np.maximum(y1[i], y1[ordem[1:]])
        xx2 = np.minimum(x2[i], x2[ordem[1:]])
        yy2 = np.minimum(y2[i], y2[ordem[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[ordem[1:]] - inter + 1e-9)
        ordem = ordem[1:][iou <= thr]
    return fica


class Detector:
    def __init__(self, nome, threads=2):
        cfg = DETECTORES[nome]
        self.h, self.w = cfg["entrada"]
        self.thr = cfg["thr"]
        self.s = sessao(os.path.join(MODELOS, cfg["arq"]), threads)
        self.ent = self.s.get_inputs()[0].name
        self.sai = [o.name for o in self.s.get_outputs()]

    def __call__(self, bgr):
        h, w = bgr.shape[:2]
        r = min(self.h / h, self.w / w)
        nw, nh = int(w * r), int(h * r)
        tela = np.full((self.h, self.w, 3), 114, np.uint8)
        tela[:nh, :nw] = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(tela, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None])

        pred = self.s.run(self.sai, {self.ent: blob})[0][0].T   # (A, 4+nc)
        conf = pred[:, 4]                                       # classe 0 = pessoa
        fica = conf > self.thr
        if not fica.any():
            return np.zeros((0, 4), np.float32), np.zeros((0,), np.float32)
        cx, cy, bw, bh = (pred[fica, 0], pred[fica, 1], pred[fica, 2], pred[fica, 3])
        caixas = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)
        c = conf[fica]
        k = nms(caixas, c)
        return caixas[k] / r, c[k]


class Pose:
    """RTMPose com pre e pos implementados aqui (o rtmlib so fazia isto)."""

    def __init__(self, nome, threads=2):
        cfg = POSES[nome]
        self.w, self.h = cfg["entrada"]
        self.s = sessao(os.path.join(MODELOS, cfg["arq"]), threads)
        self.ent = self.s.get_inputs()[0].name
        self.sai = [o.name for o in self.s.get_outputs()]

    def _afim(self, bgr, caixa):
        x1, y1, x2, y2 = caixa
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        bw, bh = (x2 - x1) * 1.25, (y2 - y1) * 1.25       # folga de 25%
        prop = self.w / self.h
        if bw > bh * prop:                                 # respeita a proporcao
            bh = bw / prop
        else:
            bw = bh * prop
        # warp direto do frame original: uma reamostragem so, sem distorcer
        orig = np.float32([[cx, cy], [cx, cy - bh / 2], [cx - bw / 2, cy]])
        dest = np.float32([[self.w / 2, self.h / 2], [self.w / 2, 0], [0, self.h / 2]])
        M = cv2.getAffineTransform(orig, dest)
        rec = cv2.warpAffine(bgr, M, (self.w, self.h), flags=cv2.INTER_LINEAR)
        return rec, (cx, cy), (bw, bh)

    def __call__(self, bgr, caixas):
        saida = []
        for c in caixas:
            rec, centro, escala = self._afim(bgr, c)
            blob = np.ascontiguousarray(
                ((rec.astype(np.float32) - MEAN) / STD).transpose(2, 0, 1)[None])
            sx, sy = self.s.run(self.sai, {self.ent: blob})
            # SimCC: argmax em cada eixo -> coordenada em resolucao sub-pixel
            ix, iy = sx[0].argmax(1), sy[0].argmax(1)
            vx = sx[0][np.arange(len(ix)), ix]
            vy = sy[0][np.arange(len(iy)), iy]
            conf = np.minimum(vx, vy)
            xy = np.stack([ix / 2.0, iy / 2.0], 1)          # simcc_split_ratio=2
            xy = xy / np.array([self.w, self.h]) * np.array(escala)
            xy = xy + np.array(centro) - np.array(escala) / 2
            saida.append(np.hstack([xy.astype(np.float32),
                                    conf.reshape(-1, 1).astype(np.float32)]))
        return saida


class Pipeline:
    def __init__(self, det="yolo11n-256", pose="rtmpose-s", threads=2,
                 max_pessoas=0):
        t0 = time.perf_counter()
        self.det = Detector(det, threads)
        self.pose = Pose(pose, threads)
        self.carga_s = time.perf_counter() - t0
        self.nome = f"{det}+{pose}"
        self.max_pessoas = max_pessoas
        self.ms_det = self.ms_pose = 0.0

    def __call__(self, bgr):
        a = time.perf_counter()
        caixas, scores = self.det(bgr)
        self.ms_det = (time.perf_counter() - a) * 1e3
        if not len(caixas):
            self.ms_pose = 0.0
            return [], []
        if self.max_pessoas and len(caixas) > self.max_pessoas:
            # os N mais PROXIMOS da camera (pe mais baixo na imagem), nao os
            # de maior score: a pose custa 112 ms por pessoa e quem interessa
            # sao os jogadores da frente.
            ordem = np.argsort(-caixas[:, 3])[:self.max_pessoas]
            caixas, scores = caixas[ordem], scores[ordem]
        b = time.perf_counter()
        kpts = self.pose(bgr, caixas)
        self.ms_pose = (time.perf_counter() - b) * 1e3
        return caixas, kpts


def gesto_margem(k, conf_min=0.3, escala_conf=1.0):
    """Quanto o gesto esta ACIMA ou ABAIXO do criterio, em larguras de ombro.

    O criterio original devolve sim/nao, o que joga fora a informacao mais
    util para calibrar: o quanto faltou. Aqui a saida e continua -

        >= 0,35  gesto (o limiar atual)
        0,15..0,35  quase: e o que revela falso NEGATIVO na revisao
        < 0,15   nao e gesto

    O valor e o MENOR dos dois lados, porque o criterio exige os dois bracos.
    Devolve None quando nem da para avaliar (confianca baixa ou ombros
    colados, que e o caso da pessoa de perfil).
    """
    c = k[:, 2] / max(escala_conf, 1e-6)
    if min(c[[5, 6, 7, 8, 9, 10]]) < conf_min:
        return None
    larg = float(np.linalg.norm(k[5, :2] - k[6, :2]))
    if larg < 1.0:
        return None
    # cotovelo acima do ombro e condicao dura: sem ela, aceno com a mao na
    # altura da cabeca passaria por braco levantado
    if not (k[7, 1] < k[5, 1] and k[8, 1] < k[6, 1]):
        return -1.0
    esq = (k[5, 1] - k[9, 1]) / larg
    dir = (k[6, 1] - k[10, 1]) / larg
    return float(min(esq, dir))


def gesto_bracos(k, margem=0.35, conf_min=0.3, escala_conf=1.0):
    """punho.y < ombro.y - margem*largura_ombros nos dois lados, e cotovelo
    acima do ombro. Exigir o cotovelo separa braco levantado de aceno com a
    mao na altura da cabeca. y cresce para baixo na imagem."""
    c = k[:, 2] / max(escala_conf, 1e-6)
    if min(c[[5, 6, 7, 8, 9, 10]]) < conf_min:
        return False
    larg = float(np.linalg.norm(k[5, :2] - k[6, :2]))
    if larg < 1.0:
        return False
    m = margem * larg
    return bool(k[9, 1] < k[5, 1] - m and k[10, 1] < k[6, 1] - m and
                k[7, 1] < k[5, 1] and k[8, 1] < k[6, 1])


def desenha(img, caixas, kpts, conf_min=0.3, escala_conf=1.0, gestos=None):
    for i, k in enumerate(kpts):
        ativo = gestos[i] if gestos else False
        cor = (60, 60, 240) if ativo else (80, 230, 80)
        x1, y1, x2, y2 = [int(v) for v in caixas[i]]
        cv2.rectangle(img, (x1, y1), (x2, y2), cor, 2 if ativo else 1)
        c = k[:, 2] / max(escala_conf, 1e-6)
        for a, b in ESQUELETO:
            if c[a] >= conf_min and c[b] >= conf_min:
                cv2.line(img, (int(k[a, 0]), int(k[a, 1])),
                         (int(k[b, 0]), int(k[b, 1])), cor, 2)
        for j in range(len(k)):
            if c[j] >= conf_min:
                cv2.circle(img, (int(k[j, 0]), int(k[j, 1])), 3, (255, 255, 255), -1)
        if ativo:
            cv2.putText(img, "BRACOS LEVANTADOS", (x1, max(y1 - 8, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 240), 2)
    return img
