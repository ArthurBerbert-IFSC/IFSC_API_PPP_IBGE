#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Janela (Tkinter) para o PPP em sessões curtas.

Cada etapa roda numa thread separada; a janela mostra o andamento (barra, contador,
tempo decorrido, tempo restante e a contagem regressiva das esperas) e o registro.
Rodar:  python ppp_gui.py      (no Windows, clique duas vezes em rodar.bat)
"""

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import ppp_sessoes as nucleo

TITULO = "PPP em sessões curtas · IBGE-PPP × RBMC"
GIRA = "◐◓◑◒"


def abrir(caminho):
    caminho = str(caminho)
    if not os.path.exists(caminho):
        messagebox.showinfo(TITULO, f"Ainda não existe:\n{caminho}")
        return
    if sys.platform.startswith("win"):
        os.startfile(caminho)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", caminho])
    else:
        subprocess.Popen(["xdg-open", caminho])


def hms(segundos):
    segundos = max(0, int(segundos))
    return f"{segundos // 3600:02d}:{segundos % 3600 // 60:02d}:{segundos % 60:02d}"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(TITULO)
        self.geometry("1060x760")
        self.minsize(960, 620)
        self.fila = queue.Queue()
        self.thread = None
        self.inicio_tarefa = None
        self.inicio_etapa = None
        self.ultimo_prog = None
        self.espera = None
        self.passo_giro = 0
        self.depois = None          # o que fazer quando a tarefa atual terminar
        self._estilo()
        self._montar()
        nucleo.ouvir(lambda tipo, dados: self.fila.put((tipo, dados)))
        self.protocol("WM_DELETE_WINDOW", self._fechar)
        self.after(100, self._processar_fila)
        self.after(250, self._relogio)
        self._log(f"Pasta de trabalho: {nucleo.PASTA.parent}")

    # ------------------------------------------------------------ interface
    def _estilo(self):
        st = ttk.Style(self)
        if "vista" in st.theme_names():
            st.theme_use("vista")
        elif "clam" in st.theme_names():
            st.theme_use("clam")
        st.configure("Titulo.TLabel", font=("Segoe UI", 13, "bold"))
        st.configure("Etapa.TLabel", font=("Segoe UI", 11, "bold"))
        st.configure("Grande.TLabel", font=("Segoe UI", 10))
        st.configure("Num.TLabel", font=("Segoe UI", 16, "bold"))
        st.configure("Acao.TButton", padding=(8, 5))
        st.configure("Horizontal.TProgressbar", thickness=14)
        if st.theme_use() == "clam":
            st.configure("Horizontal.TProgressbar", background="#2a78d6", troughcolor="#e6e6e6",
                         bordercolor="#c8c8c8", lightcolor="#2a78d6", darkcolor="#2a78d6")

    def _montar(self):
        cfg = nucleo.config_padrao()
        raiz = ttk.Frame(self, padding=12)
        raiz.pack(fill="both", expand=True)
        ttk.Label(raiz, text=TITULO, style="Titulo.TLabel").pack(anchor="w")
        ttk.Label(raiz, text="Sessões de 1 a 4 h (manhã e tarde) e 24 h da RBMC, processadas no IBGE-PPP "
                             "e comparadas à coordenada oficial.", foreground="#555").pack(anchor="w", pady=(0, 8))

        # --- configuração
        fc = ttk.LabelFrame(raiz, text="Configuração", padding=8)
        fc.pack(fill="x")
        self.v_email = tk.StringVar(value=cfg["email"])
        self.v_dias = tk.StringVar(value=" ".join(cfg["dias"]))
        self.v_est = tk.StringVar(value=" ".join(cfg["estacoes"]))
        self.v_dur = tk.StringVar(value=" ".join(str(d) for d in cfg["duracoes"]))
        campos = [("E-mail para o IBGE-PPP", self.v_email, 36), ("Dias (AAAA-MM-DD)", self.v_dias, 36),
                  ("Estações RBMC", self.v_est, 36), ("Durações (h)", self.v_dur, 12)]
        for i, (rot, var, larg) in enumerate(campos):
            ttk.Label(fc, text=rot).grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 6), pady=3)
            ttk.Entry(fc, textvariable=var, width=larg).grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="we",
                                                           padx=(0, 18), pady=3)
        fc.columnconfigure(1, weight=1)
        fc.columnconfigure(3, weight=1)
        lp = ttk.Frame(fc)
        lp.grid(row=2, column=0, columnspan=4, sticky="we", pady=(4, 0))
        ttk.Button(lp, text="Coordenadas oficiais",
                   command=lambda: abrir(nucleo.PASTA / "coordenadas_oficiais.csv")).pack(side="right")
        ttk.Button(lp, text="Abrir pasta", command=lambda: abrir(nucleo.PASTA.parent)).pack(side="right", padx=6)
        ttk.Label(lp, text=f"Pasta: {nucleo.PASTA.parent}", foreground="#555").pack(side="left", fill="x",
                                                                                    expand=True)

        # --- etapas
        fe = ttk.LabelFrame(raiz, text="Etapas", padding=8)
        fe.pack(fill="x", pady=8)
        self.botoes = []
        self.b_parar = ttk.Button(fe, text="■  Parar", command=self.parar, state="disabled", style="Acao.TButton",
                                  width=10)
        self.b_parar.pack(side="right")

        etapas = [("1  Baixar RINEX", self.baixar), ("2  Cortar sessões", self.cortar),
                  ("3  Testar API", self.testar), ("4  Enviar ao IBGE-PPP", self.enviar),
                  ("5  Analisar", self.analisar)]
        for texto, cmd in etapas:
            b = ttk.Button(fe, text=texto, command=cmd, style="Acao.TButton")
            b.pack(side="left", padx=(0, 6))
            self.botoes.append(b)
        self.b_tudo = ttk.Button(fe, text="▶  Fazer tudo", command=self.tudo, style="Acao.TButton")
        self.b_tudo.pack(side="left", padx=(12, 6))
        self.botoes.append(self.b_tudo)

        # --- andamento
        fa = ttk.LabelFrame(raiz, text="Andamento", padding=8)
        fa.pack(fill="x")
        linha = ttk.Frame(fa)
        linha.pack(fill="x")
        self.l_giro = ttk.Label(linha, text=" ", style="Etapa.TLabel", width=2)
        self.l_giro.pack(side="left")
        self.l_etapa = ttk.Label(linha, text="Parado", style="Etapa.TLabel")
        self.l_etapa.pack(side="left")
        self.l_cont = ttk.Label(linha, text="", style="Grande.TLabel")
        self.l_cont.pack(side="right")
        self.barra = ttk.Progressbar(fa, mode="determinate", maximum=1)
        self.barra.pack(fill="x", pady=6)
        linha2 = ttk.Frame(fa)
        linha2.pack(fill="x")
        self.l_tempo = ttk.Label(linha2, text="", foreground="#333")
        self.l_tempo.pack(side="left")
        self.l_espera = ttk.Label(linha2, text="", foreground="#1f5fbf")
        self.l_espera.pack(side="right")
        self.l_detalhe = ttk.Label(fa, text="", foreground="#555")
        self.l_detalhe.pack(anchor="w", pady=(2, 0))
        placar = ttk.Frame(fa)
        placar.pack(fill="x", pady=(6, 0))
        self.n_ok, self.n_pend, self.n_erro = (tk.StringVar(value="–") for _ in range(3))
        for rot, var, cor in (("Concluídas", self.n_ok, "#1b7a3a"), ("Em processamento", self.n_pend, "#8a6d00"),
                              ("Com erro", self.n_erro, "#b3261e")):
            q = ttk.Frame(placar, padding=(0, 0, 28, 0))
            q.pack(side="left")
            ttk.Label(q, textvariable=var, style="Num.TLabel", foreground=cor).pack(anchor="w")
            ttk.Label(q, text=rot, foreground="#555").pack(anchor="w")
        fr = ttk.Frame(placar)
        fr.pack(side="right")
        ttk.Button(fr, text="Ver figura",
                   command=lambda: abrir(nucleo.SAIDA / "figura_divergencia_por_duracao.png")).pack(side="right")
        ttk.Button(fr, text="Abrir resultados", command=lambda: abrir(nucleo.SAIDA)).pack(side="right", padx=6)

        # --- registro
        fl = ttk.LabelFrame(raiz, text="Registro", padding=4)
        fl.pack(fill="both", expand=True, pady=(8, 0))
        self.txt = ScrolledText(fl, height=12, font=("Consolas", 9), wrap="none", state="disabled")
        rolagem = ttk.Scrollbar(fl, orient="horizontal", command=self.txt.xview)
        self.txt.configure(xscrollcommand=rolagem.set)
        rolagem.pack(side="bottom", fill="x")
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_configure("ok", foreground="#1b7a3a")
        self.txt.tag_configure("erro", foreground="#b3261e")
        self.txt.tag_configure("aviso", foreground="#8a6d00")

    # ------------------------------------------------------------ registro e relógio
    def _log(self, msg):
        tag = None
        m = str(msg)
        if "ERRO" in m or "FALHOU" in m or "-> erro" in m or "barrada" in m:
            tag = "erro"
        elif "TESTE OK" in m or "-> ok" in m or m.strip().startswith("ok:"):
            tag = "ok"
        elif "AVISO" in m or "pendente" in m or "não reconhecida" in m:
            tag = "aviso"
        self.txt.configure(state="normal")
        self.txt.insert("end", m + "\n", tag)
        linhas = int(self.txt.index("end-1c").split(".")[0])
        if linhas > 3000:
            self.txt.delete("1.0", f"{linhas - 3000}.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _processar_fila(self):
        try:
            while True:
                tipo, dados = self.fila.get_nowait()
                if tipo == "log":
                    self._log(dados)
                elif tipo == "progresso":
                    self._progresso(dados)
                elif tipo == "espera":
                    self.espera = dados if dados["restante"] else None
                elif tipo == "contagem":
                    self.n_ok.set(dados.get("ok", 0))
                    self.n_pend.set(dados.get("pendente", 0))
                    self.n_erro.set(dados.get("erro", 0))
                elif tipo == "fim":
                    self._fim(*dados)
        except queue.Empty:
            pass
        self.after(100, self._processar_fila)

    def _progresso(self, p):
        if self.ultimo_prog is None or p["etapa"] != self.ultimo_prog["etapa"]:
            self.inicio_etapa = (time.time(), p["feito"])
        self.ultimo_prog = p
        self.l_etapa.configure(text=p["etapa"])
        total = max(p["total"], 1)
        self.barra.configure(maximum=total, value=p["feito"])
        self.l_cont.configure(text=f"{p['feito']} de {p['total']}  ({100 * p['feito'] / total:.0f}%)")
        self.l_detalhe.configure(text=p["detalhe"])

    def _relogio(self):
        if self.thread and self.thread.is_alive():
            self.passo_giro = (self.passo_giro + 1) % len(GIRA)
            self.l_giro.configure(text=GIRA[self.passo_giro])
            agora = time.time()
            txt = f"Decorrido {hms(agora - self.inicio_tarefa)}"
            p = self.ultimo_prog
            if p and self.inicio_etapa and p["feito"] > self.inicio_etapa[1] and p["total"] > p["feito"]:
                t0, f0 = self.inicio_etapa
                taxa = (agora - t0) / (p["feito"] - f0)
                txt += f"   ·   Restante ~{hms(taxa * (p['total'] - p['feito']))}"
            self.l_tempo.configure(text=txt)
            e = self.espera
            self.l_espera.configure(text=f"{e['motivo']}: {e['restante']} s" if e else "")
        else:
            self.l_giro.configure(text=" ")
            self.l_espera.configure(text="")
        self.after(250, self._relogio)

    # ------------------------------------------------------------ execução
    def _args(self):
        email = self.v_email.get().strip()
        dias = self.v_dias.get().split()
        est = [x.upper() for x in self.v_est.get().split()]
        try:
            dur = [int(x) for x in self.v_dur.get().split()]
            for d in dias:
                nucleo.date.fromisoformat(d)
        except ValueError:
            messagebox.showerror(TITULO, "Confira os dias (AAAA-MM-DD) e as durações (números inteiros).")
            return None
        nucleo.salvar_config({"email": email, "dias": dias, "estacoes": est, "duracoes": dur})
        return argparse.Namespace(email=email, dias=dias, estacoes=est, duracoes=dur, etapa="gui")

    def _rodar(self, nome, funcoes, depois=None):
        if self.thread and self.thread.is_alive():
            return
        args = self._args()
        if args is None:
            return
        if any(f in (nucleo.etapa_testar_api, nucleo.etapa_enviar) for f in funcoes) and "@" not in args.email:
            messagebox.showwarning(TITULO, "Informe o e-mail que o IBGE-PPP exige.")
            return
        nucleo.PARAR.clear()
        self.depois = depois
        self.inicio_tarefa = time.time()
        self.ultimo_prog = None
        self.espera = None
        self.l_etapa.configure(text=nome)
        self.l_cont.configure(text="")
        self.l_detalhe.configure(text="")
        self.barra.configure(value=0)
        for b in self.botoes:
            b.configure(state="disabled")
        self.b_parar.configure(state="normal")
        self._log(f"\n===== {nome} ({time.strftime('%H:%M:%S')}) =====")

        def trabalho():
            resultado, erro = None, None
            try:
                for f in funcoes:
                    resultado = f(args)
            except nucleo.Interrompido:
                erro = "interrompido"
            except SystemExit as e:
                erro = str(e.code) if e.code not in (None, 0) else None
            except Exception as e:  # noqa
                import traceback
                nucleo.log(traceback.format_exc())
                erro = f"{type(e).__name__}: {e}"
            self.fila.put(("fim", (nome, resultado, erro)))

        self.thread = threading.Thread(target=trabalho, daemon=True)
        self.thread.start()

    def _fim(self, nome, resultado, erro):
        for b in self.botoes:
            b.configure(state="normal")
        self.b_parar.configure(state="disabled")
        decorrido = hms(time.time() - self.inicio_tarefa)
        self.l_tempo.configure(text=f"Terminou em {decorrido}")
        depois, self.depois = self.depois, None
        if erro == "interrompido":
            self.l_etapa.configure(text=f"{nome}: interrompido")
            self._log("Interrompido. Clique na etapa de novo para continuar de onde parou.")
            return
        if erro:
            codigo = erro
            self.l_etapa.configure(text=f"{nome}: parou")
            if codigo == "2":
                messagebox.showinfo(TITULO, "O IBGE ainda está processando o teste. Tente de novo em alguns minutos.")
            elif codigo != "1":
                self._log(f"ERRO: {codigo}")
                messagebox.showerror(TITULO, f"{nome} parou:\n\n{codigo}")
            else:
                messagebox.showerror(TITULO, "O teste falhou. Veja o registro.")
            return
        self.l_etapa.configure(text=f"{nome}: concluído ✓")
        if depois:
            depois(resultado)

    def parar(self):
        if self.thread and self.thread.is_alive():
            nucleo.PARAR.set()
            self.l_etapa.configure(text="Parando no próximo passo seguro…")

    def _fechar(self):
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno(TITULO, "Há uma etapa rodando. Parar e fechar?\n"
                                               "Na próxima vez ele continua de onde parou."):
                return
            nucleo.PARAR.set()
        self.destroy()

    # ------------------------------------------------------------ etapas
    def baixar(self):
        self._rodar("Baixar RINEX", [nucleo.etapa_baixar])

    def cortar(self):
        self._rodar("Cortar sessões", [nucleo.etapa_cortar])

    def testar(self):
        self._rodar("Testar API", [nucleo.etapa_testar_api], depois=self._pos_teste)

    def enviar(self):
        n = self._n_sessoes()
        if n and not messagebox.askyesno(TITULO, f"Enviar as sessões que faltam ao IBGE-PPP?\n\n"
                                                 f"{n} sessões no total; as já processadas são puladas. "
                                                 f"O IBGE-PPP manda um e-mail por processamento."):
            return
        self._rodar("Enviar ao IBGE-PPP", [nucleo.etapa_enviar])

    def analisar(self):
        self._rodar("Analisar", [nucleo.etapa_analisar], depois=lambda _r: self._pos_analise())

    def tudo(self):
        self._rodar("Baixar, cortar e testar", [nucleo.etapa_baixar, nucleo.etapa_cortar, nucleo.etapa_testar_api],
                    depois=self._pos_teste_tudo)

    def _n_sessoes(self):
        return sum(1 for s in nucleo.ler_csv(nucleo.PASTA / "sessoes.csv") if s.get("valida") == "sim")

    def _texto_teste(self, res):
        if not res:
            return "Teste concluído."
        resumo_arq = [p for p in (nucleo.PASTA / "ppp").glob("*/resumo.json")]
        antena = ""
        if resumo_arq:
            import json
            ultimo = max(resumo_arq, key=lambda p: p.stat().st_mtime)
            antena = json.loads(ultimo.read_text(encoding="utf-8")).get("ANTENA", "")
        return (f"TESTE OK\n\nLatitude  {res['lat']:.8f}\nLongitude {res['lon']:.8f}\n"
                f"Altitude  {res['h']:.3f} m\nAntena reconhecida: {antena}\nÓrbita: {res.get('orbita', '')}")

    def _pos_teste(self, res):
        messagebox.showinfo(TITULO, self._texto_teste(res))

    def _pos_teste_tudo(self, res):
        n = self._n_sessoes()
        if messagebox.askyesno(TITULO, self._texto_teste(res) + f"\n\nEnviar agora as {n} sessões ao IBGE-PPP?\n"
                               "(uma por vez, com 5 s de intervalo; leva de 1 a 2 horas)"):
            self._rodar("Enviar e analisar", [nucleo.etapa_enviar, nucleo.etapa_analisar],
                        depois=lambda _r: self._pos_analise())

    def _pos_analise(self):
        if messagebox.askyesno(TITULO, "Análise pronta. Abrir a pasta de resultados?"):
            abrir(nucleo.SAIDA)


def principal():
    try:
        App().mainloop()
    except Exception:  # noqa  (com pythonw não há console: registra o erro num arquivo)
        import traceback
        nucleo.PASTA.mkdir(parents=True, exist_ok=True)
        arq = nucleo.PASTA / "erro_janela.txt"
        arq.write_text(traceback.format_exc(), encoding="utf-8")
        try:
            messagebox.showerror(TITULO, f"A janela fechou com um erro. Detalhes em:\n{arq}")
        except Exception:  # noqa
            pass
        raise


if __name__ == "__main__":
    principal()
