"""Testes do gatilho em outros fabricantes e da leitura do Shinobi. Sem
camera de verdade: uma Hikvision falsa em localhost fala a ISAPI.

  python teste_fabricantes.py

O XML do alerta e o capturado na Arena Litoral em 12/09/2026
(DS-2CD1121G2-LIU, MD 2.0 com alvo humano), com IP e MAC trocados.
"""
from __future__ import annotations

import json
import queue
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


ALERTA = ('<EventNotificationAlert version="2.0" '
          'xmlns="http://www.hikvision.com/ver20/XMLSchema">\r\n'
          '<ipAddress>192.168.1.10</ipAddress>\r\n<portNo>80</portNo>\r\n'
          '<protocol>HTTP</protocol>\r\n<macAddress>00:00:00:00:00:00</macAddress>\r\n'
          '<channelID>1</channelID>\r\n<dateTime>1970-01-04T02:13:29-3:00</dateTime>\r\n'
          '<activePostCount>1</activePostCount>\r\n<eventType>{tipo}</eventType>\r\n'
          '<eventState>{estado}</eventState>\r\n<eventDescription>{desc}</eventDescription>\r\n'
          '</EventNotificationAlert>\r\n')


class Hik(BaseHTTPRequestHandler):
    """Fala a ISAPI de uma Hikvision. Os atributos de classe montam o modelo."""
    modelo = "DS-2CD1121G2-LIU"
    md = {"enabled": "true", "targetType": "human"}    # None = sem MD 2.0 (G0)
    smart = None                                        # None = 403
    campo = None                                        # XML do FieldDetection
    assinantes = []
    batida_s = 0.3

    def log_message(self, *a):
        pass

    @classmethod
    def emite(cls, tipo, estado="active"):
        for f in list(cls.assinantes):
            f.put((tipo, estado))

    def _xml(self, corpo, status=200):
        b = corpo.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        c, p = type(self), self.path
        if p.startswith("/cgi-bin/"):
            return self._xml("", 404)                   # nao e Intelbras/Dahua
        if p == "/ISAPI/System/deviceInfo":
            return self._xml(f"<DeviceInfo><model>{c.modelo}</model>"
                             "<deviceType>IPCamera</deviceType></DeviceInfo>")
        if p.endswith("/motionDetection"):
            alvo = (f"<targetType>{c.md['targetType']}</targetType>"
                    if c.md.get("targetType") is not None else "")
            return self._xml(f"<MotionDetection><enabled>{c.md['enabled']}</enabled>"
                             f"<regionType>grid</regionType>{alvo}</MotionDetection>")
        if p == "/ISAPI/Smart/capabilities":
            if c.smart is None:
                return self._xml("", 403)
            return self._xml("<SmartCap>" + "".join(
                f"<isSupport{k}>{v}</isSupport{k}>" for k, v in c.smart.items())
                + "</SmartCap>")
        if p == "/ISAPI/Smart/FieldDetection/1" and c.campo:
            return self._xml(c.campo)
        if p == "/ISAPI/Event/notification/alertStream":
            fila = queue.Queue()
            c.assinantes.append(fila)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/mixed; boundary=boundary")
            self.end_headers()
            prox = 0.0
            try:
                while True:
                    try:
                        tipo, estado = fila.get(timeout=0.05)
                        desc = "Motion alarm"
                    except queue.Empty:
                        if time.time() < prox:
                            continue
                        prox = time.time() + c.batida_s
                        tipo, estado, desc = "videoloss", "inactive", "videoloss alarm"
                    corpo = ALERTA.format(tipo=tipo, estado=estado, desc=desc)
                    self.wfile.write((f"--boundary\r\nContent-Type: application/xml; "
                                      f"charset=\"UTF-8\"\r\nContent-Length: "
                                      f"{len(corpo)}\r\n\r\n{corpo}").encode())
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                c.assinantes.remove(fila)
            return
        self._xml("", 404)


def campo_xml(topo, regiao, alvo="human"):
    """FieldDetection como a DS-2CD2347G2-LU da Tribo do Lobo devolve."""
    regs = "".join(
        f'<FieldDetectionRegion version="2.0"><id>{i}</id><enabled>'
        f'{"true" if (regiao and i == 2) else "false"}</enabled>'
        f'<sensitivityLevel>50</sensitivityLevel><detectionTarget>{alvo}'
        f'</detectionTarget></FieldDetectionRegion>' for i in (1, 2, 3, 4))
    return (f'<FieldDetection version="2.0"><id>1</id><enabled>{topo}</enabled>'
            f'<FieldDetectionRegionList size="4">{regs}</FieldDetectionRegionList>'
            f'</FieldDetection>')


def teste_sonda(porta):
    print("1. sonda Hikvision: o que a camera oferece, pela ISAPI")
    s = lambda: ia.sonda("127.0.0.1", "u", "p", porta=porta)
    r = s()
    ok(r["fabricante"] == "hikvision" and r["modelo"] == "DS-2CD1121G2-LIU"
       and ia.usavel(r) and r["eventos"] == ["VMD"],
       "Arena Litoral: MD 2.0 com alvo humano e ligado -> usavel, ouve VMD")
    Hik.md = {"enabled": "false", "targetType": "human,vehicle"}
    r = s()
    ok(r["suporta"] is True and r["ligada"] is False and not ia.usavel(r)
       and "desligada" in r["motivo"],
       "Arena Sunset: tem o filtro, movimento desligado -> suporta, nao usa")
    Hik.md, Hik.modelo = {"enabled": "true", "targetType": None}, "DS-2CD1321G0-I"
    r = s()
    ok(r["suporta"] is False and "sem filtro" in r["motivo"],
       "linha G0: movimento sem alvo -> sem IA de pessoa")
    Hik.modelo = "DS-2CD2347G2-LU"
    Hik.md = {"enabled": "false", "targetType": ""}
    Hik.smart = {"FieldDetection": "true", "LineDetection": "false"}
    Hik.campo = campo_xml("false", False)
    r = s()
    ok(r["suporta"] is True and r["ligada"] is False,
       "Tribo do Lobo: AcuSense com tudo desligado -> suporta, nao usa")
    Hik.campo = campo_xml("true", True)
    r = s()
    ok(ia.usavel(r) and r["eventos"] == ["fielddetection"],
       "AcuSense com invasao de area ligada numa regiao com alvo humano -> usavel")
    Hik.campo = campo_xml("true", True, alvo="vehicle")
    ok(not ia.usavel(s()), "a mesma regiao ligada mas so para veiculo -> nao usa")
    Hik.campo = campo_xml("true", False)
    ok(not ia.usavel(s()), "evento ligado mas nenhuma regiao ligada -> nao usa")
    Hik.md, Hik.smart, Hik.campo = {"enabled": "true", "targetType": "human"}, None, None
    Hik.modelo = "DS-2CD1121G2-LIU"


def teste_vigia(porta):
    print("2. vigia Hikvision: alertStream vira a mesma Presenca")
    r = ia.sonda("127.0.0.1", "u", "p", porta=porta)
    p = ia.Presenca(espera_s=600, carencia_s=30)
    v = ia.Vigia("hik", "127.0.0.1", "u", "p", p, porta=porta, ia=r).inicia()
    ok(espera(lambda: p.conectado), "conectou no alertStream")
    time.sleep(1.0)
    ok(p.contagem == {}, "batidas (videoloss inactive) nao contam como pessoa")
    for _ in range(3):
        Hik.emite("VMD")
    ok(espera(lambda: p.contagem.get("camera") == 3),
       f"3 VMD active -> 3 pulsos de pessoa ({p.contagem})")
    ok(not p.em_curso, "pulso nao deixa 'em curso' preso (Hikvision nao manda fim)")
    Hik.emite("fielddetection")
    Hik.emite("VMD", "inactive")
    time.sleep(1.0)
    ok(p.contagem.get("camera") == 3, "evento nao ligado e VMD inactive nao contam")
    ok(p.tem_gente(time.time()) and ia.decide(True, "ia", r, p, time.time())
       == (True, "gente"), "a regra do gatilho nao sabe de fabricante: 'gente'")
    v.para()
    ok(espera(lambda: not v._thread.is_alive(), 8), "para() encerra a vigia")


def teste_mysql():
    print("3. leitura do Shinobi: o escape do `mysql -B`")
    import servico as S
    original = json.dumps({"muser": "admin", "mpass": "s3nh@#1",
                           "detector_cascades": json.dumps({"a": "b/c"}),
                           "obs": "linha1\nlinha2"})
    # o que o `mysql -B` devolve: barra dobrada, quebra e tab escapados
    lido = original.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
    try:
        json.loads(lido.replace("\\n", ""))
        antigo = "ok"
    except Exception:
        antigo = "falha"
    ok(antigo == "falha",
       "o metodo antigo quebra com barra no details (Epic Boulevard, 9 cameras)")
    d = json.loads(S.desescapa_mysql(lido), strict=False)
    ok(d["muser"] == "admin" and d["mpass"] == "s3nh@#1",
       "desescape completo: credencial certa")
    ok(json.loads(S.desescapa_mysql(json.dumps({"muser": "u"})))["muser"] == "u",
       "details sem barra continua igual")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Hik)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    porta = srv.server_address[1]
    teste_sonda(porta)
    teste_vigia(porta)
    teste_mysql()
    srv.shutdown()
    print("\ntodos passaram")
