"""Presenca de gente pela IA da propria camera (Intelbras/Dahua).

POR QUE
    O hands-up so tem o que fazer com gente em quadra, mas ate aqui cada
    camera ligada mandava 1 quadro/s para a nuvem o dia inteiro, com a quadra
    vazia ou nao. As cameras da linha "-IA" da Intelbras (ex. VIP-3430-D-IA, as
    tres do Fit Club) ja detectam humano sozinhas e publicam o evento
    `SmartMotionHuman`. Ouvindo esse evento, a Pi so captura - e so paga a
    nuvem - enquanto ha alguem na quadra.

COMO A PI OUVE A CAMERA
    A camera nao chama ninguem. E a Pi que abre UMA conexao HTTP longa,
    `eventManager.cgi?action=attach`, e a camera escreve nela uma linha por
    evento, mais um `Heartbeat` a cada ~5 s. Nao e polling: a conexao fica
    aberta e o evento chega na hora. Medido no Fit Club (10/09/2026, 170 s,
    tres cameras): nenhuma derrubou a conexao; a da quadra com gente mandou
    Start/Stop a cada 10-50 s (cada ciclo de 8 a 38 s); as vazias, so batida.

A CAMERA SO VE MOVIMENTO
    SmartMotionHuman e deteccao de MOVIMENTO filtrada por humano: gente parada
    nao gera evento. Por isso o evento nao desliga nada sozinho - ele renova um
    prazo (`espera_s`, 10 min por padrao). E, com a captura rodando, as pessoas
    que o NOSSO detector ve tambem renovam: a camera acorda o hands-up, quem
    ve gente mantem.

NA DUVIDA, CONSIDERA QUE TEM GENTE
    Conexao caiu, camera nao responde, servico acabou de subir: em todos esses
    casos a Pi nao sabe, e nao saber vira "tem gente" pelo prazo inteiro.
    Perder um gesto custa mais caro que alguns minutos de nuvem.

    Camera sem IA, ou com a deteccao de humano DESLIGADA na propria camera,
    nunca pausa: sem evento nenhum chegando, o gatilho deixaria o hands-up
    dormindo para sempre sem ninguem perceber.
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.request

#: Evento de humano das Intelbras/Dahua com IA.
CODIGO = "SmartMotionHuman"
#: Quanto tempo o modo segue de pe depois da ultima pessoa vista.
ESPERA_S = 600
ESPERA_MIN_S, ESPERA_MAX_S = 60, 3600
#: Sem conexao com a camera por mais que isto = cego = considera que tem gente.
#: Curto o bastante para nao perder um jogo; longo o bastante para uma
#: reconexao normal (a camera de arena cai e volta o tempo todo) nao contar.
CARENCIA_S = 30
#: Timeout de leitura do stream de eventos. A batida vem a cada ~5 s; quatro
#: perdidas seguidas e a conexao esta morta mesmo sem o TCP ter percebido.
LEITURA_S = 20
#: Pessoa vista pelo NOSSO detector so renova o prazo se for CONSISTENTE:
#: tantos quadros com gente dentro da janela. Medido no Fit Club (10/09,
#: quadras vazias, 1 fps): 6 quadros isolados com "1 pessoa" em 3 min, nunca
#: dois seguidos. Sem o filtro, um falso positivo a cada 10 min seguraria a
#: quadra vazia ligada para sempre. Jogador de verdade aparece em quase todo
#: quadro; quem passa atras da quadra por alguns segundos tambem conta.
DETECTOR_MIN_QUADROS = 3
DETECTOR_JANELA_S = 10.0
#: Com que frequencia reconfirmar o que a camera oferece.
RESONDA_OK_S = 6 * 3600       # ja sabemos a resposta: so para pegar mudanca
RESONDA_FALHA_S = 300         # camera nao respondeu: tenta de novo logo

#: Como a camera esta sendo usada, visto do painel. O OPS desenha a partir
#: disto - e o contrato, nao mude os nomes sem mudar la.
MODOS = {
    "desligada": "chave desligada no OPS",
    "manual":    "ativa sempre (ativacao manual)",
    "sem_ia":    "ativa sempre: camera sem IA de humano utilizavel",
    "gente":     "ativa: gente em quadra (ou dentro do prazo)",
    "sem_sinal": "ativa: perdeu contato com a IA da camera",
    "pausada":   "pausada: ninguem em quadra ha mais que o prazo",
    "forcada":   "forcada pelo painel local da Pi (depuracao)",
}


# ------------------------------------------------------------------ decisao
def usavel(ia):
    """A camera pode governar o hands-up? So se TEM a deteccao de humano E ela
    esta LIGADA. Na duvida (nao sondou, nao respondeu), nao - e ai a camera
    fica sempre ativa, que e o comportamento de antes."""
    return bool(ia and ia.get("suporta") is True and ia.get("ligada") is True)


def decide(ligada, gatilho, ia, presenca, agora):
    """-> (capturar, modo). Pura: toda a regra do gatilho mora aqui.

    `ligada`   chaves do OPS (ativo + quadra + camera)
    `gatilho`  "manual" | "ia"
    `ia`       resultado da `sonda` desta camera (ou None)
    `presenca` a `Presenca` desta camera (ou None se ninguem esta ouvindo)
    """
    if not ligada:
        return False, "desligada"
    if gatilho != "ia":
        return True, "manual"
    if not usavel(ia):
        return True, "sem_ia"
    if presenca is None or presenca.cego(agora):
        return True, "sem_sinal"
    if presenca.tem_gente(agora):
        return True, "gente"
    return False, "pausada"


class Presenca:
    """Se ha gente na quadra, so com o que a camera disse e quando.

    Sem rede e sem relogio proprio: todo metodo recebe `agora`, entao da para
    testar o dia inteiro em milissegundos (ver teste_gatilho.py).
    """

    def __init__(self, espera_s=ESPERA_S, carencia_s=CARENCIA_S, agora=None):
        t = time.time() if agora is None else agora
        self.espera_s, self.carencia_s = espera_s, carencia_s
        self.lock = threading.Lock()
        self.em_curso = set()       # indices com Start sem Stop ainda
        # Nasce com o prazo cheio: acabou de subir, nao sabe se tem gente.
        self.ultimo = t
        self.conectado = False
        self.caiu_em = t            # sem conexao desde
        self.visto = {}             # fonte -> instante da ultima pessoa vista
        self.contagem = {}          # fonte -> quantas vezes
        self._acertos = []          # quadros do detector com gente, na janela

    # -- o que chega da camera
    def evento(self, acao, indice, agora):
        with self.lock:
            if acao == "Start":
                self.em_curso.add(indice)
            elif acao == "Stop":
                self.em_curso.discard(indice)
            self._viu(agora, "camera")

    def batida(self, agora):
        """Heartbeat. Com humano em curso, o prazo corre a partir de agora:
        um Start longo (jogo corrido) nao pode vencer o prazo no meio."""
        with self.lock:
            if self.em_curso:
                self.ultimo = max(self.ultimo, agora)

    def conectou(self, agora, em_curso=()):
        with self.lock:
            # Ficou cego mais que a carencia: o que houve nesse intervalo e
            # desconhecido, e desconhecido conta como gente. Uma reconexao
            # rapida NAO renova - senao uma camera que derruba o stream a cada
            # minuto nunca deixaria o hands-up pausar.
            if not self.conectado and agora - self.caiu_em > self.carencia_s:
                self.ultimo = max(self.ultimo, agora)
            self.conectado = True
            # estado atual perguntado a camera: quem esta parado agora nao vai
            # gerar Start ate se mexer
            self.em_curso = set(em_curso)
            if self.em_curso:
                self.ultimo = max(self.ultimo, agora)

    def caiu(self, agora):
        with self.lock:
            if self.em_curso:
                # tinha gente quando perdemos a camera
                self.ultimo = max(self.ultimo, agora)
            # um Start sem o Stop correspondente ficaria preso para sempre
            self.em_curso.clear()
            if self.conectado:
                self.conectado = False
                self.caiu_em = agora

    # -- o que chega do nosso detector
    def detector(self, agora):
        """Um quadro analisado COM gente. `agora` e o instante da captura, e os
        quadros chegam em ordem (a Cronologia garante). So renova o prazo com
        `DETECTOR_MIN_QUADROS` dentro de `DETECTOR_JANELA_S`; os isolados sao
        contados a parte, para dar para ver quanto falso positivo houve."""
        with self.lock:
            self._acertos = [t for t in self._acertos
                             if agora - t <= DETECTOR_JANELA_S] + [agora]
            if len(self._acertos) >= DETECTOR_MIN_QUADROS:
                self._viu(agora, "detector")
            else:
                self.contagem["detector_isolado"] = (
                    self.contagem.get("detector_isolado", 0) + 1)

    def viu(self, agora, fonte="detector"):
        with self.lock:
            self._viu(agora, fonte)

    def _viu(self, agora, fonte):
        self.ultimo = max(self.ultimo, agora)
        self.visto[fonte] = max(self.visto.get(fonte, 0.0), agora)
        self.contagem[fonte] = self.contagem.get(fonte, 0) + 1

    # -- consultas
    def cego(self, agora):
        return not self.conectado and agora - self.caiu_em > self.carencia_s

    def tem_gente(self, agora):
        return bool(self.em_curso) or agora - self.ultimo < self.espera_s

    def estado(self, agora):
        with self.lock:
            v = max(self.visto.values(), default=None)
            return {
                "conectado": self.conectado,
                "em_curso": bool(self.em_curso),
                # ultima pessoa vista por QUALQUER fonte (camera ou detector)
                "ultima_pessoa": round(v, 1) if v else None,
                "ha_s": round(agora - v) if v else None,
                # quanto falta para pausar se ninguem aparecer
                "restante_s": (self.espera_s if self.em_curso else
                               max(0, round(self.espera_s - (agora - self.ultimo)))),
                "espera_s": self.espera_s,
                "eventos": dict(self.contagem),
            }


# ------------------------------------------------------------------ camera
def interpreta(linha):
    """`Code=SmartMotionHuman;action=Start;index=0;data={` -> dict, ou None.

    O `data={` abre um JSON de varias linhas (hora, UTC, nome) que nao usamos;
    as linhas seguintes nao comecam com `Code=` e caem fora aqui.
    """
    if not linha.startswith("Code="):
        return None
    campos = {}
    for parte in linha.split(";"):
        k, sep, v = parte.partition("=")
        if not sep:
            continue
        k = k.strip()
        if k == "data":
            break
        campos[k] = v.strip()
    codigo, acao = campos.get("Code"), campos.get("action")
    if not codigo or not acao:
        return None
    try:
        indice = int(campos.get("index", 0))
    except ValueError:
        indice = 0
    return {"codigo": codigo, "acao": acao, "indice": indice}


def _kv(texto):
    """Corpo `chave=valor` por linha (formato das CGIs Dahua) -> dict."""
    out = {}
    for ln in texto.replace("\r", "").split("\n"):
        k, sep, v = ln.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def abridor(host, usuario, senha, porta=80):
    """urllib com Digest (o que as Intelbras pedem) e Basic (firmware antigo)."""
    raiz = f"http://{host}:{porta}/"
    senhas = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    senhas.add_password(None, raiz, usuario, senha)
    return urllib.request.build_opener(
        urllib.request.HTTPDigestAuthHandler(senhas),
        urllib.request.HTTPBasicAuthHandler(senhas))


def _get(op, url, timeout):
    """-> (status_http, corpo). Status 0 = nem respondeu. Nunca levanta."""
    try:
        with op.open(url, timeout=timeout) as r:
            return r.status, r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, type(e).__name__


def sonda(host, usuario, senha, porta=80, timeout=6):
    """O que a camera oferece, perguntado a ela. Nunca levanta excecao.

    `suporta`  True/False, ou None se nao deu para saber (camera fora do ar,
               credencial recusada)
    `ligada`   a deteccao de humano esta habilitada NA camera
    `motivo`   frase para o painel quando nao da para usar

    Tres perguntas, ~1 s por camera no Fit Club: o modelo (e se a API
    Intelbras/Dahua existe), a lista de eventos que ela sabe publicar, e a
    configuracao do SmartMotionDetect.
    """
    ia = {"suporta": None, "ligada": None, "modelo": None, "evento": CODIGO,
          "motivo": None, "sondado_em": round(time.time(), 1)}
    if not host:
        ia.update(suporta=False, motivo="monitor sem host no Shinobi")
        return ia
    base = f"http://{host}:{porta}/cgi-bin/"
    op = abridor(host, usuario, senha, porta)

    st, corpo = _get(op, base + "magicBox.cgi?action=getDeviceType", timeout)
    if st == 0:
        ia["motivo"] = f"camera nao respondeu em http:{porta} ({corpo})"
        return ia
    if st == 401:
        ia["motivo"] = "camera recusou a credencial do Shinobi"
        return ia
    if st != 200:
        ia.update(suporta=False,
                  motivo=f"sem API Intelbras/Dahua (HTTP {st}); outro fabricante?")
        return ia
    ia["modelo"] = _kv(corpo).get("type")

    st, corpo = _get(op, base + "eventManager.cgi?action=getExposureEvents", timeout)
    eventos = ({v for k, v in _kv(corpo).items() if k.startswith("events")}
               if st == 200 else None)
    st2, corpo2 = _get(op, base + "configManager.cgi?action=getConfig"
                               "&name=SmartMotionDetect", timeout)
    smd = _kv(corpo2) if st2 == 200 else {}
    tem_smd = any(k.startswith("table.SmartMotionDetect") for k in smd)
    # firmware sem getExposureEvents: a existencia da config ja responde
    suporta = (CODIGO in eventos) if eventos else tem_smd
    if not suporta:
        ia.update(suporta=False,
                  motivo="camera sem deteccao de humano (nao publica SmartMotionHuman)")
        return ia
    ia["suporta"] = True
    habilitada = smd.get("table.SmartMotionDetect[0].Enable") == "true"
    humano = smd.get("table.SmartMotionDetect[0].ObjectTypes.Human") == "true"
    ia["ligada"] = habilitada and humano
    if not tem_smd:
        ia["ligada"] = None
        ia["motivo"] = "nao consegui ler a configuracao de humano da camera"
    elif not ia["ligada"]:
        ia["motivo"] = ("deteccao de humano desligada na camera "
                        "(SmartMotionDetect: Enable/ObjectTypes.Human)")
    return ia


def em_curso_agora(op, host, porta=80, timeout=6):
    """Indices com humano em curso AGORA. Vazio se nenhum ou se falhou.

    `getEventIndexes` responde `channels[0]=0` com gente e `Error:No Events`
    sem ninguem - conferido contra o proprio attach no Fit Club.
    """
    st, corpo = _get(op, f"http://{host}:{porta}/cgi-bin/eventManager.cgi"
                         f"?action=getEventIndexes&code={CODIGO}", timeout)
    if st != 200:
        return set()
    out = set()
    for k, v in _kv(corpo).items():
        if k.startswith("channels"):
            try:
                out.add(int(v))
            except ValueError:
                pass
    return out


class Vigia:
    """Uma conexao de eventos aberta com a camera, alimentando uma Presenca.

    Reconecta sozinha, com espera crescente ate 60 s. Parar e so sinalizar:
    a batida chega a cada ~5 s e o laco confere o sinal a cada linha.
    """

    def __init__(self, nome, host, usuario, senha, presenca, porta=80):
        self.nome, self.host, self.porta = nome, host, porta
        self.presenca = presenca
        self.op = abridor(host, usuario, senha, porta)
        self.url = (f"http://{host}:{porta}/cgi-bin/eventManager.cgi?action=attach"
                    f"&codes=%5B{CODIGO}%5D&heartbeat=5")
        self.erro = None
        self.conexoes = 0
        self._parar = threading.Event()
        self._resp = None
        self._thread = threading.Thread(target=self._roda, daemon=True,
                                        name=f"vigia-{nome}")

    def inicia(self):
        self._thread.start()
        return self

    def para(self):
        self._parar.set()
        r = self._resp
        if r is not None:
            try:
                r.close()           # destrava um readline parado
            except Exception:
                pass

    def _roda(self):
        espera = 5
        while not self._parar.is_set():
            try:
                self._resp = self.op.open(self.url, timeout=LEITURA_S)
                self.presenca.conectou(
                    time.time(), em_curso_agora(self.op, self.host, self.porta))
                self.conexoes += 1
                self.erro, espera = None, 5
                while not self._parar.is_set():
                    ln = self._resp.readline()
                    if not ln:
                        raise ConnectionError("a camera fechou o stream de eventos")
                    s = ln.decode("utf-8", "replace").strip()
                    if s == "Heartbeat":
                        self.presenca.batida(time.time())
                        continue
                    ev = interpreta(s)
                    if ev and ev["codigo"] == CODIGO:
                        self.presenca.evento(ev["acao"], ev["indice"], time.time())
            except urllib.error.HTTPError as e:
                self.erro = ("camera recusou a credencial" if e.code == 401
                             else f"HTTP {e.code} no attach")
            except Exception as e:
                if not self._parar.is_set():
                    self.erro = f"{type(e).__name__}: {e}"[:120]
            finally:
                r, self._resp = self._resp, None
                if r is not None:
                    try:
                        r.close()
                    except Exception:
                        pass
                self.presenca.caiu(time.time())
            self._parar.wait(espera)
            espera = min(espera * 2, 60)
