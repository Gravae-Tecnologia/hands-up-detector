"""Testes do gatilho pela IA da camera. Sem camera de verdade e sem nuvem:
uma camera falsa em localhost fala o mesmo HTTP das Intelbras.

  python teste_gatilho.py

As linhas de evento sao as capturadas no Fit Club em 10/09/2026
(VIP-3430-D-IA, `eventManager.cgi?action=attach`).
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ia_camera as ia


def ok(cond, msg):
    print(f"  {'ok  ' if cond else 'FALHOU'} {msg}")
    if not cond:
        raise SystemExit(1)


def espera(cond, s=8.0):
    fim = time.time() + s
    while time.time() < fim:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------- linha
def teste_interpreta():
    print("1. linha do stream de eventos")
    ev = ia.interpreta("Code=SmartMotionHuman;action=Start;index=0;data={")
    ok(ev == {"codigo": "SmartMotionHuman", "acao": "Start", "indice": 0},
       "Start real do Fit Club, com o JSON de data= aberto")
    ok(ia.interpreta("Code=SmartMotionHuman;action=Stop;index=1")["acao"] == "Stop",
       "Stop sem data=, indice 1")
    ok(ia.interpreta('"LocaleTime":\t"2026-09-10 15:26:11",') is None,
       "linha do JSON que segue o evento e ignorada")
    ok(ia.interpreta("Heartbeat") is None, "batida nao e evento")
    ok(ia.interpreta("Code=;action=") is None, "linha truncada nao quebra")
    ok(ia.interpreta("Code=X;action=Pulse;index=abc")["indice"] == 0,
       "indice ilegivel vira 0")


# ------------------------------------------------------------- presenca
def teste_presenca():
    print("2. prazo de 10 min depois da ultima pessoa")
    t = 1000.0
    p = ia.Presenca(espera_s=600, carencia_s=30, agora=t)
    p.conectou(t + 1)
    ok(p.tem_gente(t + 599), "acabou de subir: prazo cheio (na duvida, tem gente)")
    ok(not p.tem_gente(t + 601), "10 min sem ninguem: pausa")

    p.evento("Start", 0, t + 700)
    ok(p.tem_gente(t + 700), "Start: gente")
    for k in range(1, 200):                       # jogo corrido de 1000 s
        p.batida(t + 700 + 5 * k)
    ok(p.tem_gente(t + 1690), "Start longo com batidas: nao vence no meio do jogo")
    p.evento("Stop", 0, t + 1700)
    ok(p.tem_gente(t + 2299), "depois do Stop, ainda dentro do prazo")
    ok(not p.tem_gente(t + 2301), "Stop + 10 min: pausa")

    p.viu(t + 2400, "detector")
    ok(p.tem_gente(t + 2999), "pessoa vista pelo NOSSO detector renova o prazo")
    est = p.estado(t + 2500)
    ok(est["ha_s"] == 100 and est["restante_s"] == 500,
       f"estado para o painel: pessoa ha {est['ha_s']} s, pausa em {est['restante_s']} s")
    ok(est["eventos"] == {"camera": 2, "detector": 1}, f"contagem por fonte {est['eventos']}")


def teste_presenca_conexao():
    print("3. conexao com a camera: na duvida, tem gente")
    t = 0.0
    p = ia.Presenca(espera_s=600, carencia_s=30, agora=t)
    p.conectou(t + 1)
    p.caiu(t + 1000)                               # ja pausado quando caiu
    ok(not p.cego(t + 1020) and not p.tem_gente(t + 1020),
       "queda curta: segue pausado, reconexao normal nao conta")
    ok(p.cego(t + 1031), "sem conexao > 30 s: cego")
    ok(ia.decide(True, "ia", {"suporta": True, "ligada": True}, p, t + 1031)
       == (True, "sem_sinal"), "cego: captura liga (sem_sinal)")
    p.conectou(t + 1100)
    ok(p.tem_gente(t + 1699), "voltou depois de cego: prazo cheio de novo")

    p2 = ia.Presenca(espera_s=600, carencia_s=30, agora=t)
    p2.conectou(t + 1)
    p2.caiu(t + 900)
    p2.conectou(t + 910)                           # queda de 10 s
    ok(not p2.tem_gente(t + 911), "queda de 10 s NAO renova o prazo "
       "(camera que derruba o stream nao pode segurar o hands-up ligado)")

    p3 = ia.Presenca(espera_s=600, carencia_s=30, agora=t)
    p3.conectou(t + 1)
    p3.evento("Start", 0, t + 800)
    p3.caiu(t + 900)
    ok(not p3.em_curso and p3.tem_gente(t + 1499),
       "caiu com gente em curso: Start nao fica preso, mas o prazo conta dali")
    p3.conectou(t + 905, em_curso={0})
    ok(p3.em_curso == {0}, "ao conectar, o estado atual vem da camera (getEventIndexes)")


def teste_decide():
    print("4. decisao por camera")
    t = 5000.0
    boa = {"suporta": True, "ligada": True}
    p = ia.Presenca(600, 30, agora=t - 700)
    p.conectou(t - 699)
    ok(ia.decide(False, "ia", boa, p, t) == (False, "desligada"), "chave desligada manda")
    ok(ia.decide(True, "manual", boa, p, t) == (True, "manual"), "manual: sempre ativa")
    ok(ia.decide(True, "ia", boa, p, t) == (False, "pausada"), "ia sem gente: pausa")
    p.evento("Start", 0, t)
    ok(ia.decide(True, "ia", boa, p, t) == (True, "gente"), "ia com gente: ativa")
    ok(ia.decide(True, "ia", None, None, t) == (True, "sem_ia"),
       "ainda nao sondou: ativa (comportamento de antes)")
    ok(ia.decide(True, "ia", {"suporta": False, "ligada": None}, None, t)
       == (True, "sem_ia"), "camera sem IA: nunca pausa")
    ok(ia.decide(True, "ia", {"suporta": True, "ligada": False}, None, t)
       == (True, "sem_ia"), "IA desligada na camera: nunca pausa (nada chegaria)")
    ok(ia.decide(True, "ia", {"suporta": None, "ligada": None}, None, t)
       == (True, "sem_ia"), "camera nao respondeu a sonda: nunca pausa")


# ------------------------------------------------------------- camera falsa
class Cam(BaseHTTPRequestHandler):
    """Fala o HTTP de uma Intelbras VIP-3430-D-IA."""
    modelo = "VIP-3430-D-IA"
    eventos = ["VideoMotion", "SmartMotionHuman", "SmartMotionVehicle"]
    smd = {"Enable": "true", "ObjectTypes.Human": "true"}
    em_curso = False
    assinantes = []            # uma fila por conexao de attach aberta
    derruba = threading.Event()
    batida_s = 0.5
    attaches = 0
    status_tipo = 200

    def log_message(self, *a):
        pass

    @classmethod
    def emite(cls, acao):
        """Evento para TODAS as conexoes abertas, como a camera de verdade -
        uma fila compartilhada deixaria uma conexao velha roubar o evento."""
        for f in list(cls.assinantes):
            f.put(acao)

    def _texto(self, corpo, status=200):
        b = corpo.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        c = type(self)
        if "getDeviceType" in self.path:
            return self._texto(f"type={c.modelo}\r\n", c.status_tipo)
        if "getExposureEvents" in self.path:
            return self._texto("".join(f"events[{i}]={e}\r\n"
                                       for i, e in enumerate(c.eventos)))
        if "name=SmartMotionDetect" in self.path:
            return self._texto("".join(f"table.SmartMotionDetect[0].{k}={v}\r\n"
                                       for k, v in c.smd.items()))
        if "getEventIndexes" in self.path:
            return self._texto("channels[0]=0\r\n" if c.em_curso
                               else "Error:No Events\r\n")
        if "action=attach" in self.path:
            c.attaches += 1
            fila = queue.Queue()
            c.assinantes.append(fila)
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=myboundary")
            self.end_headers()
            prox = time.time()
            try:
                while not c.derruba.is_set():
                    try:
                        ev = fila.get(timeout=0.05)
                        corpo = (f"Code=SmartMotionHuman;action={ev};index=0;data={{\n"
                                 f'\t"LocaleTime":\t"2026-09-10 15:26:11",\n'
                                 f'\t"Name":\t"IPC"\n}}\r\n')
                    except queue.Empty:
                        if time.time() < prox:
                            continue
                        prox = time.time() + c.batida_s
                        corpo = "Heartbeat\r\n"
                    self.wfile.write(
                        (f"--myboundary\r\nContent-Type: text/plain\r\n"
                         f"Content-Length: {len(corpo)}\r\n\r\n{corpo}").encode())
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                c.assinantes.remove(fila)
            return
        self._texto("", 404)


def sobe_camera():
    s = ThreadingHTTPServer(("127.0.0.1", 0), Cam)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, s.server_address[1]


def teste_sonda(porta):
    print("5. sonda: a Pi descobre se a camera tem IA")
    r = ia.sonda("127.0.0.1", "u", "p", porta=porta)
    ok(r["suporta"] is True and r["ligada"] is True and ia.usavel(r),
       f"VIP-3430-D-IA com humano ligado: usavel ({r['modelo']})")
    Cam.smd = {"Enable": "true", "ObjectTypes.Human": "false"}
    r = ia.sonda("127.0.0.1", "u", "p", porta=porta)
    ok(r["suporta"] is True and r["ligada"] is False and not ia.usavel(r),
       f"humano desligado na camera: nao usavel - '{r['motivo'][:40]}...'")
    Cam.smd = {"Enable": "true", "ObjectTypes.Human": "true"}
    Cam.eventos = ["VideoMotion", "VideoLoss"]
    r = ia.sonda("127.0.0.1", "u", "p", porta=porta)
    ok(r["suporta"] is False, "camera sem SmartMotionHuman: sem IA")
    Cam.eventos = ["VideoMotion", "SmartMotionHuman"]
    Cam.status_tipo = 404
    r = ia.sonda("127.0.0.1", "u", "p", porta=porta)
    ok(r["suporta"] is False and "fabricante" in r["motivo"],
       "sem a API Intelbras/Dahua (outro fabricante): sem IA")
    Cam.status_tipo = 200
    with socket.socket() as s:                    # porta sem ninguem
        s.bind(("127.0.0.1", 0))
        livre = s.getsockname()[1]
    r = ia.sonda("127.0.0.1", "u", "p", porta=livre, timeout=2)
    ok(r["suporta"] is None and "nao respondeu" in r["motivo"],
       "camera fora do ar: nao sei (e vai sondar de novo em 5 min)")


def teste_vigia(porta):
    print("6. vigia: conexao de eventos aberta com a camera")
    Cam.em_curso = True
    p = ia.Presenca(espera_s=600, carencia_s=30)
    v = ia.Vigia("cam", "127.0.0.1", "u", "p", p, porta=porta).inicia()
    ok(espera(lambda: p.conectado), "conectou no attach")
    ok(p.em_curso == {0}, "estado atual lido na conexao: tinha gente parada")
    Cam.emite("Stop")
    ok(espera(lambda: not p.em_curso), "Stop chegou pelo stream")
    Cam.emite("Start")
    ok(espera(lambda: p.em_curso == {0}), "Start chegou pelo stream")
    n = p.contagem.get("camera", 0)
    ok(n == 2, f"eventos contados: {n}")
    antes = Cam.attaches
    Cam.em_curso = False
    Cam.derruba.set()
    ok(espera(lambda: not p.conectado, 5), "camera derrubou o stream: percebeu")
    ok(not p.em_curso, "e nao ficou Start preso")
    Cam.derruba.clear()
    ok(espera(lambda: p.conectado and Cam.attaches > antes, 12),
       f"reconectou sozinho ({v.conexoes} conexoes)")
    v.para()
    ok(espera(lambda: not v._thread.is_alive(), 8), "para() encerra a thread")


# ------------------------------------------------------------- servico
def teste_servico(porta):
    print("7. servico: aplica_config liga e pausa a captura sozinho")
    import servico as S
    S.Camera.foto = lambda self, *a, **k: None       # sem ffmpeg
    S.Camera.liga = lambda self: setattr(self, "ativa", True)
    S.Camera.desliga = lambda self: setattr(self, "ativa", False)
    pasta = tempfile.mkdtemp()
    S.H.conf = S.cfgmod.Config(os.path.join(pasta, "hands-up.json"))
    S.H.cfg = {"dur_gesto": 2.0}
    S.H.registro = None
    cam = {"mid": "quadra01_camera01", "nome": "q1", "res": "", "rtsp": "",
           "host": "127.0.0.1", "usuario": "u", "senha": "p"}
    orig_sonda = ia.sonda
    ia.sonda = lambda h, u, s, porta_=None, **k: orig_sonda(h, u, s, porta=porta)
    orig_vigia = ia.Vigia
    ia.Vigia = lambda *a, **k: orig_vigia(*a, porta=porta, **k)
    try:
        c = S.Camera(cam)
        S.H.cams = {c.mid: c}
        S.H.conf.sincroniza([c.mid])
        ok(espera(lambda: c.ia is not None), "sondou a camera no arranque")
        ok(S.aplica_config() == [] and c.modo == "desligada",
           "tudo desligado por padrao (invariante 3)")
        S.H.conf.define(ativo=True)
        S.H.conf.define(quadra="quadra01", valor=True)
        ok(S.aplica_config() == [c.mid] and c.modo == "manual",
           "ligou no OPS, gatilho manual: captura")
        ok(c.vigia is None, "manual nao abre conexao de eventos")

        S.H.conf.define(gatilho="ia")
        S.aplica_config()
        ok(c.vigia is not None and c.modo == "gente" and c.ativa,
           "gatilho ia: abriu a conexao e segue ativa no prazo inicial")
        ok(espera(lambda: c.presenca.conectado), "conexao de eventos no ar")
        c.presenca.ultimo -= 601                      # 10 min sem ninguem
        S.aplica_config()
        ok(c.modo == "pausada" and not c.ativa, "10 min sem gente: PAUSOU a captura")
        Cam.emite("Start")
        ok(espera(lambda: c.presenca.em_curso), "camera avisou gente")
        S.aplica_config()
        ok(c.modo == "gente" and c.ativa, "e a captura VOLTOU sozinha")

        cfg = json.dumps(S.H.conf.d)
        ok('"gatilho": "ia"' in cfg and "senha" not in cfg,
           "gatilho persistido; credencial da camera nunca vai para a config")
        r = c.resumo_gatilho(time.time())
        ok(r["ia_usavel"] and r["presenca"]["em_curso"] and "senha" not in json.dumps(r),
           "resumo para o OPS sem credencial")

        S.H.conf.define(gatilho="manual")
        S.aplica_config()
        ok(c.vigia is None and c.modo == "manual" and c.ativa,
           "voltou para manual: fecha a conexao e fica sempre ativa")
        ok(S.cfgmod.Config.valida_gatilho({"gatilho": "sempre"}) is not None
           and S.cfgmod.Config.valida_gatilho({"espera_ia_s": 10}) is not None
           and S.cfgmod.Config.valida_gatilho({"gatilho": "ia", "espera_ia_s": 600}) is None,
           "valor invalido de gatilho/espera e recusado")
    finally:
        ia.sonda, ia.Vigia = orig_sonda, orig_vigia
        for c in S.H.cams.values():
            c.nao_ouve()


if __name__ == "__main__":
    teste_interpreta()
    teste_presenca()
    teste_presenca_conexao()
    teste_decide()
    srv, porta = sobe_camera()
    teste_sonda(porta)
    teste_vigia(porta)
    if "--sem-servico" not in sys.argv:
        teste_servico(porta)
    srv.shutdown()
    print("\ntodos passaram")
