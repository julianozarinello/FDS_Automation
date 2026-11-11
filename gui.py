# gui.py — Interface gráfica do FDS Automation (Tkinter)
#
# Janela unica, pensada para quem nao e de TI:
#   - cabecalho de status (pasta, casos, FDS, firewall)
#   - escolher pasta com um clique
#   - botao grande RODAR com acompanhamento ao vivo e tempo restante
#   - cancelamento seguro (mata a arvore FDS/MPI via Job Object)
#
# Arquitetura: a fila roda em um PROCESSO separado (este mesmo programa,
# chamado com --rodar). Isso mantem a janela sempre responsiva e permite
# cancelar com seguranca. O acompanhamento e feito lendo o fds_run.log
# que o main.py ja escreve (o arquivo e atualizado a cada linha).
#
# O menu de terminal (menu.py) continua existindo para uso avancado.
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

BASE_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")


# ---------------------------------------------------------------------------
# Helpers de configuracao (sem importar menu.py, que mexe no console)
# ---------------------------------------------------------------------------
def _carregar_cfg() -> dict:
    try:
        import yaml
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _texto_yaml(valor) -> str:
    """Serializa texto para YAML sem quebrar caminhos Windows ou aspas."""
    return json.dumps(str(valor or "").strip(), ensure_ascii=False)


def _salvar_config_atomico(texto: str) -> None:
    """Troca o config completo somente depois de a gravacao terminar."""
    pasta = os.path.dirname(os.path.abspath(CONFIG_PATH))
    os.makedirs(pasta, exist_ok=True)
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=pasta, prefix=os.path.basename(CONFIG_PATH) + ".", suffix=".tmp",
        ) as f:
            temporario = f.name
            f.write(texto)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporario, CONFIG_PATH)
        temporario = None
    finally:
        if temporario:
            try:
                os.remove(temporario)
            except OSError:
                pass


def _validar_config_existente(texto: str) -> None:
    if not texto.strip():
        return
    try:
        import yaml
        dados = yaml.safe_load(texto)
    except Exception as exc:
        raise ValueError(
            "O config.yaml atual está inválido. Corrija-o ou restaure uma "
            "cópia válida antes de salvar pela interface."
        ) from exc
    if not isinstance(dados, dict):
        raise ValueError("O config.yaml deve conter um mapa de opções.")


def _salvar_pasta_simulacoes(nova_pasta: str) -> None:
    """Reescreve so a linha 'pasta_simulacoes:' preservando o resto."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            texto = f.read()
    except FileNotFoundError:
        texto = ""

    _validar_config_existente(texto)

    linha_nova = f"pasta_simulacoes: {_texto_yaml(nova_pasta)}"
    novo_texto, n = re.subn(
        r"^pasta_simulacoes:.*$", lambda _match: linha_nova,
        texto, count=1, flags=re.MULTILINE,
    )
    if n == 0:
        novo_texto = texto.rstrip() + f"\n\n{linha_nova}\n"

    _salvar_config_atomico(novo_texto)


def _salvar_monitoramento_cfg(valores: dict) -> None:
    """
    Reescreve apenas o bloco 'monitoramento:' do config.yaml, preservando
    todo o resto do arquivo (comentarios ficam acima do bloco e sobrevivem).
    """
    bloco = (
        "monitoramento:\n"
        f"  ativo: {'true' if valores.get('ativo') else 'false'}\n"
        f"  arquivo_xlsx: {_texto_yaml(valores.get('arquivo_xlsx', ''))}\n"
        f"  usuario: {_texto_yaml(valores.get('usuario', ''))}\n"
        f"  empresa: {_texto_yaml(valores.get('empresa', ''))}\n"
        f"  pc: {_texto_yaml(valores.get('pc', ''))}\n"
        f"  intervalo_seg: {int(valores.get('intervalo_seg', 60) or 60)}\n"
    )

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            texto = f.read()
    except FileNotFoundError:
        texto = ""

    _validar_config_existente(texto)

    novo_texto, n = re.subn(
        r"^monitoramento:\n(?:[ \t]+.*\n?)*", lambda _match: bloco, texto,
        count=1, flags=re.MULTILINE,
    )
    if n == 0:
        novo_texto = texto.rstrip() + "\n\n" + bloco

    _salvar_config_atomico(novo_texto)


def _listar_casos(pasta: str) -> list[str]:
    casos = []
    try:
        for raiz, dirs, nomes in os.walk(pasta):
            dirs.sort()
            for nome in sorted(nomes):
                if nome.lower().endswith(".fds"):
                    casos.append(os.path.relpath(os.path.join(raiz, nome), pasta))
    except Exception:
        pass
    return casos


def _diretorio_fds_atual(cfg: dict) -> str:
    diretorio = str(cfg.get("diretorio_fds", "") or "")
    if os.path.isdir(diretorio):
        return diretorio
    try:
        import path_detector
        for pasta in path_detector.detect_fds_paths() + path_detector.detect_pyrosim_paths():
            if os.path.exists(os.path.join(pasta, "fds.exe")):
                return pasta
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Modo worker: --rodar / --testar (roda a fila neste processo, sem GUI)
# ---------------------------------------------------------------------------
def executar_modo_worker(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--rodar", action="store_true")
    parser.add_argument("--testar", action="store_true")
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--otimizador", choices=["0", "1"], default=None)
    parser.add_argument("--controle-caso", default="")
    args = parser.parse_args(argv)

    try:
        import main as fds_main
        cfg = fds_main.carregar_config(args.config)
        if args.controle_caso:
            from case_control import CHAVE_CAMINHO, limpar_solicitacao
            cfg[CHAVE_CAMINHO] = os.path.abspath(args.controle_caso)
            limpar_solicitacao(args.controle_caso)
        if args.otimizador is not None:
            cfg.setdefault("otimizador", {})["ativo"] = args.otimizador == "1"
        if args.testar:
            fds_main.testar_melhor_configuracao(cfg)
            return 0
        sucesso = fds_main.executar_fila(cfg)
        return 0 if sucesso else 2
    except Exception:
        # Em exe sem console nao ha stderr util: registra o erro em arquivo
        import traceback
        try:
            with open(os.path.join(BASE_DIR, "fds_gui_erro.log"), "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        return 1
    finally:
        if args.controle_caso:
            try:
                from case_control import limpar_solicitacao
                limpar_solicitacao(args.controle_caso)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Interface grafica
# ---------------------------------------------------------------------------
# Remove o prefixo "2026-07-13 17:30:01 [INFO] " das linhas do log
_RE_PREFIXO_LOG = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[\w+\] ")
_RE_PROGRESSO_FILA = re.compile(r"Progresso\s*:\s*(\d+)/(\d+)")
_RE_RODANDO = re.compile(
    r"Rodando: (.+?) \| passo (\d+) \| tempo simulado ([\d.eE+\-]+)s"
    r" \| decorrido (.+?)(?: \| restante ~(.+))?$"
)
_RE_INICIO_CASO = re.compile(r"SIMULAÇÃO\s+(\d+)/(\d+):\s*(.+)$", re.IGNORECASE)
_RE_ERRO_FATAL_FDS = re.compile(r"FDS\s*\|\s*ERROR(?:\(\d+\))?:", re.IGNORECASE)


class AppGUI:
    CORES = {
        "fundo": "#f4f4f6",
        "card": "#ffffff",
        "destaque": "#c62828",   # vermelho fogo
        "ok": "#2e7d32",
        "alerta": "#e65100",
        "texto": "#212121",
        "texto2": "#616161",
    }

    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.worker = None            # subprocess.Popen do modo --rodar/--testar
        self.job = None               # Job Object protegendo o worker
        self.log_handle = None        # arquivo fds_run.log sendo "seguido"
        self.log_path = None
        self.total_casos_fila = 0
        self._cancelando = False
        self._processos_pendentes = []
        self.controle_caso_path = None
        self.caso_atual = None
        self._parada_caso_solicitada = False
        self.diagnosticos_path = None
        self._diagnosticos_assinatura = None
        self._diagnosticos: list[dict] = []
        # Impede que o refresh automatico e o botao "Atualizar agora"
        # leiam simultaneamente o mesmo arquivo compartilhado.
        self._acomp_lock = threading.Lock()
        self._diagnostico_em_andamento = False

        root.title("FDS Automation — Simulações de Incêndio")
        root.geometry("1020x720")
        root.minsize(820, 620)
        root.configure(bg=self.CORES["fundo"])
        root.protocol("WM_DELETE_WINDOW", self.ao_fechar)

        self._montar_layout()
        self.atualizar_status()
        self._agendar_tick()

    # -- construcao da tela -------------------------------------------------
    def _montar_layout(self):
        tk, ttk, C = self.tk, self.ttk, self.CORES

        estilo = ttk.Style()
        try:
            estilo.theme_use("vista")
        except Exception:
            pass
        estilo.configure("Card.TFrame", background=C["card"])
        estilo.configure("Fundo.TFrame", background=C["fundo"])

        # Cabecalho
        topo = tk.Frame(self.root, bg=C["destaque"])
        topo.pack(fill="x")
        tk.Label(
            topo, text="🔥 FDS Automation", font=("Segoe UI", 18, "bold"),
            bg=C["destaque"], fg="white", pady=8, padx=16,
        ).pack(side="left")
        tk.Label(
            topo, text="Suas simulações de incêndio no piloto automático",
            font=("Segoe UI", 10), bg=C["destaque"], fg="#ffd0d0",
        ).pack(side="left", padx=4)
        tk.Label(
            topo,
            text="© JZ Engenharia\nlicenciado à RME Consultoria & Engenharia",
            font=("Segoe UI", 8), bg=C["destaque"], fg="#ffd0d0",
            justify="right",
        ).pack(side="right", padx=12)

        # Abas: execucao, diagnosticos amigaveis e planilha da equipe.
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(8, 0))

        corpo = tk.Frame(self.notebook, bg=C["fundo"], padx=8, pady=8)
        self.notebook.add(corpo, text="  ▶  Rodar  ")

        self.aba_log_erros = tk.Frame(
            self.notebook, bg=C["fundo"], padx=8, pady=8
        )
        self.notebook.add(self.aba_log_erros, text="  🧾  Log e erros  ")
        self._montar_aba_log_erros(self.aba_log_erros)

        aba_acomp = tk.Frame(self.notebook, bg=C["fundo"], padx=8, pady=8)
        self.notebook.add(aba_acomp, text="  📊  Acompanhamento  ")
        self._montar_aba_acompanhamento(aba_acomp)

        # Linha 1: pasta
        linha_pasta = tk.Frame(corpo, bg=C["fundo"])
        linha_pasta.pack(fill="x", pady=(0, 8))
        tk.Label(
            linha_pasta, text="📁 Pasta das simulações:",
            font=("Segoe UI", 10, "bold"), bg=C["fundo"], fg=C["texto"],
        ).pack(side="left")
        self.var_pasta = tk.StringVar()
        tk.Entry(
            linha_pasta, textvariable=self.var_pasta, state="readonly",
            font=("Segoe UI", 10), readonlybackground="white",
        ).pack(side="left", fill="x", expand=True, padx=8)
        self.btn_pasta = tk.Button(
            linha_pasta, text="Escolher…", command=self.escolher_pasta,
            font=("Segoe UI", 10),
        )
        self.btn_pasta.pack(side="left")

        # Linha 2: status
        self.lbl_status_fds = self._label_status(corpo)
        self.lbl_status_fw = self._label_status(corpo)

        # Linha 3: casos + log lado a lado
        meio = tk.Frame(corpo, bg=C["fundo"])
        meio.pack(fill="both", expand=True, pady=(6, 8))

        painel_casos = tk.LabelFrame(
            meio, text=" Casos encontrados ", font=("Segoe UI", 9, "bold"),
            bg=C["card"], fg=C["texto2"],
        )
        painel_casos.pack(side="left", fill="both", padx=(0, 8))
        self.lista_casos = tk.Listbox(
            painel_casos, width=38, font=("Segoe UI", 9), borderwidth=0,
            highlightthickness=0, activestyle="none",
        )
        self.lista_casos.pack(fill="both", expand=True, padx=6, pady=6)

        painel_log = tk.LabelFrame(
            meio, text=" Resumo da execução ", font=("Segoe UI", 9, "bold"),
            bg=C["card"], fg=C["texto2"],
        )
        painel_log.pack(side="left", fill="both", expand=True)
        self.txt_log = tk.Text(
            painel_log, font=("Consolas", 9), state="disabled", wrap="none",
            bg="#1e1e1e", fg="#dcdcdc", borderwidth=0,
        )
        rolagem = tk.Scrollbar(painel_log, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=rolagem.set)
        rolagem.pack(side="right", fill="y")
        self.txt_log.pack(fill="both", expand=True, padx=(6, 0), pady=6)

        self.var_detalhes = tk.BooleanVar(value=False)
        tk.Checkbutton(
            corpo, text="Mostrar detalhes técnicos do FDS",
            variable=self.var_detalhes, bg=C["fundo"], fg=C["texto2"],
            font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x")

        # Linha 4: opcao pre-execucao
        self.var_otimizar = tk.BooleanVar(value=True)
        tk.Checkbutton(
            corpo,
            text="Antes de rodar, descobrir a configuração mais rápida "
                 "(testes curtos; recomendado — fica salvo para as próximas vezes)",
            variable=self.var_otimizar, bg=C["fundo"], fg=C["texto"],
            font=("Segoe UI", 9), anchor="w",
        ).pack(fill="x", pady=(2, 6))

        # Linha 5: botoes
        botoes = tk.Frame(corpo, bg=C["fundo"])
        botoes.pack(fill="x", pady=(0, 3))
        self.btn_rodar = tk.Button(
            botoes, text="▶  RODAR SIMULAÇÕES", command=self.rodar,
            font=("Segoe UI", 12, "bold"), bg=C["ok"], fg="white",
            activebackground="#1b5e20", activeforeground="white",
            padx=18, pady=6, cursor="hand2",
        )
        self.btn_rodar.pack(side="left")
        self.btn_parar_caso = tk.Button(
            botoes, text="⏭  Parar caso atual", command=self.parar_caso_atual,
            font=("Segoe UI", 10, "bold"), bg="#ef6c00", fg="white",
            activebackground="#c43e00", activeforeground="white",
            padx=10, pady=6, state="disabled",
        )
        self.btn_parar_caso.pack(side="left", padx=(8, 0))
        self.btn_cancelar = tk.Button(
            botoes, text="■  Cancelar fila", command=self.cancelar,
            font=("Segoe UI", 10), bg="#b71c1c", fg="white",
            activebackground="#7f0000", activeforeground="white",
            padx=12, pady=6, state="disabled",
        )
        self.btn_cancelar.pack(side="left", padx=8)

        botoes_aux = tk.Frame(corpo, bg=C["fundo"])
        botoes_aux.pack(fill="x", pady=(0, 6))
        self.btn_fw = tk.Button(
            botoes_aux, text="🛡️ Parar perguntas do Windows",
            command=self.liberar_firewall, font=("Segoe UI", 10), padx=8,
        )
        self.btn_fw.pack(side="right")
        self.btn_diagnostico = tk.Button(
            botoes_aux, text="🖥️ Diagnóstico do computador",
            command=self.mostrar_diagnostico, font=("Segoe UI", 10), padx=8,
        )
        self.btn_diagnostico.pack(side="right", padx=(0, 8))

        # Barra de status inferior
        self.var_barra = tk.StringVar(value="Pronto.")
        tk.Label(
            self.root, textvariable=self.var_barra, anchor="w",
            font=("Segoe UI", 9), bg="#e0e0e0", fg=C["texto"], padx=10, pady=4,
        ).pack(fill="x", side="bottom")

    def _label_status(self, pai):
        lbl = self.tk.Label(
            pai, font=("Segoe UI", 9), bg=self.CORES["fundo"],
            fg=self.CORES["texto2"], anchor="w",
        )
        lbl.pack(fill="x")
        return lbl

    # -- aba de log e erros ---------------------------------------------------
    def _montar_aba_log_erros(self, aba):
        tk, ttk, C = self.tk, self.ttk, self.CORES

        self.notebook_log = ttk.Notebook(aba)
        self.notebook_log.pack(fill="both", expand=True)

        explicados = tk.Frame(self.notebook_log, bg=C["fundo"], padx=8, pady=8)
        tecnico = tk.Frame(self.notebook_log, bg=C["fundo"], padx=6, pady=6)
        self.notebook_log.add(explicados, text="  Erros explicados  ")
        self.notebook_log.add(tecnico, text="  Log técnico completo  ")

        self.var_resumo_erros = tk.StringVar(
            value="Nenhum erro identificado. Os diagnósticos da próxima execução aparecerão aqui."
        )
        self.lbl_resumo_erros = tk.Label(
            explicados, textvariable=self.var_resumo_erros,
            font=("Segoe UI", 10, "bold"), anchor="w", justify="left",
            bg="#e8f5e9", fg=C["ok"], padx=10, pady=8,
        )
        self.lbl_resumo_erros.pack(fill="x", pady=(0, 8))

        painel_lista = tk.LabelFrame(
            explicados, text=" Ocorrências desta execução ",
            font=("Segoe UI", 9, "bold"), bg=C["card"], fg=C["texto2"],
        )
        painel_lista.pack(fill="x", pady=(0, 8))

        colunas = ("hora", "status", "caso", "resumo")
        self.tree_erros = ttk.Treeview(
            painel_lista, columns=colunas, show="headings", height=7,
            selectmode="browse",
        )
        self.tree_erros.heading("hora", text="Horário")
        self.tree_erros.heading("status", text="Status")
        self.tree_erros.heading("caso", text="Caso")
        self.tree_erros.heading("resumo", text="Resumo")
        self.tree_erros.column("hora", width=75, stretch=False, anchor="center")
        self.tree_erros.column("status", width=105, stretch=False, anchor="center")
        self.tree_erros.column("caso", width=250, minwidth=140)
        self.tree_erros.column("resumo", width=430, minwidth=240)
        self.tree_erros.tag_configure("erro", foreground="#b71c1c")
        self.tree_erros.tag_configure("aviso", foreground="#e65100")
        scroll_lista = ttk.Scrollbar(
            painel_lista, orient="vertical", command=self.tree_erros.yview
        )
        self.tree_erros.configure(yscrollcommand=scroll_lista.set)
        scroll_lista.pack(side="right", fill="y")
        self.tree_erros.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        self.tree_erros.bind("<<TreeviewSelect>>", self._ao_selecionar_diagnostico)

        painel_detalhe = tk.LabelFrame(
            explicados, text=" O que aconteceu e como verificar ",
            font=("Segoe UI", 9, "bold"), bg=C["card"], fg=C["texto2"],
        )
        painel_detalhe.pack(fill="both", expand=True)
        self.txt_diagnostico = tk.Text(
            painel_detalhe, state="disabled", wrap="word",
            font=("Segoe UI", 10), bg="white", fg=C["texto"],
            borderwidth=0, padx=10, pady=8,
        )
        self.txt_diagnostico.tag_configure("titulo", font=("Segoe UI", 12, "bold"), foreground="#b71c1c")
        self.txt_diagnostico.tag_configure("secao", font=("Segoe UI", 10, "bold"), foreground="#37474f", spacing1=8)
        self.txt_diagnostico.tag_configure("original", font=("Consolas", 9), background="#f5f5f5", lmargin1=8, lmargin2=8)
        scroll_detalhe = ttk.Scrollbar(
            painel_detalhe, orient="vertical", command=self.txt_diagnostico.yview
        )
        self.txt_diagnostico.configure(yscrollcommand=scroll_detalhe.set)
        scroll_detalhe.pack(side="right", fill="y")
        self.txt_diagnostico.pack(side="left", fill="both", expand=True)
        self._mostrar_texto_diagnostico(
            "Selecione uma ocorrência na lista para ver a mensagem original "
            "do FDS, a possível causa e os passos de verificação."
        )

        acoes = tk.Frame(explicados, bg=C["fundo"])
        # Reserva a barra antes de o painel de detalhes consumir o espaco
        # expansivel; em telas menores os botoes continuam sempre visiveis.
        acoes.pack(fill="x", pady=(8, 0), before=painel_detalhe)
        self.btn_abrir_fds_erro = tk.Button(
            acoes, text="Abrir arquivo .fds", command=self._abrir_fds_diagnostico,
            font=("Segoe UI", 9), state="disabled",
        )
        self.btn_abrir_fds_erro.pack(side="left")
        self.btn_abrir_out_erro = tk.Button(
            acoes, text="Abrir saída .out", command=self._abrir_out_diagnostico,
            font=("Segoe UI", 9), state="disabled",
        )
        self.btn_abrir_out_erro.pack(side="left", padx=6)
        self.btn_abrir_pasta_erro = tk.Button(
            acoes, text="Abrir pasta do caso", command=self._abrir_pasta_diagnostico,
            font=("Segoe UI", 9), state="disabled",
        )
        self.btn_abrir_pasta_erro.pack(side="left")
        self.btn_copiar_erro = tk.Button(
            acoes, text="Copiar diagnóstico", command=self._copiar_diagnostico,
            font=("Segoe UI", 9), state="disabled",
        )
        self.btn_copiar_erro.pack(side="left", padx=6)
        tk.Button(
            acoes, text="Abrir fds_run.log", command=self._abrir_log_completo,
            font=("Segoe UI", 9),
        ).pack(side="right")

        self.txt_log_tecnico = tk.Text(
            tecnico, font=("Consolas", 9), state="disabled", wrap="none",
            bg="#1e1e1e", fg="#dcdcdc", borderwidth=0,
        )
        scroll_tecnico_y = tk.Scrollbar(tecnico, command=self.txt_log_tecnico.yview)
        scroll_tecnico_x = tk.Scrollbar(
            tecnico, command=self.txt_log_tecnico.xview, orient="horizontal"
        )
        self.txt_log_tecnico.configure(
            yscrollcommand=scroll_tecnico_y.set,
            xscrollcommand=scroll_tecnico_x.set,
        )
        scroll_tecnico_y.pack(side="right", fill="y")
        scroll_tecnico_x.pack(side="bottom", fill="x")
        self.txt_log_tecnico.pack(fill="both", expand=True)

    def _mostrar_texto_diagnostico(self, texto: str) -> None:
        self.txt_diagnostico.configure(state="normal")
        self.txt_diagnostico.delete("1.0", "end")
        self.txt_diagnostico.insert("end", texto)
        self.txt_diagnostico.configure(state="disabled")

    def _limpar_aba_diagnosticos(self) -> None:
        for item in self.tree_erros.get_children():
            self.tree_erros.delete(item)
        self._diagnosticos = []
        self._diagnosticos_assinatura = None
        self.var_resumo_erros.set(
            "Execução iniciada. Se algum caso falhar, a mensagem exata do FDS aparecerá aqui."
        )
        self.lbl_resumo_erros.config(bg="#e3f2fd", fg="#1565c0")
        self.notebook.tab(self.aba_log_erros, text="  🧾  Log e erros  ")
        self._mostrar_texto_diagnostico(
            "Nenhum erro identificado até o momento. A fila está sendo acompanhada."
        )
        self._habilitar_acoes_diagnostico(False)

        self.txt_log_tecnico.configure(state="normal")
        self.txt_log_tecnico.delete("1.0", "end")
        self.txt_log_tecnico.configure(state="disabled")

    def _habilitar_acoes_diagnostico(self, habilitar: bool) -> None:
        estado = "normal" if habilitar else "disabled"
        for botao in (
            self.btn_abrir_fds_erro,
            self.btn_abrir_out_erro,
            self.btn_abrir_pasta_erro,
            self.btn_copiar_erro,
        ):
            botao.config(state=estado)

    @staticmethod
    def _hora_evento(evento: dict) -> str:
        data = str(evento.get("data", "") or "")
        match = re.search(r"T(\d{2}:\d{2}:\d{2})", data)
        return match.group(1) if match else "--:--:--"

    def _atualizar_aba_diagnosticos(self, forcar: bool = False) -> None:
        caminho = self.diagnosticos_path
        if not caminho:
            return
        try:
            stat = os.stat(caminho)
            assinatura = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return
        if not forcar and assinatura == self._diagnosticos_assinatura:
            return

        try:
            from fds_diagnostics import ler_eventos
            todos = ler_eventos(caminho)
        except Exception:
            return

        eventos = [
            evento for evento in todos
            if evento.get("tipo") in {"erro_fds", "erro_automacao", "interrompida"}
        ]
        resumos = [evento for evento in todos if evento.get("tipo") == "resumo"]
        selecao_anterior = self.tree_erros.selection()
        selecionado = selecao_anterior[0] if selecao_anterior else None

        for item in self.tree_erros.get_children():
            self.tree_erros.delete(item)
        self._diagnosticos = eventos
        for evento in eventos:
            iid = str(evento.get("id") or len(self.tree_erros.get_children()))
            interrompida = evento.get("tipo") == "interrompida"
            status = "INTERROMPIDA" if interrompida else "ERRO"
            tag = "aviso" if interrompida else "erro"
            self.tree_erros.insert(
                "", "end", iid=iid,
                values=(
                    self._hora_evento(evento),
                    status,
                    evento.get("caso", ""),
                    evento.get("titulo", ""),
                ),
                tags=(tag,),
            )

        erros = sum(evento.get("tipo") != "interrompida" for evento in eventos)
        interrompidas = sum(evento.get("tipo") == "interrompida" for evento in eventos)
        concluidas = None
        if resumos:
            resumo = resumos[-1]
            concluidas = int(resumo.get("concluidas", 0) or 0)
            erros = int(resumo.get("falhas", erros) or 0)
            interrompidas = int(resumo.get("interrompidas", interrompidas) or 0)

        partes = []
        if concluidas is not None:
            partes.append(f"{concluidas} concluída(s)")
        partes.extend((f"{erros} erro(s)", f"{interrompidas} interrompida(s)"))
        self.var_resumo_erros.set("Resumo da fila: " + "  •  ".join(partes))

        if erros:
            self.lbl_resumo_erros.config(bg="#ffebee", fg="#b71c1c")
        elif interrompidas:
            self.lbl_resumo_erros.config(bg="#fff3e0", fg="#e65100")
        else:
            self.lbl_resumo_erros.config(bg="#e8f5e9", fg=self.CORES["ok"])

        total_ocorrencias = erros + interrompidas
        texto_aba = "  🧾  Log e erros  "
        if total_ocorrencias:
            texto_aba = f"  ⚠  Log e erros ({total_ocorrencias})  "
        self.notebook.tab(self.aba_log_erros, text=texto_aba)

        filhos = self.tree_erros.get_children()
        if selecionado in filhos:
            self.tree_erros.selection_set(selecionado)
        elif filhos:
            ultimo = filhos[-1]
            self.tree_erros.selection_set(ultimo)
            self.tree_erros.focus(ultimo)
            self.tree_erros.see(ultimo)
            self._exibir_diagnostico(self._diagnostico_por_id(ultimo))
        else:
            self._mostrar_texto_diagnostico(
                "Nenhuma falha ou interrupção foi registrada nesta execução."
            )
            self._habilitar_acoes_diagnostico(False)
        self._diagnosticos_assinatura = assinatura

    def _diagnostico_por_id(self, identificador: str | None) -> dict | None:
        for evento in self._diagnosticos:
            if str(evento.get("id")) == str(identificador):
                return evento
        return None

    def _diagnostico_selecionado(self) -> dict | None:
        selecao = self.tree_erros.selection()
        return self._diagnostico_por_id(selecao[0]) if selecao else None

    def _ao_selecionar_diagnostico(self, _evento=None) -> None:
        self._exibir_diagnostico(self._diagnostico_selecionado())

    def _exibir_diagnostico(self, evento: dict | None) -> None:
        if not evento:
            self._habilitar_acoes_diagnostico(False)
            return
        t = self.txt_diagnostico
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", str(evento.get("titulo", "Ocorrência")) + "\n", "titulo")
        t.insert("end", f"Caso: {evento.get('caso', '—')}\n")
        t.insert("end", f"Fonte: {evento.get('fonte', '—')}")
        if evento.get("codigo_fds"):
            t.insert("end", f"  |  Código do FDS: {evento['codigo_fds']}")
        if evento.get("codigo_retorno") is not None:
            t.insert("end", f"  |  Retorno do processo: {evento['codigo_retorno']}")
        t.insert("end", "\n")
        t.insert("end", f"Arquivo: {evento.get('arquivo_fds', '—')}\n")

        t.insert("end", "\nMensagem original\n", "secao")
        t.insert("end", str(evento.get("mensagem_original", "—")) + "\n", "original")
        t.insert("end", "\nO que isso provavelmente significa\n", "secao")
        t.insert("end", str(evento.get("explicacao", "—")) + "\n")
        t.insert("end", "\nComo verificar ou corrigir\n", "secao")
        for indice, acao in enumerate(evento.get("acoes") or [], start=1):
            t.insert("end", f"{indice}. {acao}\n")

        contexto = evento.get("contexto") or []
        if contexto:
            t.insert("end", "\nTrecho técnico próximo ao erro\n", "secao")
            t.insert("end", "\n".join(str(linha) for linha in contexto) + "\n", "original")
        t.configure(state="disabled")
        t.see("1.0")
        self._habilitar_acoes_diagnostico(True)

    def _abrir_caminho_diagnostico(self, caminho: str, pasta: bool = False) -> None:
        from tkinter import messagebox
        alvo = os.path.dirname(caminho) if pasta else caminho
        if not alvo or not os.path.exists(alvo):
            messagebox.showwarning(
                "Arquivo não encontrado",
                f"Não encontrei este caminho:\n{alvo or caminho}\n\n"
                "Ele pode não ter sido criado porque o FDS encerrou muito cedo.",
            )
            return
        try:
            if os.name == "nt":
                os.startfile(alvo)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", alvo])
        except Exception as exc:
            messagebox.showerror("Não foi possível abrir", str(exc))

    def _abrir_fds_diagnostico(self) -> None:
        evento = self._diagnostico_selecionado()
        if evento:
            self._abrir_caminho_diagnostico(str(evento.get("arquivo_fds", "")))

    def _abrir_out_diagnostico(self) -> None:
        evento = self._diagnostico_selecionado()
        if evento:
            self._abrir_caminho_diagnostico(str(evento.get("arquivo_out", "")))

    def _abrir_pasta_diagnostico(self) -> None:
        evento = self._diagnostico_selecionado()
        if evento:
            self._abrir_caminho_diagnostico(
                str(evento.get("arquivo_fds", "")), pasta=True
            )

    def _copiar_diagnostico(self) -> None:
        evento = self._diagnostico_selecionado()
        if not evento:
            return
        linhas = [
            str(evento.get("titulo", "Ocorrência")),
            f"Caso: {evento.get('caso', '—')}",
            f"Arquivo: {evento.get('arquivo_fds', '—')}",
            "",
            "Mensagem original:",
            str(evento.get("mensagem_original", "—")),
            "",
            "Explicação:",
            str(evento.get("explicacao", "—")),
            "",
            "Como verificar/corrigir:",
        ]
        linhas.extend(
            f"{i}. {acao}" for i, acao in enumerate(evento.get("acoes") or [], 1)
        )
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(linhas))
        self.var_barra.set("Diagnóstico copiado para a área de transferência.")

    def _abrir_log_completo(self) -> None:
        caminho = self.log_path
        if not caminho:
            cfg = _carregar_cfg()
            pasta = str(cfg.get("pasta_simulacoes", "") or "")
            caminho = os.path.join(pasta, "fds_run.log") if pasta else ""
        self._abrir_caminho_diagnostico(str(caminho or ""))

    def _log_tecnico_gui(self, linha: str) -> None:
        self.txt_log_tecnico.configure(state="normal")
        self.txt_log_tecnico.insert("end", linha + "\n")
        excesso = int(float(self.txt_log_tecnico.index("end-1c").split(".")[0])) - 8000
        if excesso > 0:
            self.txt_log_tecnico.delete("1.0", f"{excesso + 1}.0")
        self.txt_log_tecnico.see("end")
        self.txt_log_tecnico.configure(state="disabled")

    # -- aba de acompanhamento (planilha compartilhada) ------------------------
    def _montar_aba_acompanhamento(self, aba):
        tk, ttk, C = self.tk, self.ttk, self.CORES
        mon = _carregar_cfg().get("monitoramento") or {}

        # Identificacao do usuario/PC/empresa
        painel_id = tk.LabelFrame(
            aba, text=" Quem está usando este PC ", font=("Segoe UI", 9, "bold"),
            bg=C["fundo"], fg=C["texto2"],
        )
        painel_id.pack(fill="x", pady=(0, 6))

        self.var_usuario = tk.StringVar(value=str(mon.get("usuario", "") or ""))
        self.var_empresa = tk.StringVar(value=str(mon.get("empresa", "") or ""))
        self.var_pc = tk.StringVar(
            value=str(mon.get("pc", "") or "")
            or os.environ.get("COMPUTERNAME", "")
        )

        for rotulo, var in (
            ("Seu nome:", self.var_usuario),
            ("Empresa:", self.var_empresa),
            ("Nome do PC:", self.var_pc),
        ):
            tk.Label(
                painel_id, text=rotulo, font=("Segoe UI", 9),
                bg=C["fundo"], fg=C["texto"],
            ).pack(side="left", padx=(10, 4), pady=6)
            tk.Entry(
                painel_id, textvariable=var, font=("Segoe UI", 9), width=20,
            ).pack(side="left", padx=(0, 8), pady=6)

        # Planilha compartilhada
        painel_arq = tk.LabelFrame(
            aba, text=" Planilha compartilhada (.xlsx) ",
            font=("Segoe UI", 9, "bold"), bg=C["fundo"], fg=C["texto2"],
        )
        painel_arq.pack(fill="x", pady=(0, 6))

        linha_arq = tk.Frame(painel_arq, bg=C["fundo"])
        linha_arq.pack(fill="x", padx=10, pady=(6, 2))
        self.var_xlsx = tk.StringVar(value=str(mon.get("arquivo_xlsx", "") or ""))
        tk.Entry(
            linha_arq, textvariable=self.var_xlsx, state="readonly",
            font=("Segoe UI", 9), readonlybackground="white",
        ).pack(side="left", fill="x", expand=True)
        tk.Button(
            linha_arq, text="Escolher…", command=self.escolher_planilha,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(8, 0))

        tk.Label(
            painel_arq,
            text="Dica: escolha um arquivo dentro da pasta do Google Drive/OneDrive. "
                 "Todos os PCs apontam para o MESMO arquivo e a equipe acompanha tudo junto.",
            font=("Segoe UI", 8), bg=C["fundo"], fg=C["texto2"], anchor="w",
            wraplength=780, justify="left",
        ).pack(fill="x", padx=10, pady=(0, 4))

        linha_acoes = tk.Frame(painel_arq, bg=C["fundo"])
        linha_acoes.pack(fill="x", padx=10, pady=(0, 8))
        self.var_mon_ativo = tk.BooleanVar(value=bool(mon.get("ativo", False)))
        tk.Checkbutton(
            linha_acoes, text="Ativar acompanhamento na planilha",
            variable=self.var_mon_ativo, bg=C["fundo"], fg=C["texto"],
            font=("Segoe UI", 9),
        ).pack(side="left")
        tk.Button(
            linha_acoes, text="💾 Salvar", command=self.salvar_monitoramento,
            font=("Segoe UI", 9, "bold"), bg=C["ok"], fg="white", padx=10,
        ).pack(side="left", padx=10)
        tk.Button(
            linha_acoes, text="📂 Abrir planilha", command=self.abrir_planilha,
            font=("Segoe UI", 9), padx=8,
        ).pack(side="left")
        tk.Button(
            linha_acoes, text="🔄 Atualizar agora",
            command=lambda: self.atualizar_acompanhamento(manual=True),
            font=("Segoe UI", 9), padx=8,
        ).pack(side="left", padx=10)

        # Tabela com todas as simulacoes (de todos os PCs)
        painel_tab = tk.LabelFrame(
            aba, text=" Simulações da equipe (todos os PCs) ",
            font=("Segoe UI", 9, "bold"), bg=C["fundo"], fg=C["texto2"],
        )
        painel_tab.pack(fill="both", expand=True)

        colunas = (
            "Caso", "Status", "Progresso", "Tempo restante", "Usuário",
            "Empresa", "PC", "Início", "Duração", "Erro", "Atualizado em",
        )
        larguras = {
            "Caso": 190, "Status": 80, "Progresso": 130, "Tempo restante": 95,
            "Usuário": 95, "Empresa": 95, "PC": 90, "Início": 110,
            "Duração": 80, "Erro": 220, "Atualizado em": 110,
        }
        moldura = tk.Frame(painel_tab, bg=C["fundo"])
        moldura.pack(fill="both", expand=True, padx=6, pady=6)
        self.tabela = ttk.Treeview(moldura, columns=colunas, show="headings", height=10)
        for col in colunas:
            self.tabela.heading(col, text=col)
            self.tabela.column(col, width=larguras[col], stretch=(col in ("Caso", "Erro")))
        rol_y = ttk.Scrollbar(moldura, orient="vertical", command=self.tabela.yview)
        rol_x = ttk.Scrollbar(moldura, orient="horizontal", command=self.tabela.xview)
        self.tabela.configure(yscrollcommand=rol_y.set, xscrollcommand=rol_x.set)
        rol_y.pack(side="right", fill="y")
        rol_x.pack(side="bottom", fill="x")
        self.tabela.pack(fill="both", expand=True)

        # Cores por status (mesmas da planilha)
        self.tabela.tag_configure("RODANDO", background="#bbdefb")
        self.tabela.tag_configure("CONCLUÍDA", background="#c8e6c9")
        self.tabela.tag_configure("ERRO", background="#ffcdd2")
        self.tabela.tag_configure("NA FILA", background="#f9e6b3")
        self.tabela.tag_configure("CANCELADA", background="#e0e0e0")

        self.lbl_acomp_info = tk.Label(
            painel_tab, text="", font=("Segoe UI", 8), bg=C["fundo"],
            fg=C["texto2"], anchor="w",
        )
        self.lbl_acomp_info.pack(fill="x", padx=8, pady=(0, 4))

        self.atualizar_acompanhamento()
        self._agendar_refresh_acompanhamento()

    def escolher_planilha(self):
        from tkinter import filedialog
        caminho = filedialog.asksaveasfilename(
            title="Escolha (ou crie) a planilha compartilhada",
            defaultextension=".xlsx",
            filetypes=[("Planilha do Excel", "*.xlsx")],
            initialfile="acompanhamento_simulacoes.xlsx",
            confirmoverwrite=False,
        )
        if caminho:
            self.var_xlsx.set(caminho.replace("\\", "/"))
            self.var_mon_ativo.set(True)

    def salvar_monitoramento(self):
        from tkinter import messagebox
        mon_atual = _carregar_cfg().get("monitoramento") or {}
        valores = {
            "ativo": self.var_mon_ativo.get(),
            "arquivo_xlsx": self.var_xlsx.get().strip(),
            "usuario": self.var_usuario.get().strip(),
            "empresa": self.var_empresa.get().strip(),
            "pc": self.var_pc.get().strip(),
            # preserva o intervalo configurado manualmente no config.yaml
            "intervalo_seg": mon_atual.get("intervalo_seg", 60),
        }
        if valores["ativo"] and not valores["arquivo_xlsx"]:
            messagebox.showwarning(
                "Falta a planilha",
                "Escolha o arquivo .xlsx antes de ativar o acompanhamento.",
            )
            return
        try:
            _salvar_monitoramento_cfg(valores)
        except Exception as exc:
            messagebox.showerror("Erro", f"Não foi possível salvar:\n{exc}")
            return
        messagebox.showinfo(
            "Salvo",
            "Configuração de acompanhamento salva!\n"
            "As próximas execuções vão atualizar a planilha automaticamente.",
        )
        self.atualizar_acompanhamento(manual=True)

    def abrir_planilha(self):
        from tkinter import messagebox
        caminho = self.var_xlsx.get().strip()
        if not caminho or not os.path.exists(caminho):
            messagebox.showinfo(
                "Planilha ainda não existe",
                "A planilha é criada automaticamente na primeira execução\n"
                "com o acompanhamento ativado.",
            )
            return
        os.startfile(caminho)

    def _agendar_refresh_acompanhamento(self):
        self.root.after(30000, self._refresh_acompanhamento_auto)

    def _refresh_acompanhamento_auto(self):
        try:
            self.atualizar_acompanhamento()
        finally:
            self._agendar_refresh_acompanhamento()

    def atualizar_acompanhamento(self, manual: bool = False):
        """Recarrega a tabela a partir da planilha (em segundo plano)."""
        caminho = self.var_xlsx.get().strip()
        if not caminho:
            if manual:
                from tkinter import messagebox
                messagebox.showinfo(
                    "Sem planilha", "Escolha o arquivo .xlsx primeiro."
                )
            return

        # O refresh automatico pode coincidir com o clique em "Atualizar
        # agora". Alem do trabalho duplicado, duas leituras simultaneas de
        # um arquivo sincronizado por OneDrive/Google Drive aumentam a chance
        # de erro de compartilhamento. Uma leitura em andamento e suficiente.
        if not self._acomp_lock.acquire(blocking=False):
            if manual:
                self.lbl_acomp_info.config(
                    text="A planilha já está sendo atualizada. Aguarde um instante…"
                )
            return

        def trabalho():
            erro = None
            try:
                from monitor_xlsx import ler_planilha
                linhas = ler_planilha(caminho, propagar_erros=True)
            except Exception as exc:
                linhas = []
                erro = str(exc)

            def aplicar():
                if erro:
                    import datetime
                    agora = datetime.datetime.now().strftime("%H:%M:%S")
                    self.lbl_acomp_info.config(
                        text="Não foi possível atualizar a planilha às "
                             f"{agora}; os dados anteriores foram mantidos. "
                             "Feche o arquivo no Excel e tente novamente."
                    )
                    return
                self.tabela.delete(*self.tabela.get_children())
                for reg in linhas:
                    status = reg.get("Status", "")
                    self.tabela.insert("", "end", values=(
                        reg.get("Caso", ""), status, reg.get("Progresso", ""),
                        reg.get("Tempo restante", ""), reg.get("Usuário", ""),
                        reg.get("Empresa", ""), reg.get("PC", ""),
                        reg.get("Início", ""), reg.get("Duração", ""),
                        reg.get("Erro", ""), reg.get("Atualizado em", ""),
                    ), tags=(status,))
                import datetime
                agora = datetime.datetime.now().strftime("%H:%M:%S")
                rodando = sum(1 for r in linhas if r.get("Status") == "RODANDO")
                erros = sum(1 for r in linhas if r.get("Status") == "ERRO")
                self.lbl_acomp_info.config(
                    text=f"{len(linhas)} simulação(ões) na planilha | "
                         f"{rodando} rodando | {erros} com erro | "
                         f"lida às {agora} (atualiza sozinha a cada 30 s)"
                )
            try:
                self.root.after(0, aplicar)
            except Exception:
                pass
            finally:
                self._acomp_lock.release()

        threading.Thread(target=trabalho, daemon=True).start()

    # -- status / dados ------------------------------------------------------
    def atualizar_status(self):
        cfg = _carregar_cfg()
        pasta = str(cfg.get("pasta_simulacoes", "") or "")
        self.var_pasta.set(pasta if pasta else "(clique em Escolher…)")

        self.lista_casos.delete(0, "end")
        casos = _listar_casos(pasta) if pasta and os.path.isdir(pasta) else []
        for caso in casos:
            self.lista_casos.insert("end", " " + caso)
        if not casos:
            self.lista_casos.insert("end", " (nenhum caso .fds encontrado)")

        if self.worker is None and pasta and os.path.isdir(pasta):
            caminho_diag = os.path.join(pasta, "fds_diagnosticos.jsonl")
            if self.diagnosticos_path != caminho_diag:
                self.diagnosticos_path = caminho_diag
                self._diagnosticos_assinatura = None
            self._atualizar_aba_diagnosticos()

        diretorio_fds = _diretorio_fds_atual(cfg)
        if diretorio_fds:
            self.lbl_status_fds.config(
                text="🔥 Programa FDS: encontrado ✔", fg=self.CORES["ok"]
            )
        else:
            self.lbl_status_fds.config(
                text="🔥 Programa FDS: NÃO encontrado — instale o FDS/PyroSim",
                fg=self.CORES["destaque"],
            )

        self.lbl_status_fw.config(text="🛡️ Firewall: verificando…", fg=self.CORES["texto2"])
        threading.Thread(
            target=self._checar_firewall_bg, args=(diretorio_fds,), daemon=True
        ).start()

    def _checar_firewall_bg(self, diretorio_fds: str):
        try:
            import firewall_config
            status = firewall_config.status_firewall(diretorio_fds) if diretorio_fds else "desconhecido"
        except Exception:
            status = "desconhecido"

        def aplicar():
            if status == "liberado":
                self.lbl_status_fw.config(
                    text="🛡️ Firewall: liberado — o Windows não vai perguntar ✔",
                    fg=self.CORES["ok"],
                )
            elif status == "pendente":
                self.lbl_status_fw.config(
                    text="🛡️ Firewall: pendente — clique em “Parar perguntas do Windows”",
                    fg=self.CORES["alerta"],
                )
            else:
                self.lbl_status_fw.config(text="🛡️ Firewall: não verificado", fg=self.CORES["texto2"])
        try:
            self.root.after(0, aplicar)
        except Exception:
            pass

    # -- acoes ----------------------------------------------------------------
    def escolher_pasta(self):
        from tkinter import filedialog, messagebox
        pasta = filedialog.askdirectory(
            title="Selecione a pasta raiz com os casos .fds"
        )
        if not pasta:
            return
        pasta = pasta.replace("\\", "/")
        try:
            _salvar_pasta_simulacoes(pasta)
        except Exception as exc:
            messagebox.showerror("Erro", f"Não foi possível salvar a pasta:\n{exc}")
            return
        self.atualizar_status()

    def mostrar_diagnostico(self):
        """Mostra hardware e paralelismo recomendado sem bloquear a janela."""
        from tkinter import messagebox

        if self._diagnostico_em_andamento:
            return

        cfg = _carregar_cfg()
        pasta = str(cfg.get("pasta_simulacoes", "") or "")
        casos = _listar_casos(pasta) if pasta and os.path.isdir(pasta) else []

        # Usa o caso selecionado como referencia. Se nada estiver selecionado,
        # usa o primeiro da lista para entregar uma recomendacao util com um
        # unico clique, sem exigir conhecimento de MPI/OpenMP do usuario.
        caso_rel = casos[0] if casos else ""
        selecao = self.lista_casos.curselection()
        if selecao and 0 <= int(selecao[0]) < len(casos):
            caso_rel = casos[int(selecao[0])]
        arquivo_ref = os.path.join(pasta, caso_rel) if caso_rel else ""

        self._diagnostico_em_andamento = True
        self.btn_diagnostico.config(state="disabled", text="Analisando computador…")
        self.var_barra.set("Analisando o computador e calculando a configuração recomendada…")

        def trabalho():
            try:
                from fds_reader import contar_meshes
                from hw_detector import detectar_hardware, recomendar_config

                hw = detectar_hardware()
                hw_recomendacao = dict(hw)
                if hw.get("is_hybrid") and bool(
                    (cfg.get("hybrid_cpu") or {}).get("usar_e_cores", False)
                ):
                    # Espelha main.aplicar_autoconfig: quando o engenheiro
                    # libera E-cores, a recomendacao usa todos os fisicos.
                    hw_recomendacao["cores_efetivos"] = hw.get(
                        "cores_fisicos", hw.get("cores_efetivos", 1)
                    )
                n_meshes = contar_meshes(arquivo_ref) if arquivo_ref else 0
                rec = recomendar_config(hw_recomendacao, n_meshes)
                diretorio_fds = _diretorio_fds_atual(cfg)

                cpu = str(hw.get("cpu_nome", "") or "").strip()
                if not cpu:
                    cpu = os.environ.get("PROCESSOR_IDENTIFIER", "Não identificado")

                linhas_cpu = [
                    f"Processador: {cpu}",
                    f"Núcleos físicos: {hw.get('cores_fisicos', 1)}",
                    f"Processadores lógicos: {hw.get('cores_logicos', 1)}",
                ]
                if hw.get("is_hybrid"):
                    linhas_cpu.append(
                        f"Núcleos de desempenho/eficiência: "
                        f"{hw.get('p_cores', 0)} P-core(s) + "
                        f"{hw.get('e_cores', 0)} E-core(s)"
                    )
                linhas_cpu.extend((
                    f"Memória: {hw.get('ram_total_gb', 0)} GB total | "
                    f"{hw.get('ram_disponivel_gb', 0)} GB disponível",
                    f"FDS: {'Encontrado em ' + diretorio_fds if diretorio_fds else 'Não encontrado'}",
                ))

                modo = str(rec.get("modo", "OPENMP"))
                hibrido = rec.get("hybrid") or {}
                mpi = int(hibrido.get("mpi_procs", 1) or 1)
                omp = int(hibrido.get("omp_threads", 1) or 1)
                if modo == "OPENMP":
                    distribuicao = (
                        f"1 processo do FDS usando {rec.get('num_cores', 1)} núcleo(s)"
                    )
                elif modo == "MPI":
                    distribuicao = f"{mpi} processo(s) do FDS, 1 núcleo para cada"
                else:
                    distribuicao = (
                        f"{mpi} processo(s) do FDS × {omp} núcleo(s) por processo "
                        f"= {rec.get('num_cores', mpi * omp)} núcleo(s)"
                    )

                if caso_rel:
                    referencia = (
                        f"Caso analisado: {caso_rel}\n"
                        f"Meshes encontradas: {n_meshes}"
                    )
                else:
                    referencia = (
                        "Recomendação geral (nenhum caso .fds foi encontrado na pasta)."
                    )

                ms_cfg = cfg.get("mesh_splitter") or {}
                divisao_efetiva = (
                    bool(ms_cfg.get("ativo", False))
                    and bool(ms_cfg.get("permitir_divisao", False))
                    and not bool(ms_cfg.get("dry_run", False))
                )
                if divisao_efetiva:
                    seguranca_mesh = (
                        "Divisão avançada de meshes: ATIVA. Compare o caso "
                        "original e o otimizado e confira VELOCITY_ERROR nas "
                        "interfaces antes de usar os resultados em projeto."
                    )
                else:
                    seguranca_mesh = (
                        "Divisão avançada de meshes: desativada (modo seguro). "
                        "O programa preserva as interfaces e a ordem das meshes."
                    )

                texto = (
                    "COMPUTADOR\n"
                    + "\n".join(linhas_cpu)
                    + "\n\nCONFIGURAÇÃO INICIAL RECOMENDADA\n"
                    + referencia
                    + f"\nModo: {modo}\nDistribuição: {distribuicao}\n\n"
                    + str(rec.get("motivo", ""))
                    + "\n\nSEGURANÇA DO MODELO\n"
                    + seguranca_mesh
                    + "\n\nO teste curto antes da execução pode confirmar ou ajustar "
                      "automaticamente essa recomendação para cada caso."
                )
                erro = None
            except Exception as exc:
                texto = ""
                erro = str(exc)

            def concluir():
                self._diagnostico_em_andamento = False
                self.btn_diagnostico.config(
                    state="normal" if self.worker is None else "disabled",
                    text="🖥️ Diagnóstico do computador",
                )
                if erro:
                    self.var_barra.set("Não foi possível concluir o diagnóstico.")
                    messagebox.showerror(
                        "Diagnóstico do computador",
                        f"Não foi possível analisar este computador:\n{erro}",
                    )
                else:
                    self.var_barra.set("Diagnóstico concluído.")
                    messagebox.showinfo("Diagnóstico do computador", texto)

            try:
                self.root.after(0, concluir)
            except Exception:
                pass

        threading.Thread(target=trabalho, daemon=True).start()

    def rodar(self):
        from tkinter import messagebox
        cfg = _carregar_cfg()
        pasta = str(cfg.get("pasta_simulacoes", "") or "")
        if not pasta or not os.path.isdir(pasta):
            messagebox.showwarning(
                "Escolha a pasta",
                "Antes de rodar, escolha a pasta com os casos .fds\n"
                "(botão “Escolher…” no topo da janela).",
            )
            return
        if not _listar_casos(pasta):
            messagebox.showwarning(
                "Nenhum caso encontrado",
                "Não encontrei nenhum arquivo .fds na pasta escolhida.",
            )
            return

        n = len(_listar_casos(pasta))
        otim = self.var_otimizar.get()
        ms_cfg = cfg.get("mesh_splitter") or {}
        divisao_ativa = (
            bool(ms_cfg.get("ativo", False))
            and bool(ms_cfg.get("permitir_divisao", False))
            and not bool(ms_cfg.get("dry_run", False))
        )
        aviso_divisao = (
            "\n\nATENÇÃO: a divisão avançada de meshes está ATIVA. Ela cria "
            "novas interfaces numéricas. Use somente se você já planejou "
            "comparar o caso original e o otimizado e verificar "
            "VELOCITY_ERROR no arquivo de saída."
            if divisao_ativa else ""
        )
        msg = (
            f"Vou rodar {n} simulação(ões) da pasta:\n{pasta}\n\n"
            + ("Antes, farei testes curtos para descobrir a configuração "
               "mais rápida (recomendado).\n\n" if otim else "")
            + "As simulações podem levar horas."
            + aviso_divisao
            + "\n\nComeçar agora?"
        )
        if not messagebox.askyesno("Confirmar", msg):
            return

        argumentos = ["--rodar", "--config", CONFIG_PATH,
                      "--otimizador", "1" if otim else "0"]
        self._spawn_worker(argumentos, pasta)

    def _spawn_worker(self, argumentos: list[str], pasta_log: str):
        from tkinter import messagebox
        from case_control import novo_caminho_controle

        self._limpar_controle_caso()
        self.controle_caso_path = novo_caminho_controle()
        argumentos = list(argumentos) + [
            "--controle-caso", self.controle_caso_path,
        ]
        if getattr(sys, "frozen", False):
            comando = [sys.executable] + argumentos
        else:
            comando = [sys.executable, os.path.abspath(__file__)] + argumentos

        # Comeca a "seguir" o log a partir do fim atual
        self.log_path = os.path.join(pasta_log, "fds_run.log")
        self.diagnosticos_path = os.path.join(
            pasta_log, "fds_diagnosticos.jsonl"
        )
        try:
            os.remove(self.diagnosticos_path)
        except FileNotFoundError:
            pass
        except OSError:
            # O worker tambem trunca o arquivo ao iniciar a sessao.
            pass
        self._limpar_aba_diagnosticos()
        self._abrir_log_no_fim()

        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.worker = subprocess.Popen(
                comando, cwd=BASE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception as exc:
            messagebox.showerror("Erro", f"Não foi possível iniciar a execução:\n{exc}")
            self.worker = None
            self._limpar_controle_caso()
            return

        try:
            from process_guard import proteger_processo
            self.job = proteger_processo(self.worker)
        except Exception:
            self.job = None

        self.total_casos_fila = 0
        self.caso_atual = None
        self._parada_caso_solicitada = False
        self._processos_pendentes = []
        self._log_gui("── Execução iniciada ──")
        self.var_barra.set("Rodando… preparando a fila.")
        self._definir_estado_rodando(True)

    def _abrir_log_no_fim(self):
        self._fechar_log()
        try:
            self.log_handle = open(self.log_path, "r", encoding="utf-8", errors="replace")
            self.log_handle.seek(0, os.SEEK_END)
        except OSError:
            self.log_handle = None  # arquivo ainda nao existe; tenta no tick

    def _fechar_log(self):
        if self.log_handle is not None:
            try:
                self.log_handle.close()
            except OSError:
                pass
            self.log_handle = None

    @staticmethod
    def _fechar_handle_job(job) -> None:
        """Libera um Job Object mesmo se o modulo auxiliar falhar ao importar."""
        if job is None:
            return
        try:
            from process_guard import fechar_job
            fechar_job(job)
        except Exception:
            # Ultimo recurso para nao vazar um handle nativo do Windows.
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(job)
            except Exception:
                pass

    def _fechar_job_atual(self) -> None:
        # Zera primeiro para impedir uma segunda tentativa de fechar o mesmo
        # handle caso outro callback da interface seja executado em seguida.
        job = self.job
        self.job = None
        self._fechar_handle_job(job)

    def _limpar_controle_caso(self) -> None:
        caminho = self.controle_caso_path
        self.controle_caso_path = None
        if caminho:
            try:
                from case_control import limpar_solicitacao
                limpar_solicitacao(caminho)
            except Exception:
                pass

    def parar_caso_atual(self):
        """Solicita ao worker que encerre somente o FDS/teste deste caso."""
        from tkinter import messagebox
        if (
            self.worker is None
            or self.caso_atual is None
            or self._parada_caso_solicitada
            or self._cancelando
        ):
            return

        nome = str(self.caso_atual.get("nome", "simulação atual"))
        if not messagebox.askyesno(
            "Parar somente o caso atual",
            f"Parar esta simulação?\n\n{nome}\n\n"
            "O progresso deste caso será perdido, mas a fila continuará "
            "automaticamente com a próxima simulação.",
        ):
            return

        try:
            from case_control import solicitar_pulo
            solicitar_pulo(
                str(self.controle_caso_path or ""),
                int(self.caso_atual["indice"]),
                nome,
            )
        except Exception as exc:
            messagebox.showerror(
                "Não foi possível parar o caso",
                f"A solicitação não pôde ser enviada:\n{exc}",
            )
            return

        self._parada_caso_solicitada = True
        self.btn_parar_caso.config(state="disabled", text="⏳  Parando caso…")
        self.var_barra.set(
            f"Interrompendo somente {nome}. A fila seguirá para o próximo caso…"
        )
        self._log_gui(
            f"── Parada solicitada para {nome}; os próximos casos continuarão ──"
        )

    def cancelar(self):
        from tkinter import messagebox
        if self.worker is None or self._cancelando:
            return
        if not messagebox.askyesno(
            "Cancelar toda a fila",
            "Interromper a simulação atual e cancelar todos os casos restantes?\n"
            "Para parar somente o caso atual, use “Parar caso atual”.",
        ):
            return
        self.btn_cancelar.config(state="disabled")
        self.btn_parar_caso.config(state="disabled")
        self.var_barra.set("Cancelando e encerrando os processos do FDS…")
        self._cancelando = True

        def trabalho():
            sucesso = self._matar_worker()

            def concluir():
                self._cancelando = False
                if sucesso:
                    self._log_gui("── Execução CANCELADA pelo usuário ──")
                    self.var_barra.set("Cancelado.")
                    self._definir_estado_rodando(False)
                    self._marcar_fila_cancelada(assinc=True)
                else:
                    self.var_barra.set(
                        "Não consegui encerrar todos os processos; tente cancelar novamente."
                    )
                    self.btn_cancelar.config(state="normal")
                    if self.caso_atual is not None and not self._parada_caso_solicitada:
                        self.btn_parar_caso.config(state="normal")
                    messagebox.showerror(
                        "Cancelamento incompleto",
                        "Um ou mais processos do FDS ainda estão ativos. "
                        "O programa não vai liberar uma nova fila enquanto "
                        "eles não forem encerrados. Tente Cancelar novamente "
                        "ou use o Gerenciador de Tarefas do Windows.",
                    )

            try:
                self.root.after(0, concluir)
            except Exception:
                pass

        threading.Thread(target=trabalho, daemon=True).start()

    def _marcar_fila_cancelada(self, assinc: bool = True) -> None:
        """Reconcilia a planilha depois do encerramento forcado do worker."""
        cfg = _carregar_cfg()
        pasta = str(cfg.get("pasta_simulacoes", "") or "")
        arquivos = [
            os.path.join(pasta, relativo)
            for relativo in _listar_casos(pasta)
        ] if pasta and os.path.isdir(pasta) else []

        def trabalho():
            try:
                from monitor_xlsx import MonitorPlanilha
                MonitorPlanilha(cfg).cancelar_fila(arquivos)
            except Exception:
                pass

        if assinc:
            threading.Thread(target=trabalho, daemon=True).start()
        else:
            trabalho()

    def _matar_worker(self) -> bool:
        worker = self.worker
        if worker is None:
            self._fechar_job_atual()
            self._fechar_log()
            self._limpar_controle_caso()
            return True

        # Guarda os descendentes antes de qualquer tentativa. Isso permite
        # encerrar fds.exe/hydra_pmi_proxy ate quando o PID do worker some e
        # o fallback por taskkill ja nao consegue mais encontrar a raiz.
        psutil_mod = None
        processos = list(self._processos_pendentes)
        try:
            import psutil as psutil_mod
            raiz = psutil_mod.Process(worker.pid)
            processos.extend(raiz.children(recursive=True) + [raiz])
        except Exception:
            pass

        # Um mesmo processo pode vir da tentativa anterior e da nova captura.
        unicos = {}
        for processo in processos:
            try:
                unicos[processo.pid] = processo
            except Exception:
                pass
        processos = list(unicos.values())

        job = self.job
        self.job = None
        if job is not None:
            try:
                from process_guard import matar_job
                matar_job(job)
            except Exception:
                pass
            finally:
                # KILL_ON_JOB_CLOSE oferece uma segunda chance de encerrar a
                # arvore e sempre libera o handle, inclusive em falhas.
                self._fechar_handle_job(job)

            try:
                worker.wait(timeout=2)
            except Exception:
                pass

        def ainda_vivo(processo) -> bool:
            try:
                return processo.is_running()
            except Exception:
                return False

        precisa_fallback = worker.poll() is None or any(
            ainda_vivo(processo) for processo in processos
        )
        if precisa_fallback:
            # Primeiro usa o encerramento em arvore do Windows. Em seguida,
            # os PIDs capturados acima sao tratados individualmente para o
            # caso conhecido em que a raiz MPI termina antes dos filhos.
            try:
                from process_guard import matar_arvore_processos
                matar_arvore_processos(worker.pid)
            except Exception:
                pass

            if psutil_mod is not None:
                for processo in processos:  # filhos primeiro; raiz por ultimo
                    try:
                        if processo.is_running():
                            processo.kill()
                    except Exception:
                        pass
                try:
                    psutil_mod.wait_procs(processos, timeout=2)
                except Exception:
                    pass

        # Fallback final para a propria raiz, tambem util fora do Windows.
        if worker.poll() is None:
            try:
                worker.terminate()
                worker.wait(timeout=2)
            except Exception:
                try:
                    worker.kill()
                    worker.wait(timeout=2)
                except Exception:
                    pass

        sobreviventes = [processo for processo in processos if ainda_vivo(processo)]
        sucesso = worker.poll() is not None and not sobreviventes
        self._processos_pendentes = sobreviventes
        if sucesso:
            self.worker = None
            self.caso_atual = None
            self._parada_caso_solicitada = False
            self._limpar_controle_caso()
        self._fechar_log()
        return sucesso

    def liberar_firewall(self):
        from tkinter import messagebox
        cfg = _carregar_cfg()
        diretorio_fds = _diretorio_fds_atual(cfg)
        if not diretorio_fds:
            messagebox.showerror(
                "FDS não encontrado",
                "Não encontrei a instalação do FDS neste computador.",
            )
            return

        import firewall_config
        if not firewall_config.eh_admin():
            messagebox.showinfo(
                "Permissão do Windows",
                "O Windows vai abrir UMA janela pedindo permissão de "
                "administrador.\nClique em “Sim” para concluir.",
            )

        pasta = str(cfg.get("pasta_simulacoes", "") or "")
        pastas_projetos = []
        if pasta and os.path.isdir(pasta):
            for raiz, _dirs, nomes in os.walk(pasta):
                if any(n.lower().endswith(".fds") for n in nomes):
                    pastas_projetos.append(raiz)

        self.btn_fw.config(state="disabled", text="Liberando…")

        def trabalho():
            try:
                ok, msg = firewall_config.liberar_firewall_interativo(
                    diretorio_fds, pastas_projetos
                )
            except Exception as exc:
                ok, msg = False, f"Falhou: {exc}"

            def concluir():
                self.btn_fw.config(state="normal", text="🛡️ Parar perguntas do Windows")
                (messagebox.showinfo if ok else messagebox.showwarning)(
                    "Firewall do Windows", msg
                )
                self.atualizar_status()
            try:
                self.root.after(0, concluir)
            except Exception:
                pass

        threading.Thread(target=trabalho, daemon=True).start()

    # -- loop de acompanhamento ------------------------------------------------
    def _agendar_tick(self):
        self.root.after(500, self._tick)

    def _tick(self):
        try:
            if self.worker is not None:
                if self._cancelando:
                    return
                if self.log_handle is None and self.log_path:
                    self._abrir_log_no_fim_se_existir()
                self._consumir_log()
                self._atualizar_aba_diagnosticos()

                if self.worker.poll() is not None:
                    pendentes_vivos = []
                    for processo in self._processos_pendentes:
                        try:
                            if processo.is_running():
                                pendentes_vivos.append(processo)
                        except Exception:
                            pass
                    self._processos_pendentes = pendentes_vivos
                    if pendentes_vivos:
                        self.var_barra.set(
                            "Há processos do FDS ainda ativos; use Cancelar novamente."
                        )
                        self.btn_cancelar.config(state="normal")
                        return
                    codigo = self.worker.returncode
                    self._consumir_log()
                    self._atualizar_aba_diagnosticos(forcar=True)
                    # A fila terminou por conta propria: o Job Object nao e
                    # mais necessario e seu handle nativo deve ser liberado.
                    self._fechar_job_atual()
                    self.worker = None
                    self.caso_atual = None
                    self._parada_caso_solicitada = False
                    self._limpar_controle_caso()
                    self._fechar_log()
                    self._definir_estado_rodando(False)
                    erros = sum(
                        evento.get("tipo") != "interrompida"
                        for evento in self._diagnosticos
                    )
                    interrompidas = sum(
                        evento.get("tipo") == "interrompida"
                        for evento in self._diagnosticos
                    )
                    if codigo == 0:
                        if interrompidas:
                            self._log_gui(
                                f"── Fila finalizada com {interrompidas} caso(s) "
                                "interrompido(s) pelo usuário ──"
                            )
                            self.var_barra.set(
                                "Fila concluída; os casos interrompidos podem ser "
                                "consultados em “Log e erros”."
                            )
                        else:
                            self._log_gui("── Fila finalizada ──")
                            self.var_barra.set("Concluído! Todos os casos finalizaram.")
                    else:
                        if erros:
                            self._log_gui(
                                f"── Fila concluída com {erros} caso(s) com erro; "
                                "veja a explicação na aba Log e erros ──"
                            )
                            self.var_barra.set(
                                f"Fila concluída com {erros} erro(s). A causa e os "
                                "passos de correção estão em “Log e erros”."
                            )
                            self.notebook.select(self.aba_log_erros)
                        else:
                            self._log_gui(
                                "── A fila não pôde ser concluída; consulte o log técnico ──"
                            )
                            self.var_barra.set(
                                "A fila não foi concluída. Consulte “Log e erros”."
                            )
        finally:
            self._agendar_tick()

    def _abrir_log_no_fim_se_existir(self):
        # Na 1a execucao o fds_run.log pode nao existir ainda: comeca do
        # inicio assim que ele aparecer (para nao perder as 1as linhas).
        try:
            self.log_handle = open(self.log_path, "r", encoding="utf-8", errors="replace")
        except OSError:
            self.log_handle = None

    def _consumir_log(self):
        if self.log_handle is None:
            return
        try:
            linhas = self.log_handle.readlines()
        except OSError:
            return
        for linha in linhas:
            self._processar_linha_log(linha.rstrip("\n"))

    def _processar_linha_log(self, linha: str):
        self._log_tecnico_gui(linha)
        texto = _RE_PREFIXO_LOG.sub("", linha)
        if not texto.strip():
            return

        eh_detalhe_fds = texto.startswith("FDS | ")
        eh_erro_fatal = bool(_RE_ERRO_FATAL_FDS.search(texto)) or any(
            frase in texto.upper()
            for frase in ("FDS WAS IMPROPERLY SET-UP", "FDS STOPPED")
        )
        # A mensagem que explica a falha nunca fica escondida pelo modo
        # simplificado; somente as milhares de linhas tecnicas normais somem.
        if not eh_detalhe_fds or self.var_detalhes.get() or eh_erro_fatal:
            self._log_gui(texto)

        inicio = _RE_INICIO_CASO.search(texto)
        if inicio:
            indice, total, nome = inicio.groups()
            self.caso_atual = {
                "indice": int(indice),
                "total": int(total),
                "nome": nome.strip(),
            }
            self._parada_caso_solicitada = False
            self.btn_parar_caso.config(
                state="normal", text="⏭  Parar caso atual"
            )

        if (
            texto.startswith("[SUCESSO]")
            or texto.startswith("[PULADO]")
            or texto.startswith("[ERRO] FDS encerrou")
            or texto.startswith("Falha antes/durante a preparacao")
        ):
            self.caso_atual = None
            self._parada_caso_solicitada = False
            self.btn_parar_caso.config(
                state="disabled", text="⏭  Parar caso atual"
            )

        m = _RE_PROGRESSO_FILA.search(texto)
        if m:
            self.total_casos_fila = int(m.group(2))
            self.var_barra.set(
                f"Rodando simulação {m.group(1)} de {m.group(2)}…"
            )

        m = _RE_RODANDO.search(texto)
        if m:
            nome, passo, sim_s, decorrido, restante = m.groups()
            barra = f"▶ {nome} | tempo simulado {sim_s}s | decorrido {decorrido}"
            if restante:
                barra += f" | restante ~{restante}"
            self.var_barra.set(barra)

    def _log_gui(self, texto: str):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", texto + "\n")
        # Limita o widget a ~4000 linhas para nao pesar em filas longas
        excesso = int(float(self.txt_log.index("end-1c").split(".")[0])) - 4000
        if excesso > 0:
            self.txt_log.delete("1.0", f"{excesso + 1}.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _definir_estado_rodando(self, rodando: bool):
        estado_botoes = "disabled" if rodando else "normal"
        self.btn_rodar.config(state=estado_botoes)
        self.btn_pasta.config(state=estado_botoes)
        self.btn_fw.config(state=estado_botoes)
        self.btn_diagnostico.config(
            state="disabled" if rodando or self._diagnostico_em_andamento else "normal"
        )
        self.btn_cancelar.config(state="normal" if rodando else "disabled")
        pode_parar_caso = (
            rodando
            and self.caso_atual is not None
            and not self._parada_caso_solicitada
            and not self._cancelando
        )
        self.btn_parar_caso.config(
            state="normal" if pode_parar_caso else "disabled",
            text=(
                "⏳  Parando caso…"
                if self._parada_caso_solicitada and rodando
                else "⏭  Parar caso atual"
            ),
        )

    def ao_fechar(self):
        from tkinter import messagebox
        if self._cancelando:
            messagebox.showinfo(
                "Cancelamento em andamento",
                "Aguarde o encerramento dos processos do FDS antes de fechar.",
            )
            return
        if self.worker is not None:
            if not messagebox.askyesno(
                "Sair",
                "As simulações ainda estão rodando.\n"
                "Fechar a janela vai INTERROMPER tudo. Fechar mesmo assim?",
            ):
                return
            if not self._matar_worker():
                messagebox.showerror(
                    "Ainda há processos ativos",
                    "Não foi possível encerrar toda a árvore do FDS. "
                    "A janela permanecerá aberta para evitar deixar uma "
                    "simulação órfã.",
                )
                self.btn_cancelar.config(state="normal")
                return
            self._marcar_fila_cancelada(assinc=False)
        else:
            self._fechar_job_atual()
            self._limpar_controle_caso()
        self.root.destroy()


def executar_gui() -> int:
    # Texto nitido em telas com escala (125%/150%) no Windows
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    import tkinter as tk
    root = tk.Tk()

    # Primeira execucao: exige o aceite da EULA antes de mostrar o app.
    # A tela e desenhada na propria janela root (visivel, com icone na
    # barra de tarefas) e limpa apos o aceite.
    import eula
    if not eula.garantir_aceite_gui(root):
        root.destroy()
        return 0

    AppGUI(root)
    root.mainloop()
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--rodar" in argv or "--testar" in argv:
        return executar_modo_worker(argv)

    try:
        return executar_gui()
    except Exception:
        # Exe sem console: um erro na GUI seria invisivel (processo some
        # ou fica fantasma). Registra em arquivo para diagnostico.
        import traceback
        try:
            with open(os.path.join(BASE_DIR, "fds_gui_erro.log"), "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
