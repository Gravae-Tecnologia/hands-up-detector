"""Configuracao persistente do serviço, com switches por quadra e por camera.

Fica em /etc/gravae/hands-up.json para sobreviver a reinstalacao do codigo -
o mesmo lugar onde ja mora o `device.json` que o agente escreve.

DOIS NIVEIS DE CHAVE, E ELES NAO SAO REDUNDANTES
    Uma arena tem quadras e cada quadra tem uma ou duas cameras. O nome do
    monitor no Shinobi ja carrega os dois: `campo01_camera02` e a camera 02 da
    quadra `campo01`. Desligar a QUADRA desliga tudo dela de uma vez (o caso
    comum: quadra em reforma, ou fora do horario). Desligar uma CAMERA sozinha
    e para o caso especifico de um angulo ruim ou defeito.

    Uma camera so processa se a quadra dela E ela mesma estiverem ligadas.

TUDO DESLIGADO POR PADRAO
    Uma atualizacao que chega em todas as Raspberries do parque nao pode
    comecar a consumir CPU e banda sozinha. O operador liga pelo OPS quando
    quiser, arena por arena.
"""
from __future__ import annotations

import json
import os
import threading

PADRAO = "/etc/gravae/hands-up.json"


class Config:
    def __init__(self, caminho=None):
        self.caminho = caminho or os.environ.get("HANDS_UP_CONFIG", PADRAO)
        self.lock = threading.Lock()
        self.d = {
            "ativo": False,
            "nuvem": "",
            "webhook": "",
            "fps": 1.0,
            "qualidade": 75,
            "largura": 640,
            "altura": 400,
            "quadras": {},        # {"campo01": true}
            "cameras": {},        # {"campo01_camera01": true}
        }
        self.carrega()

    def carrega(self):
        try:
            with open(self.caminho, encoding="utf-8") as f:
                self.d.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"config invalida ({e}); usando padroes", flush=True)
        return self.d

    def salva(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.caminho), exist_ok=True)
            tmp = self.caminho + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.d, f, indent=2, ensure_ascii=False)
            # troca atomica: um corte de energia no meio da escrita nao pode
            # deixar a Raspberry com um json truncado e o servico sem subir
            os.replace(tmp, self.caminho)

    @staticmethod
    def quadra_de(mid):
        """`campo01_camera02` -> `campo01`. E a convencao de nome que o
        arena_agent usa para mapear GPIO->monitor, entao ja e garantida."""
        return mid.split("_")[0] if "_" in mid else mid

    def ligada(self, mid):
        if not self.d.get("ativo"):
            return False
        q = self.quadra_de(mid)
        return bool(self.d["quadras"].get(q) and self.d["cameras"].get(mid))

    def define(self, *, ativo=None, quadra=None, camera=None, valor=None,
               **resto):
        with self.lock:
            if ativo is not None:
                self.d["ativo"] = bool(ativo)
            if quadra is not None:
                self.d["quadras"][quadra] = bool(valor)
                # Ligar a quadra LIGA as cameras dela. Nao e `setdefault`: o
                # `sincroniza` ja criou todas como False, entao setdefault nao
                # faria nada e ligar a quadra nao teria efeito visivel.
                # Desligar uma camera especifica depois continua valendo - e
                # esse o refinamento que o segundo nivel existe para permitir.
                if valor:
                    for m in list(self.d["cameras"]):
                        if self.quadra_de(m) == quadra:
                            self.d["cameras"][m] = True
            if camera is not None:
                self.d["cameras"][camera] = bool(valor)
            for k, v in resto.items():
                if k in self.d and v is not None:
                    self.d[k] = v
        self.salva()
        return self.d

    def sincroniza(self, mids):
        """Cria as entradas que faltam (camera nova na arena) sem apagar as
        existentes. Desligadas por padrao."""
        mudou = False
        with self.lock:
            for m in mids:
                if m not in self.d["cameras"]:
                    self.d["cameras"][m] = False
                    mudou = True
                q = self.quadra_de(m)
                if q not in self.d["quadras"]:
                    self.d["quadras"][q] = False
                    mudou = True
        if mudou:
            self.salva()
        return self.d
