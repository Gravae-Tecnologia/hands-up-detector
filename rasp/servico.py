"""Painel web: selecione uma ou varias cameras e veja esqueletos a 1 fps.

Sem Flask e sem framework - `http.server` da stdlib basta e mantem a promessa
de nao instalar dependencia na Pi de producao.

ARQUITETURA: FILA + POOL, NAO UM PROCESSO POR CAMERA
    Duas razoes, as duas medidas nesta Pi:

    Memoria - cada Pipeline carrega detector + pose (~200 MB). Uma por camera
    daria 800 MB com 4 cameras, e sobram ~2,1 GB com o Shinobi rodando.

    CPU - com 4 nucleos, medimos que 2 threads por sessao ONNX rende mais que
    4 (399 ms contra 472; o 3o e 4o nucleo custam mais em sincronizacao do
    que entregam). E 4 workers de 1 thread rendem 5,78 inf/s contra 2,12 de
    um worker serial de 4 threads. Entao: poucos workers, poucas threads
    cada, alimentados por uma fila.

DESCARTA QUADRO VELHO, NAO ENFILEIRA
    Cada camera guarda apenas o quadro MAIS RECENTE. Se o pool nao vence a
    demanda, o quadro anterior e descartado em vez de virar backlog - para
    deteccao de gesto, quadro velho nao tem valor, e uma fila FIFO longa faria
    a defasagem crescer sem limite. Os descartes sao contados e mostrados,
    porque descarte silencioso vira "funciona" na demo e "nao pegou o gesto"
    em producao.

  GET /                    grade + debug
  GET /api/cameras         monitores (do MariaDB, ver `cameras()`)
  GET /quadro/<mid>.mjpg   stream da camera (anotado se selecionada)
  POST /api/alterna        liga/desliga o processamento de uma camera
  GET /api/stats           metricas por camera + globais
"""
from __future__ import annotations

import argparse
import json
import os
import http.client
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urlencode

import cv2
import numpy as np

import config as cfgmod
import cronologia as cronmod
import motor
import rastreio as rastmod
import revisao as revmod

DIR = os.path.dirname(os.path.abspath(__file__))
NUVEM = None          # http.client.HTTPConnection por camera, se modo nuvem


def cameras():
    """Monitores do Shinobi, com o RTSP completo.

    Le do MariaDB e nao da API HTTP porque a API **omite as credenciais** da
    camera (`muser`/`mpass` voltam vazios) e sem elas o RTSP responde 401.
    host/port/path sao colunas da tabela; muser/mpass vivem no JSON de
    `details` - as duas fontes sao necessarias para montar a URL.
    """
    sql = ("SELECT mid, name, host, port, path, width, height, mode, details "
           "FROM Monitors ORDER BY mid")
    p = subprocess.run(["mysql", "-umajesticflame", "ccio", "-N", "-B", "-e", sql],
                       capture_output=True, timeout=20)
    saida = []
    for linha in p.stdout.decode("utf-8", "replace").strip().split("\n"):
        campos = linha.split("\t")
        if len(campos) < 9:
            continue
        mid, nome, host, porta, caminho, larg, alt, modo = campos[:8]
        try:
            det = json.loads(campos[8].replace("\\n", ""))
        except Exception:
            det = {}
        usr, pwd = det.get("muser") or "", det.get("mpass") or ""
        cred = f"{usr}:{pwd}@" if usr else ""
        saida.append({
            "mid": mid, "nome": nome or mid, "status": modo,
            "res": f"{larg}x{alt}",
            "rtsp": f"rtsp://{cred}{host}:{porta}/{caminho.lstrip('/')}",
        })
    return saida


def temperatura():
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except Exception:
        return 0.0


def throttled():
    try:
        s = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                           timeout=5).stdout.decode()
        return s.strip().split("=")[-1]
    except Exception:
        return "?"


class Camera:
    """Captura 1 fps de uma camera e guarda SO o quadro mais recente.

    A captura so existe enquanto a camera esta LIGADA. Cada ffmpeg custava
    ~27% de um nucleo (o stream principal e 1280x720@30 e o `-vf fps=1`
    descarta DEPOIS de decodificar), entao manter quatro rodando gastava
    ~110% de CPU com tres cameras que ninguem estava olhando - e a Pi ja
    chegou a 84,7 C com throttling ativo por causa disso.

    Para a grade continuar util sem custo, cada camera guarda uma FOTO tirada
    uma vez so no arranque.
    """

    def __init__(self, cam, larg, alt, fps=1.0):
        self.cam, self.larg, self.alt, self.fps = cam, larg, alt, fps
        self.mid = cam["mid"]
        self.lock = threading.Lock()
        self.quadro = None          # ultimo quadro cru
        self.t_quadro = 0.0         # instante em que ele foi capturado
        self.novo = False           # ha quadro ainda nao processado?
        self.saida = None           # ultimo JPEG (foto, ou anotado se ligada)
        self.ativa = False          # captura + entra no pool de inferencia?
        self.erro = None
        self.confs = []
        self.m = {"capturados": 0, "processados": 0, "descartados": 0,
                  "pessoas": 0, "gestos": 0, "ms": 0.0, "ms_det": 0.0,
                  "ms_pose": 0.0, "ms_encode": 0.0, "ms_rede": 0.0,
                  "kb": 0.0, "falhas": 0, "ultimo_gesto": 0.0,
                  "instantaneos": 0, "segurando": 0.0, "amostras": []}
        self.nuvem = None
        self.cron = None
        # um rastreador POR CAMERA: pessoas de quadras diferentes nao se
        # confundem, e cada camera tem sua propria escala de pixels
        self.rast = rastmod.Rastreador(dur_s=H.cfg.get("dur_gesto", 2.0))
        self.parar = threading.Event()
        self.thread = None
        threading.Thread(target=self.foto, daemon=True).start()

    def foto(self):
        """Um quadro so, para a miniatura. Custa um ffmpeg de ~2 s e acabou."""
        n = self.larg * self.alt * 3
        try:
            p = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-rtsp_transport", "tcp", "-i", self.cam["rtsp"],
                 "-frames:v", "1", "-vf", f"scale={self.larg}:{self.alt}",
                 "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                capture_output=True, timeout=40)
            if len(p.stdout) >= n:
                img = np.frombuffer(p.stdout[:n], np.uint8).reshape(
                    self.alt, self.larg, 3)
                ok, enc = cv2.imencode(".jpg", img,
                                       [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                if ok and not self.ativa:
                    self.saida = enc.tobytes()
            else:
                self.erro = p.stderr.decode("utf-8", "replace").strip()[:120]
        except Exception as e:
            self.erro = f"{type(e).__name__}"

    def liga(self):
        if self.ativa:
            return
        self.ativa = True
        self.parar.clear()
        if self.nuvem is not None:
            self.cron = cronmod.Cronologia(
                enviar=self._envia_nuvem, consumir=self._consome,
                fps=self.fps, nome=self.mid,
                em_voo_min=H.cfg.get("em_voo_min", 1),
                em_voo_max=H.cfg.get("em_voo_max", 4),
                ajuste_s=H.cfg.get("ajuste_s", 10.0))
            self.cron.inicia()
        self.thread = threading.Thread(target=self._captura, daemon=True)
        self.thread.start()

    def desliga(self):
        if not self.ativa:
            return
        self.ativa = False
        self.parar.set()
        if self.thread:
            self.thread.join(timeout=8)
        self.thread = None
        if self.cron is not None:
            self.cron.para()
            self.cron = None
        with self.lock:
            self.novo = False
        threading.Thread(target=self.foto, daemon=True).start()

    def _captura(self):
        n = self.larg * self.alt * 3
        while not self.parar.is_set():
            p = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-rtsp_transport", "tcp", "-i", self.cam["rtsp"],
                 "-vf", f"fps={self.fps},scale={self.larg}:{self.alt}",
                 "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=n * 3)
            try:
                while not self.parar.is_set():
                    buf = p.stdout.read(n)
                    if len(buf) < n:
                        self.erro = (p.stderr.read(200).decode("utf-8", "replace")
                                     .strip()[:120] or "stream caiu")
                        break
                    img = np.frombuffer(buf, np.uint8).reshape(
                        self.alt, self.larg, 3).copy()
                    self.m["capturados"] += 1
                    self.erro = None
                    if self.cron is not None:
                        # o pipe e SEMPRE lido (senao o buffer do ffmpeg enche
                        # e entrega quadro velho depois); a Cronologia decide
                        # se este vira requisicao ou e recusado. Os dois
                        # motivos de recusa sao contados separados nela
                        # (`decimados` x `sem_vaga`) - somar os dois num
                        # "descartados" so esconderia qual dos dois esta
                        # acontecendo.
                        if not self.cron.oferece(img):
                            self.m["descartados"] += 1
                    else:
                        with self.lock:
                            if self.novo:
                                self.m["descartados"] += 1
                            self.quadro = img
                            self.t_quadro = time.time()
                            self.novo = True
            finally:
                p.kill()
            if not self.parar.is_set():
                self.parar.wait(4)  # camera de arena cai e volta o tempo todo

    def pega(self):
        with self.lock:
            if not (self.novo and self.ativa):
                return None
            self.novo = False
            return self.quadro

    def _envia_nuvem(self, q):
        """Roda nas threads de envio da Cronologia. Pode bloquear na rede.

        Levantar excecao marca o quadro como falho e a janela avanca sem
        travar a camera - por isso o erro nao e engolido aqui.
        """
        r, ms_encode, ms_rede = self.nuvem.infere(q.img)
        if r is None or "erro" in r:
            raise RuntimeError((r or {}).get("erro", "sem resposta"))
        return r, {"ms_encode": ms_encode, "ms_rede": ms_rede}

    def _consome(self, q):
        """Roda numa thread unica, SEMPRE em ordem de seq.

        O relogio do rastreio e `q.t_captura`, nao o instante da resposta: com
        a latencia variando de 400 a 3.420 ms, usar a chegada faria o
        `segurando_s` do gesto carregar o jitter da rede em vez do tempo real
        que a pessoa segurou os bracos.
        """
        if q.erro:
            self.erro = q.erro
            self.m["falhas"] += 1
            if H.registro:
                H.registro.escreve(cam=self.mid, evento="falha", seq=q.seq,
                                   erro=q.erro, t_captura=q.t_captura,
                                   t_envio=q.t_envio, t_resposta=q.t_resposta)
            return
        img, t_cap = q.img, q.t_captura
        t_envio, t_resp = q.t_envio, q.t_resposta
        r = q.resultado
        ms_encode = q.meta.get("ms_encode", 0.0)
        ms_rede = q.meta.get("ms_rede", 0.0)
        self.erro = None
        kpts = [np.array(k, np.float32) for k in r.get("kpts", [])]
        caixas = np.array(r.get("caixas", []), np.float32).reshape(-1, 4)
        # margem continua por pessoa: e o que permite recalibrar o limiar
        # depois sem recapturar nada
        margens = [motor.gesto_margem(k) for k in kpts]

        # confirmacao temporal ANTES de desenhar: o gesto so vale se a
        # MESMA pessoa segurar por `dur_s`. Sem rastreio, dois quadros de
        # pessoas diferentes pareceriam uma segurando.
        por_pessoa, confirmados = self.rast.passo(
            kpts, margens, revmod.LIMIAR, caixas=caixas, agora=t_cap)
        n_g = len(confirmados)
        n_inst = sum(1 for p in por_pessoa if p["instantaneo"])

        # a pessoa de MAIOR margem e a que interessa registrar: e quem
        # esta com os bracos mais levantados no quadro
        i_pico, pico = -1, None
        for i, mm in enumerate(margens):
            if mm is not None and (pico is None or mm > pico):
                i_pico, pico = i, mm
        # copia LIMPA antes de desenhar: o esqueleto cobre o rosto, e o
        # historico existe justamente para reconhecer quem levantou a mao.
        # So copia quando ha evidencia a guardar - 768 KB por quadro seria
        # desperdicio a 1 fps sem gesto nenhum.
        precisa = (H.revisao is not None and pico is not None
                   and pico >= revmod.QUASE)
        img_limpo = img.copy() if precisa else None

        motor.desenha(img, caixas, kpts, conf_min=0.0,
                      gestos=[p["confirmado"] for p in por_pessoa],
                      trilhas=[p["id"] for p in por_pessoa])
        for i, p in enumerate(por_pessoa):
            if i >= len(caixas):
                return
            x1, y1 = int(caixas[i][0]), int(caixas[i][1])
            if p["instantaneo"] and not p["confirmado"]:
                cv2.putText(img, f"{p['segurando_s']:.1f}s",
                            (x1, max(y1 - 22, 12)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 200, 255), 2)
        total = ms_encode + ms_rede + r.get("ms_servidor", 0)
        rr = self.rast.resumo()
        cv2.putText(img, f"{self.cam['nome']}  {total:.0f}ms  {len(kpts)}p  "
                    f"trilhas {rr['trilhas_vivas']}  "
                    f"int {rr['intervalo_s']:.1f}s  tol {rr['tolerancia_s']:.1f}s",
                    (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        ok, enc = cv2.imencode(".jpg", img,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if ok:
            self.saida = enc.tobytes()
        m = self.m
        m["processados"] += 1
        m["pessoas"] = len(kpts)
        m["gestos"] += n_g
        m["instantaneos"] += n_inst
        m["segurando"] = max((p["segurando_s"] for p in por_pessoa), default=0.0)
        if n_g:
            m["ultimo_gesto"] = time.time()
        m["ms"] = total
        m["ms_encode"], m["ms_rede"] = ms_encode, ms_rede
        m["ms_det"] = r.get("ms_infer", 0.0)
        m["ms_pose"] = r.get("ms_servidor", 0.0) - r.get("ms_infer", 0.0)
        m["kb"] = r.get("_bytes", 0) / 1024
        m["amostras"].append(total)
        del m["amostras"][:-120]

        if precisa:
            # captura tambem os "quase": sem eles so da para medir
            # precisao, nunca recall - falso negativo e invisivel
            pp = por_pessoa[i_pico] if i_pico < len(por_pessoa) else {}
            H.revisao.guarda(
                img, self.mid, pico, len(kpts), (t_resp - t_cap) * 1e3,
                caixa=caixas[i_pico] if i_pico < len(caixas) else None,
                img_limpo=img_limpo,
                extra={"modelo": r.get("modelo"),
                       "segurando_s": pp.get("segurando_s", 0.0),
                       "confirmado": bool(pp.get("confirmado")),
                       "trilha": pp.get("id")})
        if H.registro:
            H.registro.escreve(
                cam=self.mid, evento="gesto" if n_g else "quadro",
                pessoas=len(kpts), gestos=n_g,
                t_captura=t_cap, t_envio=t_envio, t_resposta=t_resp,
                fila_ms=round((t_envio - t_cap) * 1e3, 1),
                ms_encode=round(ms_encode, 1),
                ms_rede=round(ms_rede, 1),
                ms_servidor=r.get("ms_servidor"),
                ms_infer=r.get("ms_infer"),
                ms_decode=r.get("ms_decode"),
                ms_total=round(total, 1),
                ms_captura_ate_resposta=round((t_resp - t_cap) * 1e3, 1),
                kb=round(m["kb"], 1), modelo=r.get("modelo"),
                temp=round(temperatura(), 1))
        if n_g:
            # aviso na hora, fora do laco: a rede da plataforma nao pode
            # atrasar o proximo quadro
            threading.Thread(
                target=self._avisa, args=(n_g, len(kpts), t_cap, t_resp),
                daemon=True).start()
        q.img = None      # so agora o quadro cru pode ser liberado

    def _avisa(self, n_g, pessoas, t_cap, t_resp):
        a = time.time()
        # GRAVA PRIMEIRO, avisa depois. O lance esta acontecendo agora: cada
        # milissegundo gasto no POST da plataforma e um pedaco do lance que o
        # Shinobi ainda nao comecou a gravar. O aviso pode chegar 200 ms mais
        # tarde sem prejuizo; o video, nao.
        gravou = dispara_gravacao(self.mid, H.cfg.get("api_key"),
                                  H.cfg.get("arena"))
        res = avisa_plataforma(
            H.cfg.get("webhook"), H.cfg.get("api_key"), self.cam["nome"], n_g,
            {"cam": self.mid, "camera": self.mid, "pessoas": pessoas,
             "gestos": n_g,
             "detectado_em": t_resp,
             "latencia_ms": round((t_resp - t_cap) * 1e3, 1),
             "gravacao": gravou},
            serial=H.cfg.get("serial", ""))
        H.alertas.append({"t": a, "hora": time.strftime("%H:%M:%S"),
                          "cam": self.mid, "gestos": n_g, "gravacao": gravou,
                          "latencia_deteccao_ms": round((t_resp - t_cap) * 1e3, 1),
                          "webhook": res})
        del H.alertas[:-200]
        if H.registro:
            H.registro.escreve(cam=self.mid, evento="aviso_plataforma",
                               gestos=n_g, t_aviso=a, webhook=res,
                               gravacao=gravou,
                               latencia_deteccao_ms=round((t_resp - t_cap) * 1e3, 1))

    def registra(self, img, caixas, kpts, ms, ms_det, ms_pose):
        for k in kpts:
            self.confs.extend(k[:, 2].tolist())
        del self.confs[:-4000]
        # o SimCC do RTMPose nao sai em [0,1]: normaliza pelo p90 do proprio
        # modelo antes de comparar com um limiar comum
        esc = (float(np.percentile(self.confs, 90))
               if len(self.confs) > 200 else 1.0)
        gestos = [motor.gesto_bracos(k, escala_conf=esc) for k in kpts]
        motor.desenha(img, caixas, kpts, escala_conf=esc, gestos=gestos)
        cv2.putText(img, f"{self.cam['nome']}  {ms:.0f}ms  {len(kpts)}p",
                    (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        if ok:
            self.saida = enc.tobytes()
        m = self.m
        m["processados"] += 1
        m["pessoas"] = len(kpts)
        m["gestos"] += sum(gestos)
        m["ms"], m["ms_det"], m["ms_pose"] = ms, ms_det, ms_pose
        m["amostras"].append(ms)
        del m["amostras"][:-120]


class Registro:
    """Uma linha JSONL por quadro, com TODOS os tempos.

    Escrito na hora e com flush: o teste e presencial e o interesse e revisar
    depois, entao perder o registro por causa de buffer nao pago seria o pior
    resultado possivel. Uma linha por quadro a 1 fps x 4 cameras da ~350 KB
    por hora - irrelevante nos 20 GB livres.

    Guarda os instantes ABSOLUTOS (captura, envio, resposta) alem das duracoes,
    porque so com eles da para reconstruir a ordem real dos eventos entre
    cameras e cruzar com o que a pessoa fez na quadra.
    """

    def __init__(self, caminho):
        self.f = open(caminho, "a", buffering=1, encoding="utf-8")
        self.lock = threading.Lock()
        self.n = 0

    def escreve(self, **kw):
        kw["t"] = time.time()
        kw["hora"] = time.strftime("%H:%M:%S", time.localtime())
        with self.lock:
            self.f.write(json.dumps(kw, ensure_ascii=False) + "\n")
            self.n += 1


#: Intervalo minimo entre dois triggers da MESMA camera.
#:
#: O gesto dura mais que um quadro: a 1 fps, um braco levantado por 4 segundos
#: dispararia 4 gravacoes empilhadas do mesmo lance. O daemon de botao tem o
#: mesmo problema resolvido por debounce no GPIO; aqui e por tempo. 15 s cobre
#: a janela de video do Shinobi sem perder um segundo aperto de verdade.
TRIGGER_COOLDOWN = float(os.environ.get("HANDS_UP_TRIGGER_COOLDOWN", 15))
_ultimo_trigger = {}


def dispara_gravacao(mid, api_key, group_key, shinobi="http://127.0.0.1:8080"):
    """Manda o Shinobi gravar a camera que positivou.

    E EXATAMENTE a chamada do daemon de botoes - mesma rota `/motion/`, mesmo
    `force=1`. O que muda e so a origem no `data`, para o evento no Shinobi
    dizer de onde veio: aperto de botao ou bracos levantados.

    A CAMERA E A QUE POSITIVOU, nao a quadra inteira: o `mid` que chega aqui e
    o da imagem onde o gesto foi visto. Uma quadra com duas cameras grava a que
    viu o gesto.

    Sem `apiKey` ou `groupKey` no device.json nao ha o que fazer - devolve o
    motivo em vez de estourar, porque isto roda numa thread solta e uma excecao
    aqui morreria calada.
    """
    if not api_key or not group_key:
        return {"ok": False, "erro": "sem apiKey/groupKey no device.json"}

    agora = time.time()
    ultimo = _ultimo_trigger.get(mid, 0)
    if agora - ultimo < TRIGGER_COOLDOWN:
        return {"ok": False, "ignorado": "cooldown",
                "faltam_s": round(TRIGGER_COOLDOWN - (agora - ultimo), 1)}
    _ultimo_trigger[mid] = agora

    dados = urlencode({
        "data": json.dumps({"plug": "hands-up", "reason": "hands_up"}),
        "force": "1",
    })
    url = f"{shinobi.rstrip('/')}/{api_key}/motion/{group_key}/{mid}?{dados}"
    a = time.perf_counter()
    try:
        u = urlparse(url)
        cls = (http.client.HTTPSConnection if u.scheme == "https"
               else http.client.HTTPConnection)
        c = cls(u.hostname, u.port, timeout=5)
        c.request("GET", u.path + ("?" + u.query if u.query else ""))
        r = c.getresponse()
        r.read()
        c.close()
        return {"ok": r.status == 200, "status": r.status,
                "ms": round((time.perf_counter() - a) * 1e3, 1)}
    except Exception as e:
        return {"ok": False, "erro": type(e).__name__,
                "ms": round((time.perf_counter() - a) * 1e3, 1)}


def avisa_plataforma(url, api_key, cam, n_gestos, detalhe, serial=""):
    """POST imediato no gesto.

    NAO passa pelo Phoenix de proposito: o daemon dele enfileira em SQLite e
    so descarrega a cada 300 s, o que atrasaria o aviso em ate 5 minutos. O
    formato do corpo segue o do webhook do Phoenix para ser compativel, mas a
    entrega e direta.

    O tipo `gesture_detected` nao esta em NOTIFY_TRIGGER_TYPES do backend, ou
    seja: fica registrado como alerta mas nao dispara a maquinaria de
    notificacao de arena (que chamaria gente a toa durante um teste).
    """
    if not url:
        return None
    corpo = json.dumps({
        # O serial e quem identifica o device no OPS: e o unico identificador
        # que TODA Pi tem a mao (device.json -> deviceId). O `apiKey` abaixo
        # carrega o shinobiApiKey e vai junto so por compatibilidade com o
        # formato do Phoenix - o OPS nao guarda esse valor em coluna.
        "serial": serial,
        "apiKey": api_key,
        "alerts": [{
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "type": "gesture_detected",
            "severity": "info",
            "message": f"Bracos levantados em {cam} ({n_gestos})",
            "details": detalhe,
        }],
    }).encode()
    a = time.perf_counter()
    try:
        u = urlparse(url)
        cls = (http.client.HTTPSConnection if u.scheme == "https"
               else http.client.HTTPConnection)
        c = cls(u.hostname, u.port, timeout=8)
        c.request("POST", u.path or "/", body=corpo,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        c.close()
        return {"status": r.status, "ms": round((time.perf_counter() - a) * 1e3, 1)}
    except Exception as e:
        return {"status": 0, "erro": type(e).__name__,
                "ms": round((time.perf_counter() - a) * 1e3, 1)}


class Nuvem:
    """Manda o quadro para o endpoint remoto e devolve o resultado.

    Uma conexao POR CAMERA e com keep-alive: sem reaproveitar a conexao cada
    quadro pagaria TCP handshake, que em link de arena passa de 200 ms - mais
    caro que a inferencia local que estamos tentando evitar.

    Reconecta sozinho: link de arena cai, e cair sem religar transformaria a
    camera em tela parada sem aviso.
    """

    def __init__(self, url, qualidade=75, arena="", camera=""):
        u = urlparse(url)
        self.https = (u.scheme == "https")
        self.host = u.hostname
        self.porta = u.port or (443 if self.https else 80)
        self.caminho = u.path or "/"
        self.qualidade = qualidade
        self.arena, self.camera = arena, camera
        # UMA CONEXAO POR THREAD, nao uma sob lock: com varias requisicoes em
        # voo, um lock em volta do request/response serializaria tudo de novo
        # e a concorrencia nao existiria. `threading.local` da a cada thread
        # de envio a sua propria conexao keep-alive.
        self._local = threading.local()

    def _liga(self):
        # Cloud Run so atende HTTPS; VM propria pode ser HTTP simples.
        cls = (http.client.HTTPSConnection if self.https
               else http.client.HTTPConnection)
        self._local.conn = cls(self.host, self.porta, timeout=30)

    def infere(self, img):
        """-> (resultado, ms_encode, ms_rede) ou (None, ms_encode, 0)."""
        a = time.perf_counter()
        ok, enc = cv2.imencode(".jpg", img,
                               [int(cv2.IMWRITE_JPEG_QUALITY), self.qualidade])
        ms_encode = (time.perf_counter() - a) * 1e3
        if not ok:
            return None, ms_encode, 0.0
        corpo = enc.tobytes()
        for tentativa in (1, 2):
            try:
                if getattr(self._local, "conn", None) is None:
                    self._liga()
                a = time.perf_counter()
                self._local.conn.request(
                    "POST", self.caminho, body=corpo,
                    headers={"Content-Type": "image/jpeg",
                             "Content-Length": str(len(corpo)),
                             # multi-tenant: o servidor separa a escala de
                             # confianca por camera e loga por arena
                             "X-Arena": self.arena,
                             "X-Camera": self.camera})
                r = json.loads(self._local.conn.getresponse().read())
                rtt = (time.perf_counter() - a) * 1e3
                r["_bytes"] = len(corpo)
                return r, ms_encode, rtt - r.get("ms_servidor", 0)
            except Exception as e:
                try:
                    self._local.conn.close()
                except Exception:
                    pass
                self._local.conn = None
                if tentativa == 2:
                    return {"erro": f"{type(e).__name__}"}, ms_encode, 0.0
        return None, ms_encode, 0.0


class Pool:
    """K workers, cada um com seu Pipeline, girando entre as cameras ativas."""

    def __init__(self, n_workers, det, pose, threads, max_pessoas):
        self.pedidos = queue.Queue()
        self.workers = []
        self.ocupado_ms = 0.0
        self.inicio = time.time()
        self.lock = threading.Lock()
        for i in range(n_workers):
            p = motor.Pipeline(det, pose, threads, max_pessoas)
            t = threading.Thread(target=self._roda, args=(p,), daemon=True)
            t.start()
            self.workers.append(p)
        self.nome = self.workers[0].nome

    def _roda(self, pipe):
        while True:
            cam = self.pedidos.get()
            img = cam.pega()
            if img is None:
                continue
            a = time.perf_counter()
            caixas, kpts = pipe(img)
            ms = (time.perf_counter() - a) * 1e3
            with self.lock:
                self.ocupado_ms += ms
            cam.registra(img, caixas, kpts, ms, pipe.ms_det, pipe.ms_pose)

    def despacha(self, cams):
        """Enfileira so quem tem quadro fresco; a fila nunca acumula."""
        for c in cams:
            if c.ativa and c.novo and self.pedidos.qsize() < len(self.workers) * 2:
                self.pedidos.put(c)


PAGINA = """<!doctype html><meta charset=utf-8><title>Gravae - esqueletos</title>
<style>
*{box-sizing:border-box}
body{background:#0b0d11;color:#e8eaed;font:14px system-ui,-apple-system,sans-serif;margin:0;padding:20px}
h1{font-size:18px;margin:0 0 4px;font-weight:600}
.sub{color:#8b93a7;font-size:13px;margin-bottom:16px}
.grade{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-bottom:20px}
.cam{background:#12151c;border:2px solid #1e222b;border-radius:10px;overflow:hidden;cursor:pointer;transition:.15s}
.cam:hover{border-color:#4a4f5e}
.cam.on{border-color:#7c5cff;box-shadow:0 0 0 3px rgba(124,92,255,.14)}
.cam img{width:100%;display:block;aspect-ratio:16/10;object-fit:cover;background:#000}
.cam .lb{padding:8px 10px;display:flex;justify-content:space-between;align-items:center;font-size:13px}
.tag{font-size:11px;font-weight:700;letter-spacing:.4px}
.tag.on{color:#7c5cff}.tag.off{color:#5a6072}
h2{font-size:14px;margin:22px 0 8px;color:#8b93a7;font-weight:600}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;color:#8b93a7;font-weight:500;padding:6px 10px;border-bottom:1px solid #232833}
td{padding:6px 10px;border-bottom:1px solid #161a21}
.n{text-align:right;font-variant-numeric:tabular-nums}
.ok{color:#4ade80}.warn{color:#fbbf24}.bad{color:#ff6b6b}
.err{color:#ff6b6b;font-size:12px}
tr.alerta td{background:#3a1020;color:#ffd0d0}
tr.alerta td:first-child::before{content:"\2691 ";color:#ff4d6d}
@keyframes pisca{0%,100%{opacity:1}50%{opacity:.45}}
tr.alerta{animation:pisca 1s infinite}
</style>
<h1>Esqueletos ao vivo &mdash; 1 quadro por segundo</h1>
<div class=sub>Clique numa camera para <b>ligar</b> a analise. So ela captura e processa &mdash;
as outras param o ffmpeg por completo, para nao gastar CPU a toa.<br>
<a href="/pessoas" style="color:#7c5cff">quem levantou as maos</a> &middot;
<a href="/revisao" style="color:#7c5cff">validar deteccoes</a></div>
<div class=grade id=grade></div>

<h2>Processamento</h2>
<table id=tab></table>
<h2>Sistema</h2>
<table id=sis></table>
<script>
let cams=[];
async function listar(){
  cams=await (await fetch('/api/cameras')).json();
  document.getElementById('grade').innerHTML=cams.map(c=>`
    <div class=cam id="c_${c.mid}" onclick="alterna('${c.mid}')">
      <img id="i_${c.mid}" src="/quadro/${c.mid}.mjpg">
      <div class=lb><b>${c.nome}</b><span class="tag off" id="t_${c.mid}">DESLIGADA</span></div>
    </div>`).join('');
}
async function alterna(mid){
  const r=await (await fetch('/api/alterna',{method:'POST',body:JSON.stringify({mid})})).json();
  cams.forEach(c=>{
    const on=(c.mid===mid&&r.ativa);
    document.getElementById('c_'+c.mid).classList.toggle('on',on);
    const tg=document.getElementById('t_'+c.mid);
    tg.textContent=on?'ANALISANDO':'DESLIGADA';
    tg.className='tag '+(on?'on':'off');
    // recarrega o mjpeg: a camera desligada volta a mostrar a foto parada
    document.getElementById('i_'+c.mid).src='/quadro/'+c.mid+'.mjpg?'+Date.now();
  });
}
function cor(v,a,b){return v<a?'ok':v<b?'warn':'bad'}
function dur(s){
  if(s<60) return s.toFixed(0)+' s';
  if(s<3600) return (s/60).toFixed(1)+' min';
  return (s/3600).toFixed(1)+' h';
}
setInterval(async()=>{
  const s=await (await fetch('/api/stats')).json();
  document.getElementById('tab').innerHTML=`<tr>
    <th>camera</th><th class=n>capt</th><th class=n>proc</th>
    <th class=n>desc</th><th class=n>falhas</th><th class=n>pessoas</th>
    <th class=n>GESTOS</th><th class=n>total ms</th><th class=n>p90</th>
    <th class=n>${s.nuvem?'encode':'det'}</th><th class=n>${s.nuvem?'rede':'pose'}</th>
    <th class=n>${s.nuvem?'servidor':'ocup'}</th><th class=n>${s.nuvem?'KB':''}</th></tr>`+
    s.cameras.map(c=>`<tr class="${c.alerta?'alerta':''}">
      <td>${c.ativa?'<b>'+c.nome+'</b>':c.nome}</td>
      <td class=n>${c.capturados}</td><td class=n>${c.processados}</td>
      <td class="n ${c.descartados>0?'warn':''}">${c.descartados}</td>
      <td class="n ${c.falhas>0?'bad':''}">${c.falhas}</td>
      <td class=n>${c.pessoas}</td><td class="n big">${c.gestos}</td>
      <td class="n ${cor(c.ms,600,900)}">${c.ms.toFixed(0)}</td>
      <td class=n>${c.p90.toFixed(0)}</td>
      <td class=n>${(s.nuvem?c.ms_encode:c.ms_det).toFixed(0)}</td>
      <td class=n>${(s.nuvem?c.ms_rede:c.ms_pose).toFixed(0)}</td>
      <td class="n ${cor(c.ocupacao,70,100)}">${s.nuvem?(c.ms_det+c.ms_pose).toFixed(0):c.ocupacao.toFixed(0)+'%'}</td>
      <td class=n>${s.nuvem?c.kb.toFixed(0):''}</td>
    </tr>`).join('')+
    s.cameras.filter(c=>c.erro).map(c=>`<tr><td colspan=11 class=err>${c.nome}: ${c.erro}</td></tr>`).join('');

  document.getElementById('sis').innerHTML=`
   <tr><td>pipeline</td><td><b>${s.pipeline}</b> &middot; ${s.workers} worker(s) x ${s.threads} thread(s)</td></tr>
   <tr><td>cameras ligadas</td><td>${s.ativas} de ${s.cameras.length}</td></tr>
   <tr><td>capacidade usada do pool</td><td class="${cor(s.uso_pool,70,100)}">${s.uso_pool.toFixed(0)}%
       <span class=sub>(${s.inf_s.toFixed(2)} de ${s.teto_inf_s.toFixed(2)} inferencias/s)</span></td></tr>
   <tr><td>taxa de descarte</td><td class="${cor(s.pct_descarte,5,20)}">${s.pct_descarte.toFixed(1)}%</td></tr>
   <tr><td>defasagem por hora</td><td>${dur(s.atraso_h)}</td></tr>
   <tr><td><b>defasagem em 10 h de tracking</b></td><td class="${cor(s.atraso_10h/3600,0.5,2)}"><b>${dur(s.atraso_10h)}</b>
       <span class=sub>(${s.perdidos_10h.toFixed(0)} quadros nao analisados)</span></td></tr>
   <tr><td>temperatura</td><td class="${cor(s.temp,70,78)}">${s.temp} C</td></tr>
   <tr><td>throttling</td><td>${s.throttled}</td></tr>
   <tr><td>tempo ligado</td><td>${dur(s.uptime)}</td></tr>`;
},1500);
listar();
</script>"""


def aplica_config():
    """Liga e desliga cameras conforme a config. Idempotente de proposito: o
    OPS pode chamar quantas vezes quiser sem efeito colateral."""
    ligadas = []
    for mid, c in H.cams.items():
        quer = H.conf.ligada(mid)
        if quer and not c.ativa:
            c.liga()
        elif not quer and c.ativa:
            c.desliga()
        if quer:
            ligadas.append(mid)
    return ligadas


PAGINA_REVISAO = """<!doctype html><meta charset=utf-8><title>Revisao de gestos</title>
<style>
*{box-sizing:border-box}
body{background:#0b0d11;color:#e8eaed;font:14px system-ui,sans-serif;margin:0;padding:20px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:#8b93a7;font-size:13px;margin-bottom:16px}
.res{display:flex;gap:26px;flex-wrap:wrap;background:#12151c;border:1px solid #1e222b;
border-radius:10px;padding:14px 18px;margin-bottom:18px}
.res div{min-width:78px}
.res b{display:block;font-size:20px;font-weight:600}
.res span{color:#8b93a7;font-size:12px}
.grade{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.ev{background:#12151c;border:2px solid #1e222b;border-radius:10px;overflow:hidden}
.ev.sim{border-color:#4ade80}.ev.nao{border-color:#ff6b6b}
.ev img{width:100%;display:block;background:#000;cursor:zoom-in;aspect-ratio:4/3;object-fit:cover}
.tags{display:flex;gap:6px;padding:0 10px 6px;flex-wrap:wrap}
.tag{font-size:10px;font-weight:700;letter-spacing:.3px;padding:2px 6px;border-radius:4px}
.tag.conf{background:#3a1020;color:#ff6b8a}
.tag.seg{background:#1a2436;color:#7cc4ff}
.tag.tr{background:#1e1a2e;color:#a78bfa}
.filtros{display:flex;gap:8px;margin-bottom:14px}
.filtros button{background:#12151c;color:#8b93a7;border:1px solid #1e222b;border-radius:7px;
padding:7px 14px;cursor:pointer;font-size:13px}
.filtros button.on{border-color:#7c5cff;color:#e8eaed}
.meta{padding:8px 10px;font-size:12px;color:#8b93a7;display:flex;justify-content:space-between}
.m{font-weight:700}
.m.alto{color:#7c5cff}.m.quase{color:#fbbf24}
.bt{display:flex;gap:6px;padding:0 10px 10px}
.bt button{flex:1;background:#1a1d24;color:#e8eaed;border:1px solid #2a2f3a;
border-radius:6px;padding:7px;cursor:pointer;font-size:12px}
.bt button:hover{border-color:#7c5cff}
.bt .on-sim{background:#14361f;border-color:#4ade80}
.bt .on-nao{background:#3a1015;border-color:#ff6b6b}
.vazio{color:#8b93a7;padding:50px;text-align:center}
</style>
<h1>Revisao de gestos</h1>
<div class=sub>Historico de quem levantou as maos, com o esqueleto desenhado.
A foto e um <b>recorte ampliado da pessoa</b> &mdash; clique para ver o quadro inteiro.
Marque <b>era gesto</b> ou <b>nao era</b> e o limiar sai do dado em vez de palpite.</div>
<div class=res id=res></div>
<div class=filtros>
  <button id=f_tudo class=on onclick="filtra('tudo')">tudo</button>
  <button id=f_conf onclick="filtra('conf')">so confirmados (maos levantadas)</button>
  <button id=f_quase onclick="filtra('quase')">so os &quot;quase&quot;</button>
</div>
<div class=grade id=g></div>
<script>
let filtro='tudo';
function filtra(f){
  filtro=f;
  ['tudo','conf','quase'].forEach(x=>
    document.getElementById('f_'+x).classList.toggle('on',x===f));
  carrega();
}
async function carrega(){
  const d=await (await fetch('/api/revisao')).json();
  const r=d.resumo;
  document.getElementById('res').innerHTML=`
    <div><b>${r.capturadas}</b><span>capturadas</span></div>
    <div><b>${r.rotuladas}</b><span>rotuladas</span></div>
    <div><b style="color:#4ade80">${r.vp}</b><span>acertos</span></div>
    <div><b style="color:#ff6b6b">${r.fp}</b><span>falso positivo</span></div>
    <div><b style="color:#fbbf24">${r.fn}</b><span>falso negativo</span></div>
    <div><b>${r.precisao}%</b><span>precisao</span></div>
    <div><b>${r.recall}%</b><span>recall</span></div>
    <div><b>${r.f1}</b><span>F1 @ ${r.limiar}</span></div>
    ${r.sugestao?`<div><b style="color:#7c5cff">${r.sugestao.limiar}</b><span>limiar sugerido (F1 ${r.sugestao.f1})</span></div>`:''}`;
  let itens=d.itens;
  if(filtro==='conf') itens=itens.filter(x=>x.confirmado);
  if(filtro==='quase') itens=itens.filter(x=>!x.gesto);
  document.getElementById('g').innerHTML = itens.length ? itens.map(it=>`
    <div class="ev ${it.rotulo||''}" id="e_${it.id}">
      <img src="/revisao/${it.id}${it.tem_recorte?'_p':''}.jpg"
           title="clique para ver o quadro inteiro"
           onclick="window.open('/revisao/${it.id}.jpg')">
      <div class=tags>
        ${it.confirmado?'<span class="tag conf">MAOS LEVANTADAS</span>':''}
        ${it.segurando_s?`<span class="tag seg">segurou ${it.segurando_s.toFixed(0)}s</span>`:''}
        ${it.trilha?`<span class="tag tr">#${it.trilha}</span>`:''}
      </div>
      <div class=meta>
        <span>${it.cam} &middot; ${it.hora}</span>
        <span class="m ${it.gesto?'alto':'quase'}">${it.margem.toFixed(2)}</span>
      </div>
      <div class=bt>
        <button class="${it.rotulo==='sim'?'on-sim':''}" onclick="rot('${it.id}','sim')">era gesto</button>
        <button class="${it.rotulo==='nao'?'on-nao':''}" onclick="rot('${it.id}','nao')">nao era</button>
      </div>
    </div>`).join('') : '<div class=vazio>nada capturado ainda</div>';
}
async function rot(id,v){
  const el=document.getElementById('e_'+id);
  const atual=el.classList.contains(v)?'':v;
  await fetch('/api/rotular',{method:'POST',body:JSON.stringify({id,rotulo:atual})});
  carrega();
}
carrega(); setInterval(carrega,10000);
</script>"""


PAGINA_PESSOAS = """<!doctype html><meta charset=utf-8><title>Quem levantou as maos</title>
<style>
*{box-sizing:border-box}
body{background:#0b0d11;color:#e8eaed;font:14px system-ui,sans-serif;margin:0;padding:20px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:#8b93a7;font-size:13px;margin-bottom:16px}
.sub a{color:#7c5cff}
.grade{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.p{background:#12151c;border:1px solid #1e222b;border-radius:12px;overflow:hidden}
.p img{width:100%;display:block;aspect-ratio:3/4;object-fit:cover;background:#000;cursor:zoom-in}
.p .i{padding:9px 11px}
.p .q{font-weight:600;font-size:13px}
.p .h{color:#8b93a7;font-size:12px;margin-top:2px}
.b{display:inline-block;font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px;margin-top:6px}
.b.conf{background:#3a1020;color:#ff6b8a}
.b.inst{background:#2a2410;color:#fbbf24}
.filtros{display:flex;gap:8px;margin-bottom:14px;align-items:center;flex-wrap:wrap}
.filtros button{background:#12151c;color:#8b93a7;border:1px solid #1e222b;border-radius:7px;
padding:7px 14px;cursor:pointer;font-size:13px}
.filtros button.on{border-color:#7c5cff;color:#e8eaed}
.vazio{color:#8b93a7;padding:50px;text-align:center}
</style>
<h1>Quem levantou as maos</h1>
<div class=sub>Foto <b>sem o esqueleto</b>, para reconhecer a pessoa. Clique para o
quadro inteiro. &mdash; <a href="/revisao">ver com esqueleto e validar</a> &middot;
<a href="/">cameras</a></div>
<div class=filtros>
  <button id=p_conf class=on onclick="fil('conf')">confirmados (segurou o gesto)</button>
  <button id=p_tudo onclick="fil('tudo')">todos os registros</button>
  <span id=cont class=sub style="margin:0 0 0 auto"></span>
</div>
<div class=grade id=g></div>
<script>
let f='conf';
function fil(x){f=x;['conf','tudo'].forEach(k=>
  document.getElementById('p_'+k).classList.toggle('on',k===x));carrega();}
async function carrega(){
  const d=await (await fetch('/api/revisao')).json();
  let it=d.itens.filter(x=>x.tem_rosto);
  if(f==='conf') it=it.filter(x=>x.confirmado);
  document.getElementById('cont').textContent=`${it.length} registro(s)`;
  document.getElementById('g').innerHTML= it.length ? it.map(x=>`
    <div class=p>
      <img src="/revisao/${x.id}_r.jpg" onclick="window.open('/revisao/${x.id}.jpg')">
      <div class=i>
        <div class=q>${x.cam.replace('_camera',' &middot; cam ')}</div>
        <div class=h>${x.hora}</div>
        <span class="b ${x.confirmado?'conf':'inst'}">${x.confirmado
          ?'SEGUROU '+(x.segurando_s||0).toFixed(0)+'S':'INSTANTANEO'}</span>
      </div>
    </div>`).join('')
    : '<div class=vazio>nenhum registro com foto ainda &mdash; as fotos comecam a partir do proximo gesto</div>';
}
carrega(); setInterval(carrega,8000);
</script>"""


class H(BaseHTTPRequestHandler):
    cams = {}
    pool = None
    cfg = {}
    conf = None
    registro = None
    revisao = None
    alertas = []

    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/cameras"):
            return self._json([{"mid": c.mid, "nome": c.cam["nome"],
                                "res": c.cam["res"]} for c in H.cams.values()])
        if self.path.startswith("/api/stats"):
            return self._json(estatisticas())
        if self.path.startswith("/api/revisao"):
            if H.revisao is None:
                return self._json({"itens": [], "resumo": {}})
            itens = [dict(x, rotulo=H.revisao.rotulos.get(x["id"], ""))
                     for x in H.revisao.itens[:120]]
            return self._json({"itens": itens, "resumo": H.revisao.resumo()})
        if self.path.startswith("/revisao/") and self.path.endswith(".jpg"):
            nome = os.path.basename(self.path)
            cam = os.path.join(H.revisao.pasta, nome) if H.revisao else ""
            if not cam or not os.path.exists(cam):
                self.send_response(404); self.end_headers(); return
            b = open(cam, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if self.path.startswith("/pessoas"):
            b = PAGINA_PESSOAS.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if self.path.startswith("/revisao"):
            b = PAGINA_REVISAO.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if self.path.startswith("/api/alertas"):
            return self._json(list(reversed(H.alertas[-40:])))
        if self.path.startswith("/api/config"):
            # o OPS le isto para desenhar os switches
            d = dict(H.conf.d)
            d["arena"] = H.cfg.get("arena", "")
            d["quadras_detalhe"] = [
                {"quadra": q,
                 "ligada": bool(d["quadras"].get(q)),
                 "cameras": [{"mid": c.mid, "ligada": bool(d["cameras"].get(c.mid)),
                              "processando": c.ativa}
                             for c in H.cams.values()
                             if cfgmod.Config.quadra_de(c.mid) == q]}
                for q in sorted(d["quadras"])]
            return self._json(d)
        if self.path.startswith("/quadro/"):
            mid = self.path.split("/")[-1].split(".")[0]
            c = H.cams.get(mid)
            if not c:
                return self._json({"erro": "camera desconhecida"})
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=q")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                ultimo = None
                while True:
                    j = c.saida
                    if j is not None and j is not ultimo:
                        self.wfile.write(b"--q\r\nContent-Type: image/jpeg\r\n"
                                         b"Content-Length: " + str(len(j)).encode()
                                         + b"\r\n\r\n" + j + b"\r\n")
                        ultimo = j
                    time.sleep(0.3)
            except Exception:
                pass
            return
        b = PAGINA.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        d = json.loads(self.rfile.read(n) or b"{}")
        if self.path.startswith("/api/rotular"):
            if H.revisao is None:
                return self._json({"erro": "revisao desligada"})
            return self._json(H.revisao.rotula(d.get("id", ""), d.get("rotulo", "")))
        if self.path.startswith("/api/config"):
            # chamada pelo OPS: {"ativo":true} | {"quadra":"campo01","valor":true}
            # | {"camera":"campo01_camera01","valor":false} | {"nuvem":"https://..."}
            H.conf.define(**{k: v for k, v in d.items()
                             if k in ("ativo", "quadra", "camera", "valor",
                                      "nuvem", "webhook", "fps", "qualidade",
                                      "dur_gesto", "em_voo_max", "em_voo_min",
                                      "ajuste_s")})
            if "dur_gesto" in d:
                # aplica nas cameras ja rodando, sem reiniciar o servico
                H.cfg["dur_gesto"] = H.conf.d["dur_gesto"]
                for c in H.cams.values():
                    c.rast.dur_s = H.cfg["dur_gesto"]
            aplicada = aplica_config()
            return self._json({"ok": True, "config": H.conf.d,
                               "processando": aplicada})
        c = H.cams.get(d.get("mid"))
        if not c:
            return self._json({"erro": "camera desconhecida"})
        if c.ativa:
            c.desliga()
        elif H.cfg.get("nuvem"):
            c.liga()          # sem exclusividade: a Pi so encoda e envia
        else:
            # foco exclusivo: so a camera clicada captura e infere. As outras
            # param o ffmpeg por completo - com quatro capturas simultaneas a
            # Pi chegou a 84,7 C e throttling ativo, gastando CPU com cameras
            # que ninguem estava olhando.
            for o in H.cams.values():
                if o is not c:
                    o.desliga()
            c.liga()
        return self._json({"ok": True, "mid": c.mid, "ativa": c.ativa,
                           "desligadas": [o.mid for o in H.cams.values()
                                          if not o.ativa]})


def estatisticas():
    pool, cfg = H.pool, H.cfg
    linhas, cap_tot, proc_tot, desc_tot = [], 0, 0, 0
    for c in H.cams.values():
        m = c.m
        am = m["amostras"]
        p90 = float(np.percentile(am, 90)) if am else 0.0
        # ocupacao: quanto de cada segundo esta camera consome do pool
        linhas.append({
            "mid": c.mid, "nome": c.cam["nome"], "ativa": c.ativa,
            "capturados": m["capturados"], "processados": m["processados"],
            "descartados": m["descartados"], "pessoas": m["pessoas"],
            "gestos": m["gestos"], "ms": m["ms"], "p90": p90,
            "ms_det": m["ms_det"], "ms_pose": m["ms_pose"],
            "ms_encode": m["ms_encode"], "ms_rede": m["ms_rede"],
            "kb": m["kb"], "falhas": m["falhas"],
            "instantaneos": m["instantaneos"], "segurando": m["segurando"],
            "rastreio": c.rast.resumo(),
            "cronologia": c.cron.resumo() if c.cron else None,
            "alerta": (time.time() - m["ultimo_gesto"]) < 8,
            "ocupacao": (m["ms"] / 10.0) if c.ativa else 0.0,
            "erro": c.erro,
        })
        if c.ativa:
            cap_tot += m["capturados"]
            proc_tot += m["processados"]
            desc_tot += m["descartados"]

    nuvem = bool(H.cfg.get("nuvem"))
    up = max(time.time() - (pool.inicio if pool else H.cfg["t0"]), 1e-6)
    n_w = len(pool.workers) if pool else len(H.cams)
    # teto do pool: N workers em paralelo, cada um levando `ms` por inferencia
    ms_med = np.mean([x["ms"] for x in linhas if x["ativa"] and x["ms"]] or [0])
    teto = (n_w * 1000.0 / ms_med) if ms_med else 0.0
    inf_s = proc_tot / up
    uso = ((pool.ocupado_ms / 1000.0) / (up * n_w) * 100) if pool else 0.0

    # defasagem: o que a captura entrega menos o que o pool consegue analisar.
    # Como o quadro velho e DESCARTADO (nao enfileirado), isto nao vira atraso
    # de relogio - vira buraco de cobertura. A conversao para tempo responde
    # "quanto do periodo ficou sem analise".
    ativas = sum(1 for x in linhas if x["ativa"])
    demanda = ativas * cfg["fps"]                      # inferencias/s exigidas
    falta = max(demanda - (teto if teto else 0), 0.0)  # inferencias/s nao atendidas
    atraso_h = (falta / max(demanda, 1e-9)) * 3600 if demanda else 0.0
    return {
        "cameras": linhas,
        "pipeline": (pool.nome if pool else f"NUVEM {cfg['nuvem']}"),
        "nuvem": nuvem, "workers": n_w, "threads": cfg["threads"],
        "ativas": ativas,
        "inf_s": inf_s, "teto_inf_s": teto,
        "uso_pool": uso,
        "pct_descarte": (desc_tot / cap_tot * 100) if cap_tot else 0.0,
        "atraso_h": atraso_h,
        "atraso_10h": atraso_h * 10,
        "perdidos_10h": falta * 36000,
        "temp": round(temperatura(), 1), "throttled": throttled(),
        "uptime": up,
        "registros": H.registro.n if H.registro else 0,
        "alertas": len(H.alertas),
        "ultimo_alerta": (H.alertas[-1] if H.alertas else None),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=8090)
    ap.add_argument("--filtro", default="campo01,campo02")
    ap.add_argument("--det", default="yolo11n-256")
    ap.add_argument("--pose", default="rtmpose-s")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--max-pessoas", type=int, default=0, dest="max_pessoas")
    ap.add_argument("--largura", type=int, default=640)
    ap.add_argument("--altura", type=int, default=400)
    ap.add_argument("--fps", type=float, default=1.0)
    ap.add_argument("--nuvem", default="",
                    help="URL do endpoint remoto; vazio = inferencia local")
    ap.add_argument("--qualidade", type=int, default=75,
                    help="qualidade do JPEG enviado (so em modo nuvem)")
    ap.add_argument("--webhook", default="",
                    help="URL avisada IMEDIATAMENTE quando detecta o gesto")
    ap.add_argument("--config", default=None,
                    help="json de configuracao (padrao /etc/gravae/hands-up.json)")
    ap.add_argument("--revisao", default="gestos",
                    help="pasta das evidencias de gesto; vazio desliga")
    ap.add_argument("--registro", default="registro.jsonl",
                    help="arquivo JSONL com todos os tempos, por quadro")
    args = ap.parse_args()

    H.cfg = {}
    H.conf = cfgmod.Config(args.config)
    todas = cameras()
    H.conf.sincroniza([c["mid"] for c in todas])
    # a nuvem/webhook da config tem precedencia sobre a linha de comando:
    # e o OPS que manda, e ele escreve na config
    if H.conf.d.get("nuvem"):
        args.nuvem = H.conf.d["nuvem"]
    if H.conf.d.get("webhook"):
        args.webhook = H.conf.d["webhook"]
    args.fps = H.conf.d.get("fps", args.fps)
    args.qualidade = H.conf.d.get("qualidade", args.qualidade)
    H.cfg["dur_gesto"] = H.conf.d.get("dur_gesto", 2.0)
    for k, padrao in (("em_voo_max", 4), ("em_voo_min", 1), ("ajuste_s", 10.0)):
        H.cfg[k] = H.conf.d.get(k, padrao)
    sel = todas
    print(f"{len(sel)} cameras | ativo={H.conf.d['ativo']} | "
          f"config={H.conf.caminho}", flush=True)

    dev = {}
    try:
        dev = json.load(open("/etc/gravae/device.json"))
    except Exception:
        pass
    # `update` e nao atribuicao: `dur_gesto` ja foi posto em H.cfg acima e as
    # cameras leem dele ao serem criadas
    H.cfg.update({"threads": args.threads, "fps": args.fps,
                  "nuvem": args.nuvem, "t0": time.time(),
                  "webhook": args.webhook,
                  "api_key": dev.get("shinobiApiKey", ""),
                  # `deviceId` no device.json E o serial do Raspberry - e como
                  # o OPS reconhece esta Pi, no aviso do gesto.
                  "serial": str(dev.get("deviceId", ""))})
    if args.revisao:
        H.revisao = revmod.Revisao(os.path.join(DIR, args.revisao))
        print(f"revisao: {H.revisao.pasta} "
              f"({len(H.revisao.itens)} evidencias) -> /revisao", flush=True)
    if args.registro:
        H.registro = Registro(os.path.join(DIR, args.registro))
        print(f"registro: {os.path.join(DIR, args.registro)}", flush=True)
    if args.webhook:
        print(f"webhook do gesto: {args.webhook}", flush=True)

    if args.nuvem:
        # Cliente magro: a Pi so decodifica, encoda JPEG e envia. Nenhum
        # modelo e carregado aqui - e o ponto do modo nuvem.
        u = urlparse(args.nuvem)
        try:
            _cls = (http.client.HTTPSConnection if u.scheme == "https"
                    else http.client.HTTPConnection)
            c0 = _cls(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                      timeout=20)
            c0.request("GET", u.path or "/")
            info = json.loads(c0.getresponse().read())
            print(f"nuvem: {args.nuvem} | {info.get('modelo')} | "
                  f"{info.get('provider')}", flush=True)
        except Exception as e:
            print(f"nuvem INDISPONIVEL ({type(e).__name__}) - "
                  f"as cameras vao acumular falhas", flush=True)
        H.pool = None
        for c in sel:
            cam = Camera(c, args.largura, args.altura, args.fps)
            cam.nuvem = Nuvem(args.nuvem, args.qualidade,
                              arena=dev.get("shinobiGroupKey", ""),
                              camera=c["mid"])
            H.cams[c["mid"]] = cam
        print(f"modo nuvem a {args.fps} fps", flush=True)
    else:
        H.pool = Pool(args.workers, args.det, args.pose, args.threads,
                      args.max_pessoas)
        print(f"pool: {args.workers} worker(s) x {args.threads} thread(s) | "
              f"{H.pool.nome} | RAM ~{args.workers * 200} MB", flush=True)
        for c in sel:
            H.cams[c["mid"]] = Camera(c, args.largura, args.altura, args.fps)

        def despachante():
            while True:
                H.pool.despacha(list(H.cams.values()))
                time.sleep(0.08)
        threading.Thread(target=despachante, daemon=True).start()

    H.cfg["arena"] = dev.get("shinobiGroupKey", "")
    ligadas = aplica_config()
    print(f"processando agora: {ligadas or 'nenhuma (ligue pelo OPS)'}",
          flush=True)
    print(f"servico em http://0.0.0.0:{args.porta}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.porta), H).serve_forever()


if __name__ == "__main__":
    main()
