# firewall_config.py — libera FDS/MPI no Firewall do Windows
#
# Por que isso existe: mpiexec, hydra_service, hydra_pmi_proxy e fds.exe
# abrem portas de comunicacao (mesmo em maquina unica, o Intel MPI usa
# sockets locais). O Firewall do Windows pergunta "Deseja permitir que
# este aplicativo se comunique?" para CADA executavel em CADA caminho —
# e como o FDS Automation copia os binarios para cada pasta de simulacao,
# o Windows pergunta de novo a cada projeto.
#
# Este modulo cadastra regras permanentes de firewall (netsh advfirewall)
# para os executaveis do FDS/MPI, tanto na pasta de instalacao quanto nas
# pastas de simulacao. Com as regras cadastradas, o Windows para de
# perguntar. Cadastrar regra exige privilegio de administrador: quando o
# programa nao esta elevado, geramos um script e pedimos elevacao (UAC)
# UMA unica vez.
#
# Tambem remove a marcacao "arquivo baixado da internet" (Zone.Identifier)
# dos executaveis copiados, que em alguns PCs dispara o aviso do
# SmartScreen ("O Windows protegeu o computador").
import ctypes
import hashlib
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Executaveis que o Windows costuma questionar. fds_openmp.exe nao abre
# rede, mas incluir e inofensivo e evita qualquer aviso residual.
NOMES_EXE_FDS = (
    "fds.exe",
    "fds_openmp.exe",
    "mpiexec.exe",
    "hydra_service.exe",
    "hydra_pmi_proxy.exe",
    "smpd.exe",
)

PREFIXO_REGRA = "FDS Automation"

# Sem esta flag, cada chamada ao netsh/powershell pisca uma janela de
# cmd na frente da interface grafica.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Caminhos ja confirmados/cadastrados nesta sessao (evita re-consultar o
# netsh a cada simulacao da fila).
_REGRAS_OK_SESSAO: set[str] = set()
_AVISO_DADO = False


def eh_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _nome_regra(caminho_exe: str) -> str:
    """Nome unico e estavel por caminho (o mesmo exe em pastas diferentes
    precisa de regras separadas)."""
    caminho_norm = os.path.normcase(os.path.abspath(caminho_exe))
    sufixo = hashlib.sha1(caminho_norm.encode("utf-8")).hexdigest()[:8]
    return f"{PREFIXO_REGRA} - {os.path.basename(caminho_norm)} - {sufixo}"


def listar_exes_fds(diretorio_fds: str, pastas_projetos: list[str] | None = None) -> list[str]:
    """
    Monta a lista de caminhos de executaveis FDS/MPI a liberar:
      - os que existem na instalacao (diretorio_fds e diretorio_fds/mpi);
      - os esperados em cada pasta de simulacao (mesmo que a copia ainda
        nao tenha acontecido — a regra pode ser cadastrada antes, pois o
        firewall guarda apenas o caminho).
    """
    caminhos: list[str] = []

    for pasta in (diretorio_fds, os.path.join(diretorio_fds or "", "mpi")):
        if not pasta or not os.path.isdir(pasta):
            continue
        for nome in NOMES_EXE_FDS:
            caminho = os.path.join(pasta, nome)
            if os.path.exists(caminho):
                caminhos.append(caminho)

    for pasta in pastas_projetos or []:
        if not pasta:
            continue
        for nome in NOMES_EXE_FDS:
            caminhos.append(os.path.join(pasta, nome))

    # remove duplicatas preservando a ordem
    vistos: set[str] = set()
    unicos = []
    for c in caminhos:
        chave = os.path.normcase(os.path.abspath(c))
        if chave in vistos:
            continue
        vistos.add(chave)
        unicos.append(c)
    return unicos


def regra_existe(caminho_exe: str) -> bool:
    chave = os.path.normcase(os.path.abspath(caminho_exe))
    if chave in _REGRAS_OK_SESSAO:
        return True
    try:
        resultado = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={_nome_regra(caminho_exe)}"],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        existe = resultado.returncode == 0
    except Exception:
        return False
    if existe:
        _REGRAS_OK_SESSAO.add(chave)
    return existe


def regras_faltantes(caminhos_exe: list[str]) -> list[str]:
    return [c for c in caminhos_exe if not regra_existe(c)]


def desbloquear_motw(caminhos_exe: list[str]) -> int:
    """
    Remove o stream :Zone.Identifier ("arquivo veio da internet") dos
    executaveis que existem. Nao precisa de administrador. Retorna
    quantos arquivos foram desbloqueados.
    """
    desbloqueados = 0
    for caminho in caminhos_exe:
        if not os.path.exists(caminho):
            continue
        try:
            os.remove(caminho + ":Zone.Identifier")
            desbloqueados += 1
        except OSError:
            pass  # stream nao existe (caso comum) ou sem permissao
    return desbloqueados


def _comandos_netsh(caminhos_exe: list[str]) -> list[list[str]]:
    comandos = []
    for caminho in caminhos_exe:
        caminho_abs = os.path.abspath(caminho)
        comandos.append([
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={_nome_regra(caminho_abs)}",
            "dir=in", "action=allow",
            f"program={caminho_abs}",
            "enable=yes", "profile=any",
        ])
    return comandos


def adicionar_regras(caminhos_exe: list[str]) -> int:
    """
    Cadastra as regras diretamente (exige processo ja elevado).
    Retorna quantas regras foram cadastradas com sucesso.
    """
    ok = 0
    for caminho, comando in zip(caminhos_exe, _comandos_netsh(caminhos_exe)):
        try:
            resultado = subprocess.run(
                comando, capture_output=True, text=True, timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
            if resultado.returncode == 0:
                ok += 1
                _REGRAS_OK_SESSAO.add(os.path.normcase(os.path.abspath(caminho)))
        except Exception:
            logger.debug(f"Falha ao cadastrar regra de firewall: {comando}")
    return ok


def adicionar_regras_com_elevacao(caminhos_exe: list[str]) -> bool:
    """
    Gera um script PowerShell com os comandos netsh e o executa elevado
    (UAC) — o usuario confirma UMA vez e as regras ficam permanentes.
    Retorna True se o script elevado terminou (confirmar sucesso real
    exige re-checar as regras com `regras_faltantes`).
    """
    if not caminhos_exe:
        return True

    def ps_quote(valor: str) -> str:
        # Aspas simples sao escapadas por duplicacao no PowerShell.
        return "'" + str(valor).replace("'", "''") + "'"

    linhas = ["$ErrorActionPreference = 'Continue'"]
    for comando in _comandos_netsh(caminhos_exe):
        # netsh exige name=/program= como argumentos unicos; no PowerShell
        # cada argumento e citado separadamente.
        args = " ".join(ps_quote(parte) for parte in comando[1:])
        linhas.append(f"& netsh {args} | Out-Null")

    # Nome aleatorio evita colisao entre instancias e reduz a superficie de
    # troca do arquivo entre a criacao e a elevacao via UAC.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8-sig", suffix=".ps1",
        prefix="fds_automation_firewall_", delete=False,
    ) as f:
        ps1 = f.name
        f.write("\n".join(linhas) + "\n")

    ps1_arg = ps_quote(ps1)
    comando_elevacao = (
        "Start-Process -FilePath 'powershell.exe' "
        "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"
        f"{ps1_arg} -Verb RunAs -WindowStyle Hidden -Wait"
    )
    try:
        resultado = subprocess.run(
            ["powershell", "-NoProfile", "-Command", comando_elevacao],
            capture_output=True, text=True, timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
        return resultado.returncode == 0
    except Exception:
        logger.debug("Elevacao para cadastrar firewall falhou/cancelada.")
        return False
    finally:
        try:
            os.remove(ps1)
        except OSError:
            pass


def garantir_firewall_pasta(cfg: dict, pasta_projeto: str) -> None:
    """
    Chamado automaticamente antes de cada simulacao (best-effort, nunca
    interrompe a fila). Se o programa estiver rodando como administrador,
    cadastra as regras em silencio; caso contrario, apenas avisa uma vez
    como parar as perguntas do Windows.
    """
    global _AVISO_DADO
    fw_cfg = cfg.get("firewall", {}) or {}
    if not bool(fw_cfg.get("auto_liberar", True)):
        return

    try:
        exes = listar_exes_fds(str(cfg.get("diretorio_fds", "")), [pasta_projeto])
        desbloquear_motw(exes)
        faltantes = regras_faltantes(exes)
        if not faltantes:
            return

        if eh_admin():
            n = adicionar_regras(faltantes)
            if n:
                logger.info(
                    f"Firewall do Windows: {n} regra(s) cadastrada(s) para o "
                    "FDS/MPI (o Windows nao vai mais perguntar).",
                    extra={"console_visible": True},
                )
        elif not _AVISO_DADO:
            _AVISO_DADO = True
            logger.warning(
                "O Windows pode perguntar se o FDS/MPI pode se comunicar. "
                "Para parar essas perguntas de vez, use a opcao "
                "'Parar as perguntas do Windows' no menu (pede permissao de "
                "administrador uma unica vez).",
            )
    except Exception:
        logger.debug("Verificacao de firewall falhou (ignorada).", exc_info=True)


def liberar_firewall_interativo(diretorio_fds: str, pastas_projetos: list[str]) -> tuple[bool, str]:
    """
    Fluxo completo usado pelo menu: lista executaveis, desbloqueia MOTW,
    cadastra as regras que faltam (elevando via UAC se preciso).
    Retorna (sucesso, mensagem_resumo).
    """
    exes = listar_exes_fds(diretorio_fds, pastas_projetos)
    if not exes:
        return False, "Nenhum executavel do FDS encontrado. Verifique a instalacao (opcao do menu)."

    desbloquear_motw(exes)
    faltantes = regras_faltantes(exes)
    if not faltantes:
        return True, f"Tudo certo! As {len(exes)} liberacoes ja estavam cadastradas."

    if eh_admin():
        n = adicionar_regras(faltantes)
        restantes = regras_faltantes(faltantes)
    else:
        adicionar_regras_com_elevacao(faltantes)
        _REGRAS_OK_SESSAO.clear()  # forca re-checagem real apos a elevacao
        restantes = regras_faltantes(faltantes)
        n = len(faltantes) - len(restantes)

    if restantes:
        return False, (
            f"{n} liberacao(oes) cadastrada(s), mas {len(restantes)} ficou(aram) "
            "pendente(s). Voce clicou em 'Sim' na janela do Windows? "
            "Tente de novo ou rode o programa como administrador."
        )
    return True, (
        f"Pronto! {n} liberacao(oes) cadastrada(s) no Firewall do Windows. "
        "O Windows nao vai mais perguntar sobre o FDS, MPI ou HYDRA."
    )


def status_firewall(diretorio_fds: str, pastas_projetos: list[str] | None = None) -> str:
    """
    Resumo rapido para o cabecalho do menu: 'liberado', 'pendente' ou
    'desconhecido' (erro ao consultar).
    """
    try:
        exes = listar_exes_fds(diretorio_fds, pastas_projetos or [])
        if not exes:
            return "desconhecido"
        return "pendente" if regras_faltantes(exes) else "liberado"
    except Exception:
        return "desconhecido"
