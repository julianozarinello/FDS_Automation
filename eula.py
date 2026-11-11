# eula.py — Aceite da licença de uso (EULA) no primeiro uso
#
# Fluxo:
#   - Na primeira execucao (GUI ou menu), o usuario precisa aceitar a
#     EULA. O aceite e registrado em 'eula_aceite.txt' (versao da EULA,
#     data/hora, usuario do Windows e nome do PC) ao lado do programa —
#     ou em %LOCALAPPDATA%\FDS_Automation se a pasta nao for gravavel.
#   - Execucoes seguintes nao perguntam de novo, a menos que a EULA mude
#     de versao (EULA_VERSAO abaixo).
#   - O modo worker (--rodar/--testar, lancado pela propria GUI) nunca
#     bloqueia esperando aceite: quem o inicia ja passou pela tela.
#
# O texto integral vem do EULA.txt (embutido no .exe via PyInstaller e
# tambem presente na pasta de distribuicao).
import datetime
import getpass
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Versao da EULA vigente. Ao publicar uma EULA nova, aumente este numero
# para que todos os usuarios precisem aceitar de novo.
EULA_VERSAO = "1.0"

NOME_ARQ_ACEITE = "eula_aceite.txt"

BASE_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)

_RESUMO_FALLBACK = (
    "FDS Automation © 2026 JZ Engenharia. Todos os direitos reservados.\n"
    "Software licenciado para uso exclusivo na RME Consultoria &\n"
    "Engenharia. É proibido copiar, distribuir, sublicenciar ou fazer\n"
    "engenharia reversa. Fornecido \"no estado em que se encontra\",\n"
    "sem garantias; a responsabilidade técnica pelas simulações é do\n"
    "engenheiro responsável. (Arquivo EULA.txt completo não encontrado —\n"
    "solicite-o à JZ Engenharia.)"
)


def _dir_dados_usuario() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "FDS_Automation")


def _caminhos_aceite() -> list[str]:
    return [
        os.path.join(BASE_DIR, NOME_ARQ_ACEITE),
        os.path.join(_dir_dados_usuario(), NOME_ARQ_ACEITE),
    ]


def texto_eula() -> str:
    """Texto integral da EULA (embutida no exe, ou da pasta do programa)."""
    candidatos = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidatos.append(os.path.join(sys._MEIPASS, "EULA.txt"))
    candidatos.append(os.path.join(BASE_DIR, "EULA.txt"))

    for caminho in candidatos:
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return _RESUMO_FALLBACK


def eula_aceita() -> bool:
    """
    True se a EULA (na versao vigente) ja foi aceita NESTE computador.
    O registro guarda o nome do PC: se a pasta do programa for copiada
    para outra maquina, o aceite nao vai junto — a EULA e exibida de
    novo para o novo usuario.
    """
    pc_atual = os.environ.get("COMPUTERNAME", "") or "desconhecido"
    for caminho in _caminhos_aceite():
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                conteudo = f.read()
        except OSError:
            continue
        if (
            f"Versao da EULA: {EULA_VERSAO}" in conteudo
            and f"Computador: {pc_atual}" in conteudo
        ):
            return True
    return False


def registrar_aceite() -> str | None:
    """
    Grava o registro do aceite (versao, data, usuario, PC). Tenta ao lado
    do programa; sem permissao, usa %LOCALAPPDATA%\\FDS_Automation.
    Retorna o caminho gravado ou None se falhou nos dois.
    """
    try:
        usuario = getpass.getuser()
    except Exception:
        usuario = os.environ.get("USERNAME", "desconhecido")
    pc = os.environ.get("COMPUTERNAME", "") or "desconhecido"
    agora = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    conteudo = (
        "FDS Automation — Registro de Aceite da EULA\n"
        "=============================================\n"
        f"Versao da EULA: {EULA_VERSAO}\n"
        f"Aceita em: {agora}\n"
        f"Usuario do Windows: {usuario}\n"
        f"Computador: {pc}\n"
        "\n"
        "Ao aceitar, o usuario declarou concordar com os termos do\n"
        "EULA.txt (© JZ Engenharia — uso licenciado a RME Consultoria\n"
        "& Engenharia).\n"
    )

    for caminho in _caminhos_aceite():
        try:
            os.makedirs(os.path.dirname(caminho), exist_ok=True)
            with open(caminho, "w", encoding="utf-8") as f:
                f.write(conteudo)
            return caminho
        except OSError:
            continue

    logger.warning("Nao foi possivel registrar o aceite da EULA em disco.")
    return None


# ---------------------------------------------------------------------------
# Aceite no console (menu de terminal)
# ---------------------------------------------------------------------------
def garantir_aceite_console() -> bool:
    """
    Se necessario, exibe a EULA no terminal e pede o aceite.
    Retorna True se o uso pode prosseguir.
    """
    if eula_aceita():
        return True

    print("=" * 64)
    print("  CONTRATO DE LICENÇA DE USO (EULA) — PRIMEIRA EXECUÇÃO")
    print("=" * 64)
    print()
    print(texto_eula())
    print("=" * 64)
    print("Para usar o FDS Automation você precisa aceitar os termos")
    print("acima (© JZ Engenharia — licenciado à RME Consultoria &")
    print("Engenharia).")
    print()
    try:
        resposta = input("Digite ACEITO para concordar (ou ENTER para sair): ")
    except (EOFError, KeyboardInterrupt):
        return False

    if resposta.strip().upper() in ("ACEITO", "ACEITA", "SIM", "S", "YES", "Y"):
        caminho = registrar_aceite()
        print()
        print("✅ Licença aceita." + (f" Registro: {caminho}" if caminho else ""))
        try:
            input("Pressione ENTER para continuar...")
        except (EOFError, KeyboardInterrupt):
            pass
        return True

    print("\nLicença não aceita. O programa será encerrado.")
    return False


# ---------------------------------------------------------------------------
# Aceite na interface gráfica
# ---------------------------------------------------------------------------
def garantir_aceite_gui(root) -> bool:
    """
    Se necessario, desenha a tela da EULA NA PROPRIA janela `root`
    (visivel, com icone na barra de tarefas) e espera a decisao.
    Ao aceitar, limpa a janela para o aplicativo ser montado nela.
    Retorna True se o uso pode prosseguir.

    Nao usa Toplevel/grab_set: uma janela-filha de um root oculto nao
    ganha icone na barra de tarefas no Windows, e grab_set falha se a
    janela ainda nao esta visivel — foi exatamente o bug que deixava o
    programa rodando invisivel.
    """
    if eula_aceita():
        return True

    import tkinter as tk

    root.deiconify()
    root.title("Licença de Uso — FDS Automation")
    root.geometry("760x600")
    root.minsize(620, 480)
    root.configure(bg="#f4f4f6")

    var_decidido = tk.IntVar(value=0)  # 0 = aguardando, 1 = aceito, 2 = recusado

    def _recusar():
        var_decidido.set(2)

    root.protocol("WM_DELETE_WINDOW", _recusar)  # fechar a janela = recusar

    topo = tk.Frame(root, bg="#c62828")
    topo.pack(fill="x")
    tk.Label(
        topo, text="📜 Contrato de Licença de Uso (EULA)",
        font=("Segoe UI", 14, "bold"), bg="#c62828", fg="white",
        padx=14, pady=8,
    ).pack(side="left")

    tk.Label(
        root,
        text="Leia os termos abaixo. O software pertence à JZ Engenharia e é "
             "licenciado para uso na RME Consultoria & Engenharia.",
        font=("Segoe UI", 9), bg="#f4f4f6", fg="#424242",
        wraplength=700, justify="left", padx=14, pady=6,
    ).pack(fill="x")

    moldura = tk.Frame(root, bg="#f4f4f6")
    moldura.pack(fill="both", expand=True, padx=14)
    txt = tk.Text(moldura, font=("Consolas", 9), wrap="word")
    rolagem = tk.Scrollbar(moldura, command=txt.yview)
    txt.configure(yscrollcommand=rolagem.set)
    txt.insert("1.0", texto_eula())
    txt.configure(state="disabled")
    rolagem.pack(side="right", fill="y")
    txt.pack(fill="both", expand=True)

    var_li = tk.BooleanVar(value=False)

    def _atualizar_botao():
        btn_aceitar.config(state="normal" if var_li.get() else "disabled")

    tk.Checkbutton(
        root, text="Li e aceito os termos da licença de uso",
        variable=var_li, command=_atualizar_botao,
        font=("Segoe UI", 10, "bold"), bg="#f4f4f6", fg="#212121",
    ).pack(pady=(8, 2))

    botoes = tk.Frame(root, bg="#f4f4f6")
    botoes.pack(pady=(2, 12))

    def _aceitar():
        registrar_aceite()
        var_decidido.set(1)

    btn_aceitar = tk.Button(
        botoes, text="✔  Aceitar e continuar", command=_aceitar,
        font=("Segoe UI", 11, "bold"), bg="#2e7d32", fg="white",
        activebackground="#1b5e20", activeforeground="white",
        padx=16, pady=5, state="disabled",
    )
    btn_aceitar.pack(side="left", padx=8)
    tk.Button(
        botoes, text="✖  Não aceito (sair)", command=_recusar,
        font=("Segoe UI", 10), padx=12, pady=5,
    ).pack(side="left", padx=8)

    root.lift()
    root.focus_force()
    root.wait_variable(var_decidido)  # bloqueia ate Aceitar/Recusar

    aceito = var_decidido.get() == 1
    if aceito:
        # Limpa a janela para o aplicativo principal ser montado nela
        for filho in root.winfo_children():
            filho.destroy()
        root.protocol("WM_DELETE_WINDOW", root.destroy)
    return aceito
