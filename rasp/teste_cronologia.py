"""Testes da ordem cronologica e do controlador de ritmo. Sem rede, sem camera.

  python teste_cronologia.py
"""
from __future__ import annotations

import threading
import time

import cronologia


def _cron(latencias, **kw):
    """Monta uma Cronologia cujo `enviar` dorme conforme `latencias[seq]`.

    latencias: dict seq -> segundos, ou 'falha', ou 'pendura'.
    """
    recebidos = []

    def enviar(q):
        lat = latencias.get(q.seq, 0.05)
        if lat == "falha":
            raise RuntimeError("boom")
        if lat == "pendura":
            time.sleep(30)
        time.sleep(lat)
        return {"seq": q.seq}, {}

    def consumir(q):
        recebidos.append(q)

    c = cronologia.Cronologia(enviar=enviar, consumir=consumir, **kw)
    return c, recebidos


def ok(cond, msg):
    print(f"  {'ok  ' if cond else 'FALHOU'} {msg}")
    if not cond:
        raise SystemExit(1)


# ---------------------------------------------------------------- ordem
def teste_ordem():
    print("1. resposta fora de ordem e entregue em ordem")
    # o seq 1 demora 1,2 s; 2 e 3 voltam antes dele
    c, rec = _cron({1: 1.2, 2: 0.1, 3: 0.1}, fps=100, em_voo_min=4,
                   em_voo_max=4, nome="ordem")
    c.inicia()
    for _ in range(3):
        c.oferece(None)
        time.sleep(0.02)
    time.sleep(2.0)
    c.para()
    seqs = [q.seq for q in rec]
    ok(seqs == [1, 2, 3], f"entregue em ordem: {seqs}")
    ok(any(q.fora_de_ordem for q in rec), "marcou os que chegaram adiantados")
    ok(c.janela.fora_de_ordem >= 2, f"contou fora_de_ordem={c.janela.fora_de_ordem}")


def teste_falha_nao_trava():
    print("2. falha no meio avanca sem travar")
    c, rec = _cron({2: "falha"}, fps=100, em_voo_min=4, em_voo_max=4,
                   nome="falha")
    c.inicia()
    for _ in range(4):
        c.oferece(None)
        time.sleep(0.02)
    time.sleep(1.2)
    c.para()
    seqs = [q.seq for q in rec]
    ok(seqs == [1, 3, 4], f"pulou o que falhou: {seqs}")
    ok(c.janela.abandonados >= 1, "contou o abandonado")


def teste_pendurada():
    print("3. requisicao pendurada nao trava a camera")
    c, rec = _cron({1: "pendura"}, fps=100, em_voo_min=4, em_voo_max=4,
                   espera_max=0.6, nome="pendura")
    c.inicia()
    for _ in range(3):
        c.oferece(None)
        time.sleep(0.02)
    time.sleep(2.0)
    c.para()
    seqs = [q.seq for q in rec]
    ok(seqs == [2, 3], f"seguiu sem o pendurado: {seqs}")


def teste_tardio_descartado():
    print("4. resposta que chega depois do seu lugar e descartada")
    j = cronologia.JanelaReordenacao(espera_max=0.2)
    for s in (1, 2):
        j.registra_envio(s)
    j.pronto(cronologia.Quadro(seq=2, t_captura=0))
    time.sleep(0.3)
    saida = j.drena()          # desiste do 1, emite o 2
    ok([q.seq for q in saida] == [2], f"emitiu {[q.seq for q in saida]}")
    aceitou = j.pronto(cronologia.Quadro(seq=1, t_captura=0))   # chega tarde
    ok(aceitou is False, "recusou o tardio")
    ok(j.tardios == 1, "contou como tardio")
    ok(j.drena() == [], "nao reinjetou fora de ordem")


def teste_monotonico():
    print("5. o consumo ve t_captura monotonico com latencia alternada")
    lat = {i: (3.0 if i % 2 else 0.2) for i in range(1, 7)}
    c, rec = _cron(lat, fps=100, em_voo_min=4, em_voo_max=4, nome="mono")
    c.inicia()
    for _ in range(6):
        c.oferece(None)
        time.sleep(0.05)
    time.sleep(4.5)
    c.para()
    ts = [q.t_captura for q in rec]
    ok(ts == sorted(ts), "t_captura crescente")
    ok([q.seq for q in rec] == sorted(q.seq for q in rec), "seq crescente")


# ------------------------------------------------------------- controlador
def teste_controlador():
    print("6. escada: sobe em_voo ate o teto, so entao mexe no intervalo")
    r = cronologia.Ritmo(fps=1.0, em_voo_min=1, em_voo_max=4, ajuste_s=0)
    t = 100.0
    passos = []
    for _ in range(6):
        r.pode_enviar(t, vagas_livres=0)      # sempre saturado
        t += 0.01
        passos.append(r.avalia(t))
    tipos = [p[0] for p in passos if p]
    ok(tipos[:3] == ["subiu_em_voo"] * 3, f"subiu em_voo primeiro: {tipos[:3]}")
    ok(r.em_voo == 4, f"chegou ao teto: em_voo={r.em_voo}")
    ok("subiu_intervalo" in tipos, "so depois mexeu no intervalo")
    ok(r.intervalo_alvo > r.base, f"intervalo subiu para {r.intervalo_alvo}")

    print("7. na volta: devolve o intervalo antes de encolher em_voo")
    volta = []
    for _ in range(8):
        t += 0.01
        volta.append(r.avalia(t))            # sem saturacao agora
    tipos = [p[0] for p in volta if p]
    i_int = tipos.index("baixou_intervalo") if "baixou_intervalo" in tipos else -1
    i_voo = tipos.index("baixou_em_voo") if "baixou_em_voo" in tipos else 99
    ok(i_int >= 0 and i_int < i_voo, f"intervalo antes de em_voo: {tipos}")
    ok(abs(r.intervalo_alvo - r.base) < 1e-6,
       f"intervalo voltou ao piso ({r.base}): {r.intervalo_alvo}")


def teste_decimacao_nao_cria_buraco():
    print("8. quadro decimado pelo ritmo nao consome seq")
    c, rec = _cron({}, fps=2.0, em_voo_min=4, em_voo_max=4, nome="decim")
    t = 1000.0
    aceitos = 0
    for i in range(10):
        if c.oferece(None, agora=t + i * 0.1):   # oferece a 10 fps, ritmo e 2
            aceitos += 1
    ok(aceitos < 10, f"decimou: aceitou {aceitos} de 10")
    ok(c.ritmo.decimados > 0, "contou como decimado, nao como sem vaga")
    ok(c.ritmo.sem_vaga == 0, "nenhum foi por falta de vaga")
    ok(c._seq == aceitos, f"seq denso: _seq={c._seq}, aceitos={aceitos}")


if __name__ == "__main__":
    for f in (teste_ordem, teste_falha_nao_trava, teste_pendurada,
              teste_tardio_descartado, teste_monotonico, teste_controlador,
              teste_decimacao_nao_cria_buraco):
        f()
    print("\ntodos passaram")
