"""Captura evidencias de gesto e serve a tela de revisao humana.

POR QUE ISTO EXISTE
    O criterio do gesto nunca foi validado. O erro medio de punho e 0,412
    (rtmpose-m) larguras de ombro contra uma margem de decisao de 0,35 - ou
    seja, o modelo erra mais que a propria distancia que decide. E nao havia
    como medir, porque nos videos de bancada a taxa-base de gesto era ~0,5% e
    o COCO nao transfere para o dominio (AUC 0,64 na quadra contra 0,82 la).

    Cada deteccao em producao vira aqui um exemplo rotulado. Depois de algumas
    partidas isso e o conjunto de validacao que faltava - e sai de graca.

CAPTURA TAMBEM OS "QUASE"
    Guardar so o que passou do limiar mede precisao, nunca recall: falso
    NEGATIVO e invisivel por definicao. Por isso a faixa 0,15..0,35 tambem e
    guardada. Se na revisao um "quase" for marcado como gesto de verdade, isso
    e um falso negativo - e a evidencia de que 0,35 esta alto demais.

    O `margem` de cada evidencia permite recalcular precisao e recall para
    QUALQUER limiar depois, sem recapturar nada.

DISCO E CARTAO SD
    Cada evidencia sao ~25 KB. O teto de 400 mantem tudo abaixo de 10 MB e a
    rotacao apaga a mais antiga - cartao SD nao gosta de escrita infinita.
"""
from __future__ import annotations

import json
import os
import threading
import time

import cv2

LIMIAR = 0.35          # o mesmo de motor.gesto_bracos
QUASE = 0.15           # abaixo disto nem vale guardar
TETO = 400


class Revisao:
    def __init__(self, pasta):
        self.pasta = pasta
        self.rotulos_p = os.path.join(pasta, "rotulos.jsonl")
        os.makedirs(pasta, exist_ok=True)
        self.lock = threading.Lock()
        self.itens = []          # mais recente primeiro
        self.rotulos = {}
        self._carrega()

    def _carrega(self):
        for n in sorted(os.listdir(self.pasta)):
            if n.endswith(".json") and n != "rotulos.jsonl":
                try:
                    self.itens.append(json.load(
                        open(os.path.join(self.pasta, n), encoding="utf-8")))
                except Exception:
                    pass
        self.itens.sort(key=lambda x: -x.get("t", 0))
        try:
            for linha in open(self.rotulos_p, encoding="utf-8"):
                r = json.loads(linha)
                self.rotulos[r["id"]] = r["rotulo"]
        except FileNotFoundError:
            pass

    @staticmethod
    def _recorta(img, caixa, largura=320):
        """Recorte da pessoa, ampliado.

        O quadro inteiro e 640x400 e a pessoa ocupa ~40x90 px - nessa escala
        nao da para reconhecer quem e, que e justamente o ponto do historico.
        A folga de 40% inclui os bracos levantados, que saem da caixa do
        detector, e o INTER_CUBIC vale a pena aqui: e uma imagem por gesto,
        nao 1 fps.
        """
        x1, y1, x2, y2 = [float(v) for v in caixa]
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        w, h = (x2 - x1) * 1.4, (y2 - y1) * 1.4
        lado = max(w, h * 0.75)
        x1, x2 = int(cx - lado / 2), int(cx + lado / 2)
        y1, y2 = int(cy - lado * 0.75 / 2), int(cy + lado * 0.75 / 2)
        H, W = img.shape[:2]
        pad = max(0, -x1, -y1, x2 - W, y2 - H)
        if pad:
            img = cv2.copyMakeBorder(img, pad, pad, pad, pad,
                                     cv2.BORDER_CONSTANT, value=(20, 22, 28))
            x1, y1, x2, y2 = x1 + pad, y1 + pad, x2 + pad, y2 + pad
        rec = img[y1:y2, x1:x2]
        if rec.size == 0:
            return None
        esc = largura / max(rec.shape[1], 1)
        return cv2.resize(rec, (largura, max(int(rec.shape[0] * esc), 1)),
                          interpolation=cv2.INTER_CUBIC)

    def guarda(self, img, cam, margem, pessoas, latencia_ms, extra=None,
               caixa=None, img_limpo=None):
        """Salva tres coisas por evidencia:

          <id>.jpg     quadro inteiro COM esqueleto  - contexto da quadra
          <id>_p.jpg   recorte da pessoa COM esqueleto - valida o criterio
          <id>_r.jpg   recorte da pessoa SEM esqueleto - reconhece quem e

        O terceiro existe porque o esqueleto desenhado cobre o rosto, e o
        historico serve para identificar a pessoa, nao so para conferir o
        modelo.
        """
        ident = f"{int(time.time() * 1000)}_{cam}"
        ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
        if not ok:
            return None
        with self.lock:
            open(os.path.join(self.pasta, ident + ".jpg"), "wb").write(enc.tobytes())
            if caixa is not None:
                for origem, sufixo in ((img, "_p"), (img_limpo, "_r")):
                    if origem is None:
                        continue
                    rec = self._recorta(origem, caixa)
                    if rec is None:
                        continue
                    ok2, e2 = cv2.imencode(".jpg", rec,
                                           [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                    if ok2:
                        open(os.path.join(self.pasta, ident + sufixo + ".jpg"),
                             "wb").write(e2.tobytes())
            d = {
                "id": ident, "t": time.time(),
                "hora": time.strftime("%d/%m %H:%M:%S"),
                "cam": cam,
                "margem": round(float(margem), 3),
                "gesto": bool(margem >= LIMIAR),
                "pessoas": pessoas,
                "latencia_ms": round(latencia_ms, 1),
                "tem_recorte": caixa is not None,
                "tem_rosto": caixa is not None and img_limpo is not None,
                **(extra or {}),
            }
            json.dump(d, open(os.path.join(self.pasta, ident + ".json"), "w",
                              encoding="utf-8"))
            self.itens.insert(0, d)
            self._rotaciona()
        return ident

    def _rotaciona(self):
        while len(self.itens) > TETO:
            velho = self.itens.pop()
            for ext in (".jpg", "_p.jpg", "_r.jpg", ".json"):
                try:
                    os.remove(os.path.join(self.pasta, velho["id"] + ext))
                except OSError:
                    pass

    def rotula(self, ident, rotulo):
        """rotulo: 'sim' (era gesto) | 'nao' (nao era) | '' (desfaz)."""
        with self.lock:
            if rotulo:
                self.rotulos[ident] = rotulo
            else:
                self.rotulos.pop(ident, None)
            with open(self.rotulos_p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": ident, "rotulo": rotulo,
                                    "t": time.time()}) + "\n")
        return self.resumo()

    def resumo(self):
        """Precisao e recall no limiar atual, contando so o que foi rotulado.

        Um 'quase' (abaixo do limiar) marcado como gesto e falso NEGATIVO -
        e o unico jeito de enxergar recall sem anotar video inteiro.
        """
        vp = fp = fn = vn = 0
        for it in self.itens:
            r = self.rotulos.get(it["id"])
            if not r:
                continue
            era = (r == "sim")
            disse = it["gesto"]
            if disse and era:
                vp += 1
            elif disse and not era:
                fp += 1
            elif not disse and era:
                fn += 1
            else:
                vn += 1
        rec = vp / max(vp + fn, 1)
        pre = vp / max(vp + fp, 1)
        return {
            "capturadas": len(self.itens),
            "rotuladas": vp + fp + fn + vn,
            "vp": vp, "fp": fp, "fn": fn, "vn": vn,
            "precisao": round(pre * 100, 1),
            "recall": round(rec * 100, 1),
            "f1": round(200 * pre * rec / max(pre + rec, 1e-9), 1),
            "limiar": LIMIAR,
            "sugestao": self._sugere_limiar(),
        }

    def _sugere_limiar(self):
        """Varre limiares nos itens ja rotulados e devolve o de melhor F1.

        E o retorno concreto de rotular: em vez de discutir se 0,35 e o
        numero certo, ele sai do dado.
        """
        pts = [(it["margem"], self.rotulos[it["id"]] == "sim")
               for it in self.itens if it["id"] in self.rotulos]
        if len(pts) < 8:
            return None
        melhor = (0.0, LIMIAR)
        for t in [x / 100 for x in range(10, 71)]:
            vp = sum(1 for m, e in pts if m >= t and e)
            fp = sum(1 for m, e in pts if m >= t and not e)
            fn = sum(1 for m, e in pts if m < t and e)
            rec = vp / max(vp + fn, 1)
            pre = vp / max(vp + fp, 1)
            f1 = 2 * pre * rec / max(pre + rec, 1e-9)
            if f1 > melhor[0]:
                melhor = (f1, t)
        return {"limiar": round(melhor[1], 2), "f1": round(melhor[0] * 100, 1)}
