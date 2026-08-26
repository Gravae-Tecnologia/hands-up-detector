"""Acesso a Pi de uma arena Gravae pelo Cloudflare Named Tunnel.

As Pis das arenas nao tem IP alcancavel: o `cloudflared` da arena publica tres
rotas (`{sub}.gravae.io` -> Shinobi 8080, `{sub}-agent` -> 8888, `{sub}-ssh`
-> 22). Para chegar no SSH e preciso subir um proxy TCP local que fala com a
rota `-ssh` e apontar o paramiko para ele - por isso este modulo existe em vez
de um `paramiko.connect(ip)` direto.

  export ARENA_PASS=...            # a senha nunca vive no codigo
  from arena import Arena
  with Arena("arenapatamares") as a:
      print(a.run("uptime"))
      a.put("modelo.onnx", "levanta/modelo.onnx")
"""
from __future__ import annotations

import os
import socket
import subprocess
import time

import paramiko

DIR = os.path.dirname(os.path.abspath(__file__))
CLOUDFLARED = os.path.join(DIR, "bin", "cloudflared.exe")


class Arena:
    def __init__(self, sub, usuario=None, senha=None, porta_local=0,
                 dominio="gravae.io"):
        # Credencial nunca no codigo: vem do ambiente. Este repositorio e
        # publico dentro da organizacao e a mesma senha vale para todo o
        # parque de Raspberries.
        usuario = usuario or os.environ.get("ARENA_USER", "gravae")
        senha = senha or os.environ.get("ARENA_PASS")
        if not senha:
            raise SystemExit("defina ARENA_PASS no ambiente")
        self.host = f"{sub}-ssh.{dominio}"
        self.usuario, self.senha = usuario, senha
        # porta 0 -> pede uma livre ao SO, para varias sessoes conviverem
        self.porta = porta_local or self._porta_livre()
        self.proc = None
        self.cli = None

    @staticmethod
    def _porta_livre():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def __enter__(self):
        self.proc = subprocess.Popen(
            [CLOUDFLARED, "access", "tcp", "--hostname", self.host,
             "--url", f"127.0.0.1:{self.porta}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                s = socket.create_connection(("127.0.0.1", self.porta), timeout=1)
                s.close()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError(f"proxy do cloudflared nao subiu para {self.host}")
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.cli.connect("127.0.0.1", port=self.porta, username=self.usuario,
                         password=self.senha, timeout=25, allow_agent=False,
                         look_for_keys=False)
        return self

    def run(self, cmd, timeout=120):
        i, o, e = self.cli.exec_command(cmd, timeout=timeout)
        return (o.read() + e.read()).decode("utf-8", "replace")

    def put(self, local, remoto):
        s = self.cli.open_sftp()
        try:
            # o cwd do sftp e /home/gravae; caminho relativo evita a conversao
            # de path do MSYS, que transformaria /home/... em C:/Program Files/...
            partes = remoto.split("/")
            acc = ""
            for d in partes[:-1]:
                acc = f"{acc}/{d}" if acc else d
                try:
                    s.mkdir(acc)
                except IOError:
                    pass
            s.put(local, remoto)
            return s.stat(remoto).st_size
        finally:
            s.close()

    def __exit__(self, *a):
        try:
            if self.cli:
                self.cli.close()
        finally:
            if self.proc:
                self.proc.terminate()


if __name__ == "__main__":
    import sys
    sub = sys.argv[1] if len(sys.argv) > 1 else "arenapatamares"
    cmd = " ".join(sys.argv[2:]) or "uptime"
    with Arena(sub) as a:
        print(a.run(cmd))
