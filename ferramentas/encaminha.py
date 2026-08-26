"""Encaminha uma porta da Pi da arena para 127.0.0.1 desta maquina.

Existe porque o `ssh -L` classico nao serve aqui: a autenticacao e por SENHA e
o ssh so a pede num terminal interativo - num processo de fundo ele falha com
"Permission denied". O paramiko autentica programaticamente e ainda abre os
canais `direct-tcpip` que fazem o encaminhamento.

O caminho completo tem dois saltos:

    navegador -> 127.0.0.1:8090
              -> paramiko (canal direct-tcpip)
              -> sshd da Pi
              -> cloudflared TCP (rota -ssh do Named Tunnel)
              -> 127.0.0.1:8090 na Pi (o painel)

  python encaminha.py                      # 8090 -> painel
  python encaminha.py --remota 8080        # 8080 -> Shinobi da arena
"""
from __future__ import annotations

import argparse
import select
import socketserver
import threading

from arena import Arena


def servidor(transporte, porta_local, host_remoto, porta_remota):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                canal = transporte.open_channel(
                    "direct-tcpip", (host_remoto, porta_remota),
                    self.request.getpeername())
            except Exception as e:
                print(f"  canal recusado: {e}")
                return
            if canal is None:
                return
            try:
                while True:
                    r, _, _ = select.select([self.request, canal], [], [], 1.0)
                    if self.request in r:
                        d = self.request.recv(65536)
                        if not d:
                            break
                        canal.sendall(d)
                    if canal in r:
                        d = canal.recv(65536)
                        if not d:
                            break
                        self.request.sendall(d)
            except Exception:
                pass
            finally:
                canal.close()
                self.request.close()

    class Srv(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    return Srv(("127.0.0.1", porta_local), Handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arena", default="arenapatamares")
    ap.add_argument("--local", type=int, default=8090)
    ap.add_argument("--remota", type=int, default=8090)
    args = ap.parse_args()

    # Reconecta sozinho: o caminho tem dois saltos (cloudflared + sshd) e
    # qualquer um dos dois cai por ociosidade, troca de rede ou reinicio do
    # cloudflared da Pi. Sem o laco, o painel "some" do navegador sem aviso.
    while True:
        srv = None
        try:
            with Arena(args.arena) as a:
                srv = servidor(a.cli.get_transport(), args.local,
                               "127.0.0.1", args.remota)
                print(f"http://127.0.0.1:{args.local}  ->  {args.arena}:"
                      f"{args.remota}   [conectado]", flush=True)
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                while True:
                    # keepalive: sem trafego o sshd derruba a sessao
                    a.run("true", timeout=25)
                    threading.Event().wait(30)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  queda ({type(e).__name__}), religando em 5s...", flush=True)
        finally:
            if srv:
                srv.shutdown()
                srv.server_close()
        threading.Event().wait(5)


if __name__ == "__main__":
    main()
