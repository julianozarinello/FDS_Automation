# FDS Automation v1.18
import os
import subprocess
import time
import logging
import argparse
import sys
import re
import shutil
import tempfile
import threading
from collections import deque
from copy import deepcopy

import datetime

import yaml    # pip install pyyaml
import psutil  # pip install psutil

from fds_reader import (
    preparar_ambiente_fds,
    contar_meshes,
    motivos_contagem_mesh_indeterminada,
)
from hw_detector  import detectar_hardware, recomendar_config, logar_hardware
from mesh_splitter import ler_meshes, processar_meshes
from optimizer    import aplicar_otimizador_desempenho, otimizador_ativo
from process_guard import (
    proteger_processo,
    matar_job,
    fechar_job,
    matar_arvore_processos,
    aplicar_prioridade_e_afinidade_com_espera,
)
from firewall_config import garantir_firewall_pasta
from monitor_xlsx import MonitorPlanilha
from case_control import (
    PularCasoSolicitado,
    configurar_caso_atual,
    consumir_pulo_se_for_caso_atual,
    verificar_pulo,
)
from fds_diagnostics import (
    iniciar_sessao as iniciar_sessao_diagnosticos,
    registrar_erro_automacao,
    registrar_erro_fds,
    registrar_interrupcao,
    registrar_resumo as registrar_resumo_diagnosticos,
)

VERSAO = "1.18"

RESULTADO_FALHA = -1.0
RESULTADO_PULADO = -2.0

# Impede que processos-filho de console (fds.exe, mpiexec, taskkill)
# abram uma janela de cmd na frente da interface grafica. A saida deles
# ja e capturada por pipe, entao a janela nao faz falta. Inofensivo no
# modo terminal (menu.py): la os filhos usam o console ja existente.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_RE_FDS_ERROR = re.compile(r"^\s*ERROR(?:\(\d+\))?:", re.IGNORECASE)
FDS_FATAL_PHRASES = (
    "FDS WAS IMPROPERLY SET-UP",
    "FDS STOPPED",
)

_RE_FDS_TIME_STEP = re.compile(
    r"Time Step:\s*(\d+),\s*Simulation Time:\s*([0-9.Ee+\-]+)\s*s",
    re.IGNORECASE,
)

_RE_T_END = re.compile(
    r"&TIME[^/]*?\bT_END\s*=\s*([0-9.Ee+\-]+)", re.IGNORECASE | re.DOTALL
)
_RE_T_BEGIN = re.compile(
    r"&TIME[^/]*?\bT_BEGIN\s*=\s*([0-9.Ee+\-]+)", re.IGNORECASE | re.DOTALL
)


class ConsoleInterfaceFilter(logging.Filter):
    def __init__(self, console_amigavel: bool) -> None:
        super().__init__()
        self.console_amigavel = console_amigavel

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "console_silent", False):
            return False

        if getattr(record, "console_visible", False):
            return True

        if not self.console_amigavel:
            return True

        if record.levelno >= logging.WARNING:
            return True

        return record.name in {"__main__", "main"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def obter_cfg_interface(cfg: dict | None) -> dict:
    if not isinstance(cfg, dict):
        return {}
    return cfg.get("interface", {}) or {}


def configurar_logging(pasta_log: str, cfg: dict | None = None) -> None:
    os.makedirs(pasta_log, exist_ok=True)
    log_path = os.path.join(pasta_log, "fds_run.log")

    interface_cfg = obter_cfg_interface(cfg)
    console_amigavel = bool(interface_cfg.get("console_amigavel", True))

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    console_handler = logging.StreamHandler()
    console_handler.addFilter(ConsoleInterfaceFilter(console_amigavel))

    # force=True: sem isso, uma chamada anterior a logging.basicConfig
    # (ex.: menu "Detectar hardware") deixaria o root logger ja configurado
    # e esta chamada viraria no-op — o fds_run.log nunca seria criado e uma
    # troca de pasta_simulacoes manteria o log preso na pasta antiga.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[file_handler, console_handler],
        force=True,
    )


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
def carregar_config(caminho_config: str) -> dict:
    if not os.path.exists(caminho_config):
        raise FileNotFoundError(
            f"Arquivo de configuração não encontrado: {caminho_config}\n"
            "Crie um config.yaml baseado no exemplo do repositório."
        )
    with open(caminho_config, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def _arquivos_fds_da_pasta(pasta: str) -> list[str]:
    """Lista (ordenada) todos os .fds da pasta, recursivamente."""
    arquivos = []
    for raiz, dirs, nomes in os.walk(pasta):
        dirs.sort()
        for nome in sorted(nomes):
            if nome.lower().endswith(".fds"):
                arquivos.append(os.path.join(raiz, nome))

    arquivos.sort(key=lambda p: os.path.relpath(p, pasta).lower())
    return arquivos


def descobrir_simulacoes(cfg: dict) -> list[str]:
    """
    Retorna uma lista ordenada de caminhos .fds.
    Prioridade: pasta_simulacoes > simulacoes > arquivo_fds
    """
    logger = logging.getLogger(__name__)

    if "pasta_simulacoes" in cfg and cfg["pasta_simulacoes"]:
        pasta = str(cfg["pasta_simulacoes"])
        if not os.path.isdir(pasta):
            raise FileNotFoundError(f"Pasta não encontrada: {pasta}")

        arquivos = _arquivos_fds_da_pasta(pasta)

        if not arquivos:
            raise FileNotFoundError(f"Nenhum arquivo .fds encontrado em: {pasta}")

        logger.info(f"{len(arquivos)} arquivo(s) .fds encontrado(s) em '{pasta}':")
        for arq in arquivos:
            logger.info(f"  - {os.path.relpath(arq, pasta)}")
        return arquivos

    if "simulacoes" in cfg and cfg["simulacoes"]:
        return [str(p) for p in cfg["simulacoes"]]

    if "arquivo_fds" in cfg and cfg["arquivo_fds"]:
        return [str(cfg["arquivo_fds"])]

    raise ValueError(
        "Nenhuma fonte de arquivos FDS definida.\n"
        "Use 'pasta_simulacoes', 'simulacoes' ou 'arquivo_fds' no config.yaml."
    )


# ---------------------------------------------------------------------------
# Auto-configuração por hardware
# ---------------------------------------------------------------------------
def aplicar_autoconfig(cfg: dict, arquivo_fds: str) -> dict:
    """
    Se 'auto_config: true' no config.yaml, detecta o hardware e sobrescreve
    modo/num_cores/hybrid com a configuração recomendada para o arquivo .fds.
    Retorna o cfg (possivelmente modificado).
    """
    if not cfg.get("auto_config", False):
        return cfg

    hw       = detectar_hardware()
    if hw.get("is_hybrid") and bool(cfg.get("hybrid_cpu", {}).get("usar_e_cores", False)):
        hw["cores_efetivos"] = hw["cores_fisicos"]
    n_meshes = contar_meshes(arquivo_fds)
    motivos = motivos_contagem_mesh_indeterminada(arquivo_fds)
    if motivos:
        logging.getLogger(__name__).warning(
            "Contagem completa de meshes depende de %s. A auto-configuracao "
            "usara o modo OpenMP conservador; preserve uma configuracao MPI "
            "manual se o caso concatenado/expandido ja foi validado.",
            "/".join(motivos),
            extra={"console_visible": True},
        )
        n_meshes = 1
    rec      = recomendar_config(hw, n_meshes)
    logar_hardware(hw, rec)

    cfg["modo"]      = rec["modo"]
    cfg["num_cores"] = rec["num_cores"]
    cfg["hybrid"]    = rec["hybrid"]
    return cfg


# ---------------------------------------------------------------------------
# Mesh Splitter — divisão e reordenação de meshes
# ---------------------------------------------------------------------------
def aplicar_mesh_splitter(cfg: dict, arquivo_fds: str) -> None:
    """
    Se 'mesh_splitter.ativo: true' no config.yaml, divide e reordena as
    meshes do arquivo .fds antes de rodar o FDS.
    Cria backup automático (.fds.bak) antes de qualquer modificação.
    """
    ms_cfg = cfg.get("mesh_splitter", {})
    if not ms_cfg.get("ativo", False):
        return

    modo = cfg["modo"].upper()
    if modo not in ("MPI", "HYBRID"):
        logger = logging.getLogger(__name__)
        logger.info("Mesh Splitter ignorado (modo não é MPI/HYBRID).")
        return

    motivos = motivos_contagem_mesh_indeterminada(arquivo_fds)
    if motivos:
        logging.getLogger(__name__).warning(
            "Mesh Splitter ignorado: %s altera a quantidade efetiva de "
            "meshes somente dentro do FDS. O arquivo original sera usado.",
            "/".join(motivos),
            extra={"console_visible": True},
        )
        return

    n_cores    = (
        cfg["hybrid"]["mpi_procs"] if modo == "HYBRID" else cfg["num_cores"]
    )
    margem_pct = float(ms_cfg.get("margem_pct", 10.0))
    dry_run    = bool(ms_cfg.get("dry_run",    False))
    pular_se_otimizado = bool(ms_cfg.get("pular_se_otimizado", True))
    peso_espacial = float(ms_cfg.get("peso_espacial", 0.15))
    max_meshes_por_core = max(1, int(ms_cfg.get("max_meshes_por_core", 4)))
    permitir_divisao = bool(ms_cfg.get("permitir_divisao", False))

    processar_meshes(
        arquivo_fds,
        n_cores,
        margem_pct,
        dry_run,
        pular_se_otimizado,
        peso_espacial,
        max_meshes_por_core,
        permitir_divisao,
    )


def ajustar_processos_mpi_ao_caso(cfg: dict, arquivo_fds: str) -> dict:
    """Evita ERROR(122) quando ha mais ranks do que meshes conhecidas."""
    modo = str(cfg.get("modo", "OPENMP")).upper()
    if modo not in {"MPI", "HYBRID"}:
        return cfg

    motivos = motivos_contagem_mesh_indeterminada(arquivo_fds)
    if motivos:
        # MULT_ID e CATF sao expandidos pelo proprio FDS. A contagem literal
        # pode ser muito menor e nao autoriza reduzir a configuracao do autor.
        return cfg

    n_meshes = contar_meshes(arquivo_fds)
    if n_meshes <= 0:
        return cfg
    n_procs = (
        int((cfg.get("hybrid") or {}).get("mpi_procs", 1))
        if modo == "HYBRID"
        else int(cfg.get("num_cores", 1))
    )
    if n_procs <= n_meshes:
        return cfg

    novo = deepcopy(cfg)
    if modo == "HYBRID":
        novo.setdefault("hybrid", {})["mpi_procs"] = n_meshes
        omp_threads = max(1, int(novo["hybrid"].get("omp_threads", 1)))
        novo["num_cores"] = n_meshes * omp_threads
    else:
        novo["num_cores"] = n_meshes

    logging.getLogger(__name__).warning(
        "Configuracao ajustada para este caso: %d processo(s) MPI -> %d, "
        "pois o FDS nao aceita mais processos do que meshes.",
        n_procs,
        n_meshes,
        extra={"console_visible": True},
    )
    return novo


# ---------------------------------------------------------------------------
# Variáveis de ambiente para performance
# ---------------------------------------------------------------------------
def aplicar_variaveis_performance(cfg: dict, modo: str) -> None:
    perf = cfg.get("performance", {})

    if modo == "OPENMP":
        os.environ["OMP_NUM_THREADS"] = str(cfg["num_cores"])
    elif modo == "HYBRID":
        os.environ["OMP_NUM_THREADS"] = str(cfg["hybrid"]["omp_threads"])
    else:
        os.environ["OMP_NUM_THREADS"] = "1"

    if perf.get("kmp_affinity", True):
        os.environ["KMP_AFFINITY"]  = "granularity=fine,compact,1,0"
        os.environ["OMP_PROC_BIND"] = "close"

    if perf.get("omp_spin", True):
        os.environ["GOMP_SPINCOUNT"]  = "10000000"
        os.environ["OMP_WAIT_POLICY"] = "active"
        os.environ["KMP_BLOCKTIME"]   = "200"

    if perf.get("numa_opt", True):
        os.environ["OMP_PLACES"] = "cores"

    # Pilha defensiva por thread OpenMP; pode ser ajustada no config caso um
    # modelo ou runtime exija outro valor.
    os.environ["OMP_STACKSIZE"] = str(perf.get("omp_stacksize", "16M"))

    # O solver UGLMAT do FDS pode usar MKL com threads. MPI puro fica em 1
    # para evitar n_ranks x n_threads; nos modos com threads, a MKL recebe o
    # mesmo orcamento do rank. Um inteiro >0 em performance.mkl_threads
    # permite sobrescrever a escolha automatica.
    try:
        mkl_cfg = int(perf.get("mkl_threads", 0) or 0)
    except (TypeError, ValueError):
        mkl_cfg = 0
    if mkl_cfg > 0:
        mkl_threads = mkl_cfg
    elif modo == "MPI":
        mkl_threads = 1
    elif modo == "HYBRID":
        mkl_threads = max(1, int(cfg["hybrid"]["omp_threads"]))
    else:
        mkl_threads = max(1, int(cfg.get("num_cores", 1)))
    os.environ["MKL_NUM_THREADS"] = str(mkl_threads)
    os.environ["MKL_DYNAMIC"]     = "FALSE"
    os.environ["OMP_DYNAMIC"]     = "FALSE"
    os.environ["OMP_DISPLAY_ENV"] = "FALSE"

    # Intel MPI pin (modo MPI/HYBRID)
    if modo in ("MPI", "HYBRID"):
        os.environ["I_MPI_PIN"]       = "1"
        os.environ["I_MPI_PIN_ORDER"] = "compact"
        # Maquina unica: comunicacao 100% em memoria compartilhada, sem
        # sondar interfaces de rede (evita overhead/latencia do provedor
        # OFI/TCP do Intel MPI). Desative com performance.mpi_shm: false
        # se um dia rodar em cluster com mais de um no.
        if perf.get("mpi_shm", True):
            os.environ["I_MPI_FABRICS"] = "shm"


# ---------------------------------------------------------------------------
# Prioridade de processo
# ---------------------------------------------------------------------------
def elevar_prioridade() -> None:
    logger = logging.getLogger(__name__)
    try:
        proc = psutil.Process(os.getpid())
        proc.nice(psutil.HIGH_PRIORITY_CLASS)
        logger.info("Prioridade de processo elevada para HIGH.")
    except (AttributeError, psutil.AccessDenied):
        try:
            psutil.Process(os.getpid()).nice(-10)
            logger.info("Prioridade de processo elevada (nice -10).")
        except psutil.AccessDenied:
            logger.warning(
                "Não foi possível elevar a prioridade — execute como Administrador."
            )


# ---------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------
def validar_atribuicoes_mpi(arquivo_fds: str, n_procs: int) -> list[int]:
    """Reproduz as regras de atribuicao que o leitor atual do FDS exige."""
    if n_procs <= 0:
        raise ValueError("O numero de processos MPI deve ser maior que zero.")

    meshes = ler_meshes(arquivo_fds)
    atribuicoes: list[int] = []
    for indice, mesh in enumerate(meshes, start=1):
        valor = mesh.mpi_process
        if valor is not None and valor >= 0:
            if valor >= n_procs:
                if n_procs == 1:
                    processo = 0  # o FDS tambem reduz para zero e avisa
                else:
                    raise ValueError(
                        f"MPI_PROCESS={valor} na mesh {indice} excede o ultimo "
                        f"processo valido ({n_procs - 1})."
                    )
            else:
                processo = valor
        else:
            if n_procs > 1 and indice > n_procs:
                raise ValueError(
                    f"A mesh {indice} precisa de MPI_PROCESS porque ha mais "
                    "meshes do que processos MPI."
                )
            processo = min(indice - 1, n_procs - 1)

        if indice == 1 and processo != 0:
            raise ValueError("A primeira mesh deve usar MPI_PROCESS=0.")
        if atribuicoes and (
            processo < atribuicoes[-1] or processo - atribuicoes[-1] > 1
        ):
            raise ValueError(
                "MPI_PROCESS deve ser continuo e monotonicamente crescente."
            )
        atribuicoes.append(processo)

    if atribuicoes and n_procs > 1 and atribuicoes[-1] != n_procs - 1:
        raise ValueError(
            f"MPI_PROCESS nao atribui nenhuma mesh ao processo {n_procs - 1}. "
            "Reduza os processos ou rebalanceie as meshes."
        )
    return atribuicoes


def validar_simulacao(arquivo_fds: str, cfg: dict) -> None:
    logger = logging.getLogger(__name__)

    if not os.path.exists(arquivo_fds):
        raise FileNotFoundError(f"Arquivo FDS não encontrado: {arquivo_fds}")

    if not os.path.exists(cfg["diretorio_fds"]):
        raise FileNotFoundError(
            f"Diretório do FDS não encontrado: {cfg['diretorio_fds']}\n"
            "Verifique o campo 'diretorio_fds' no config.yaml."
        )

    modo = cfg["modo"].upper()
    if modo not in {"SERIAL", "OPENMP", "MPI", "HYBRID"}:
        raise ValueError(f"Modo inválido: '{modo}'.")

    if modo in ("MPI", "HYBRID"):
        motivos = motivos_contagem_mesh_indeterminada(arquivo_fds)
        if motivos:
            logger.warning(
                "Validacao estrita da quantidade de meshes ignorada por %s; "
                "o FDS fara a expansao/concatenacao do caso.",
                "/".join(motivos),
            )
            return
        n_meshes = contar_meshes(arquivo_fds)
        n_procs  = cfg["hybrid"]["mpi_procs"] if modo == "HYBRID" else cfg["num_cores"]
        if n_meshes > 0 and n_procs > n_meshes:
            raise ValueError(
                f"Configuracao MPI invalida: {n_procs} processos para "
                f"{n_meshes} mesh(es). O FDS exige processos <= meshes."
            )
        if n_meshes > 0:
            atribuicoes = validar_atribuicoes_mpi(arquivo_fds, int(n_procs))
            logger.info(
                f"Validacao MPI OK: {n_meshes} mesh(es) em {n_procs} "
                f"processo(s) ({atribuicoes[0]}..{atribuicoes[-1]})."
            )


# ---------------------------------------------------------------------------
# Construção do comando
# ---------------------------------------------------------------------------
def construir_comando(cfg: dict, pasta_projeto: str, nome_fds: str) -> tuple[list, str]:
    modo       = cfg["modo"].upper()
    fds_openmp = os.path.join(pasta_projeto, "fds_openmp.exe")
    fds_exe    = os.path.join(pasta_projeto, "fds.exe")
    mpiexec    = os.path.join(pasta_projeto, "mpiexec.exe")

    if modo == "OPENMP":
        status = f"OPENMP | {cfg['num_cores']} thread(s)"
        return [fds_openmp, nome_fds], status

    elif modo == "MPI":
        n = cfg["num_cores"]
        status = f"MPI | {n} processo(s)"
        return (
            [mpiexec, "-n", str(n),
             "-genv", "I_MPI_PIN", "1",
             "-genv", "I_MPI_PIN_ORDER", "compact",
             "-bind-to", "core", fds_exe, nome_fds],
            status,
        )

    elif modo == "HYBRID":
        mpi_procs   = cfg["hybrid"]["mpi_procs"]
        omp_threads = cfg["hybrid"]["omp_threads"]
        status = (
            f"HYBRID | {mpi_procs} processo(s) MPI x {omp_threads} thread(s) OpenMP "
            f"= {mpi_procs * omp_threads} núcleo(s)"
        )
        return (
            [mpiexec, "-n", str(mpi_procs),
             "-genv", "I_MPI_PIN", "1",
             "-genv", "I_MPI_PIN_DOMAIN", f"omp:{omp_threads}",
             "-genv", "I_MPI_PIN_ORDER", "compact",
             fds_openmp, nome_fds],
            status,
        )

    else:  # SERIAL
        return [fds_exe, nome_fds], "SERIAL"


def construir_comando_binarios_instalados(cfg: dict, diretorio_fds: str, nome_fds: str) -> tuple[list, str]:
    modo = cfg["modo"].upper()
    fds_openmp = os.path.join(diretorio_fds, "fds_openmp.exe")
    fds_exe = os.path.join(diretorio_fds, "fds.exe")
    mpiexec = os.path.join(diretorio_fds, "mpi", "mpiexec.exe")
    if not os.path.exists(mpiexec):
        mpiexec = os.path.join(diretorio_fds, "mpiexec.exe")

    if modo == "OPENMP":
        status = f"OPENMP | {cfg['num_cores']} thread(s)"
        return [fds_openmp, nome_fds], status

    if modo == "MPI":
        n = cfg["num_cores"]
        status = f"MPI | {n} processo(s)"
        return (
            [mpiexec, "-n", str(n),
             "-genv", "I_MPI_PIN", "1",
             "-genv", "I_MPI_PIN_ORDER", "compact",
             "-bind-to", "core", fds_exe, nome_fds],
            status,
        )

    if modo == "HYBRID":
        mpi_procs = cfg["hybrid"]["mpi_procs"]
        omp_threads = cfg["hybrid"]["omp_threads"]
        status = (
            f"HYBRID | {mpi_procs} processo(s) MPI x {omp_threads} thread(s) OpenMP "
            f"= {mpi_procs * omp_threads} nucleo(s)"
        )
        return (
            [mpiexec, "-n", str(mpi_procs),
             "-genv", "I_MPI_PIN", "1",
             "-genv", "I_MPI_PIN_DOMAIN", f"omp:{omp_threads}",
             "-genv", "I_MPI_PIN_ORDER", "compact",
             fds_openmp, nome_fds],
            status,
        )

    return [fds_exe, nome_fds], "SERIAL"


def obter_cfg_benchmark(cfg: dict | None) -> dict:
    if not isinstance(cfg, dict):
        return {}
    return cfg.get("auto_benchmark", {}) or {}


def _cfg_candidato(cfg: dict, modo: str, num_cores: int, mpi_procs: int, omp_threads: int) -> dict:
    cand = deepcopy(cfg)
    cand["modo"] = modo
    cand["num_cores"] = num_cores
    cand["hybrid"] = {"mpi_procs": mpi_procs, "omp_threads": omp_threads}
    return cand


def gerar_candidatos_benchmark(cfg: dict, hw: dict, n_meshes: int) -> list[dict]:
    bench_cfg = obter_cfg_benchmark(cfg)
    modos = [str(m).upper() for m in bench_cfg.get("modos", ["OPENMP", "MPI", "HYBRID"])]
    cores = max(1, int(hw.get("cores_efetivos", hw.get("cores_fisicos", 1))))
    candidatos = []

    if "OPENMP" in modos:
        candidatos.append(_cfg_candidato(cfg, "OPENMP", cores, 1, cores))

    if "MPI" in modos and n_meshes > 1:
        mpi = min(n_meshes, cores)
        candidatos.append(_cfg_candidato(cfg, "MPI", mpi, mpi, 1))

    if "HYBRID" in modos and n_meshes > 1 and cores >= 2:
        mpi = min(n_meshes, max(1, cores // 2))
        omp = max(1, cores // mpi)
        candidatos.append(_cfg_candidato(cfg, "HYBRID", mpi * omp, mpi, omp))

    candidatos_unicos = []
    vistos = set()
    for cand in candidatos:
        chave = (
            cand["modo"],
            cand.get("num_cores"),
            cand.get("hybrid", {}).get("mpi_procs"),
            cand.get("hybrid", {}).get("omp_threads"),
        )
        if chave in vistos:
            continue
        vistos.add(chave)
        candidatos_unicos.append(cand)

    return candidatos_unicos


def _encerrar_processo_arvore(processo: subprocess.Popen) -> None:
    if processo.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(processo.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return

    processo.terminate()
    try:
        processo.wait(timeout=10)
    except subprocess.TimeoutExpired:
        processo.kill()


def _score_saida_benchmark(saida: str, elapsed: float) -> tuple[float, str]:
    if any(linha_indica_erro_fds(linha) for linha in saida.splitlines()):
        return 0.0, "erro FDS"

    ultimo = None
    for linha in saida.splitlines():
        progresso = extrair_time_step_fds(linha)
        if progresso:
            ultimo = progresso

    if not ultimo or elapsed <= 0:
        return 0.0, "sem progresso medido"

    step, tempo_simulado = ultimo
    try:
        sim_s = float(tempo_simulado)
    except ValueError:
        return 0.0, "tempo simulado invalido"

    return sim_s / elapsed, f"passo {step}, {tempo_simulado}s simulados"


def executar_benchmark_candidato(cfg: dict, arquivo_fds: str, timeout_seg: float) -> tuple[float, str]:
    nome_fds = os.path.basename(arquivo_fds)
    inicio = time.time()

    with tempfile.TemporaryDirectory(prefix="fds_bench_") as tmp_dir:
        tmp_fds = os.path.join(tmp_dir, nome_fds)
        shutil.copy2(arquivo_fds, tmp_fds)

        modo = cfg["modo"].upper()
        if modo in ("MPI", "HYBRID") and cfg.get("mesh_splitter", {}).get("ativo", False):
            ms_cfg = cfg.get("mesh_splitter", {})
            n_cores = cfg["hybrid"]["mpi_procs"] if modo == "HYBRID" else cfg["num_cores"]
            processar_meshes(
                tmp_fds,
                n_cores,
                float(ms_cfg.get("margem_pct", 10.0)),
                False,
                False,
                float(ms_cfg.get("peso_espacial", 0.15)),
                max(1, int(ms_cfg.get("max_meshes_por_core", 4))),
                bool(ms_cfg.get("permitir_divisao", False)),
            )

        aplicar_variaveis_performance(cfg, modo)
        comando, _ = construir_comando_binarios_instalados(cfg, cfg["diretorio_fds"], nome_fds)
        env = os.environ.copy()
        mpi_dir = os.path.join(cfg["diretorio_fds"], "mpi")
        env["PATH"] = os.pathsep.join([cfg["diretorio_fds"], mpi_dir, env.get("PATH", "")])

        processo = subprocess.Popen(
            comando,
            cwd=tmp_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )

        try:
            saida, _ = processo.communicate(timeout=timeout_seg)
        except subprocess.TimeoutExpired:
            _encerrar_processo_arvore(processo)
            saida, _ = processo.communicate(timeout=10)

    elapsed = time.time() - inicio
    return _score_saida_benchmark(saida or "", elapsed)


def aplicar_auto_benchmark(cfg: dict, arquivo_fds: str) -> dict:
    """Compatibilidade: executa o benchmark antigo pelo otimizador robusto.

    O primeiro auto-benchmark tinha um executor separado. Em particular, o
    Intel MPI pode encerrar ``mpiexec`` antes dos ranks, o que deixava filhos
    orfaos quando um teste expirava. O otimizador atual usa Job Object,
    acompanha a arvore inteira e mede avanco entre amostras; manter um unico
    caminho evita que as duas opcoes tenham seguranca e scores diferentes.
    """
    bench_cfg = obter_cfg_benchmark(cfg)
    if not bench_cfg.get("ativo", False):
        return cfg
    if not os.path.exists(cfg.get("diretorio_fds", "")):
        logging.getLogger(__name__).warning(
            "Auto-benchmark ignorado: diretorio_fds nao encontrado."
        )
        return cfg

    modos = [
        str(modo).upper()
        for modo in bench_cfg.get("modos", ["OPENMP", "MPI", "HYBRID"])
    ]
    cfg_teste = deepcopy(cfg)
    cfg_teste["otimizador"] = {
        "ativo": True,
        "tempo_teste_seg": max(5.0, float(bench_cfg.get("tempo_max_seg", 45))),
        "max_tentativas": max(2, len(modos) + 1),
        "melhoria_min_pct": 0.0,
        "usar_cache": False,
        "testar_modos": modos,
        "testar_peso_espacial": [
            float((cfg.get("mesh_splitter") or {}).get("peso_espacial", 0.15))
        ],
        "testar_threads_openmp": False,
        "copiar_arquivos_auxiliares": True,
        "copiar_arquivos_ate_mb": 250,
    }
    logging.getLogger(__name__).info(
        "Auto-benchmark iniciado pelo executor seguro do otimizador."
    )
    resultado = deepcopy(aplicar_otimizador_desempenho(cfg_teste, arquivo_fds))
    # Esta opcao continua sendo uma decisao de uma unica execucao. Nao deixe
    # o otimizador habilitado no cfg retornado, pois executar_simulacao ja
    # passou pelo ponto que escolhe entre os dois modos.
    resultado["otimizador"] = deepcopy(cfg.get("otimizador", {}))
    return resultado


def linha_indica_erro_fds(linha: str) -> bool:
    linha_upper = linha.upper()
    # "Maximum Velocity Error:" e outras metricas normais do FDS contem
    # a palavra ERROR, mas nao representam falha. Erros fatais do parser
    # comecam por ERROR(n):/ERROR:, ou usam uma das frases abaixo.
    return bool(_RE_FDS_ERROR.match(linha)) or any(
        frase in linha_upper for frase in FDS_FATAL_PHRASES
    )


def atualizar_titulo_console(titulo: str) -> None:
    if os.name != "nt":
        return

    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(titulo)
    except Exception:
        pass


def limpar_console_se_ativo(cfg: dict | None) -> None:
    interface_cfg = obter_cfg_interface(cfg)
    if not bool(interface_cfg.get("limpar_console", True)):
        return

    # Em exe empacotado SEM console (GUI/worker), sys.stdout pode ser None.
    if sys.stdout is None or not sys.stdout.isatty():
        return

    os.system("cls" if os.name == "nt" else "clear")


def mostrar_saida_fds_console(cfg: dict | None) -> bool:
    interface_cfg = obter_cfg_interface(cfg)
    return bool(interface_cfg.get("mostrar_saida_fds_console", False))


def intervalo_status_console(cfg: dict | None) -> float:
    interface_cfg = obter_cfg_interface(cfg)
    return max(1.0, float(interface_cfg.get("intervalo_status_seg", 30)))


def formatar_duracao(segundos: float) -> str:
    segundos = max(0, int(segundos))
    h = segundos // 3600
    m = (segundos % 3600) // 60
    s = segundos % 60
    return f"{h:02d}h {m:02d}m {s:02d}s"


def extrair_time_step_fds(linha: str) -> tuple[str, str] | None:
    match = _RE_FDS_TIME_STEP.search(linha)
    if not match:
        return None
    return match.group(1), match.group(2)


def ler_intervalo_tempo_fds(caminho_fds: str) -> tuple[float, float] | None:
    """
    Lê T_BEGIN e T_END da linha &TIME do arquivo .fds.
    Retorna (t_begin, t_end) ou None se não encontrar/for inválido.
    Usado para estimar o tempo restante (ETA) durante a simulação.
    """
    try:
        with open(caminho_fds, "r", encoding="latin-1") as f:
            conteudo = f.read()
        m_end = _RE_T_END.search(conteudo)
        if not m_end:
            return None
        t_end = float(m_end.group(1))
        m_begin = _RE_T_BEGIN.search(conteudo)
        t_begin = float(m_begin.group(1)) if m_begin else 0.0
        if t_end <= t_begin:
            return None
        return t_begin, t_end
    except Exception:
        return None


def estimar_restante(
    intervalo: tuple[float, float] | None,
    tempo_simulado: float,
    decorrido: float,
) -> float | None:
    """
    Estima os segundos de relogio restantes por regra de tres simples:
    o FDS avancou `tempo_simulado` em `decorrido` segundos reais; falta
    simular (t_end - tempo_simulado). Retorna None se nao ha dados
    suficientes (inicio da simulacao ou T_END desconhecido).
    """
    if intervalo is None or decorrido <= 0:
        return None
    t_begin, t_end = intervalo
    avancado = tempo_simulado - t_begin
    if avancado <= 0:
        return None
    restante_sim = max(0.0, t_end - tempo_simulado)
    return decorrido * restante_sim / avancado


def logar_status_fila(arquivo_fds: str, idx: int, total: int, cfg: dict | None = None) -> None:
    logger = logging.getLogger(__name__)
    nome_fds = os.path.basename(arquivo_fds)
    concluidas = idx - 1
    faltam = max(total - idx, 0)
    pct = (idx / total * 100.0) if total else 0.0

    limpar_console_se_ativo(cfg)
    atualizar_titulo_console(f"FDS Automation - {idx}/{total} - {nome_fds}")

    extra = {"console_visible": True}
    logger.info("=" * 55, extra=extra)
    logger.info("  STATUS DA FILA", extra=extra)
    logger.info("=" * 55, extra=extra)
    logger.info(f"  Rodando agora : {nome_fds}", extra=extra)
    logger.info(f"  Progresso     : {idx}/{total} ({pct:.1f}%)", extra=extra)
    logger.info(f"  Concluidas    : {concluidas}", extra=extra)
    logger.info(f"  Faltam depois : {faltam}", extra=extra)
    if not mostrar_saida_fds_console(cfg):
        logger.info("  Detalhes FDS  : ocultos no console, salvos no log", extra=extra)
    logger.info("=" * 55, extra=extra)


def logar_status_pos_simulacao(
    arquivo_fds: str,
    idx: int,
    total: int,
    elapsed: float,
    cfg: dict | None = None,
) -> None:
    logger = logging.getLogger(__name__)
    nome_fds = os.path.basename(arquivo_fds)
    faltam = max(total - idx, 0)
    if elapsed == RESULTADO_PULADO:
        status = "interrompida pelo usuario"
    else:
        status = "concluida" if elapsed >= 0 else "falhou"

    limpar_console_se_ativo(cfg)
    atualizar_titulo_console(
        f"FDS Automation - {idx}/{total} {status} - faltam {faltam}"
    )

    extra = {"console_visible": True}
    logger.info("-" * 55, extra=extra)
    logger.info(f"  Status da fila : {nome_fds} {status}", extra=extra)
    logger.info(f"  Simulacoes restantes: {faltam}", extra=extra)
    logger.info("-" * 55, extra=extra)


# ---------------------------------------------------------------------------
# Execução de uma simulação
# ---------------------------------------------------------------------------
def executar_simulacao(
    arquivo_fds: str,
    cfg: dict,
    idx: int,
    total: int,
    monitor: MonitorPlanilha | None = None,
) -> float:
    logger   = logging.getLogger(__name__)
    pasta    = os.path.dirname(arquivo_fds)
    nome_fds = os.path.basename(arquivo_fds)
    if monitor is None:
        monitor = MonitorPlanilha({})  # inativo (sem config)

    # O identificador acompanha todas as copias de configuracao criadas pelo
    # auto-config/otimizador. Um pedido atrasado para o caso anterior nunca
    # corresponde ao indice/nome deste caso.
    configurar_caso_atual(cfg, arquivo_fds, idx, total)

    logar_status_fila(arquivo_fds, idx, total, cfg)

    logger.info("=" * 55)
    logger.info(f"  SIMULAÇÃO {idx}/{total}: {nome_fds}")
    logger.info("=" * 55)
    verificar_pulo(cfg)

    # 1. Auto-configuração por hardware
    cfg = aplicar_autoconfig(cfg, arquivo_fds)
    configurar_caso_atual(cfg, arquivo_fds, idx, total)
    verificar_pulo(cfg)
    if otimizador_ativo(cfg):
        cfg = aplicar_otimizador_desempenho(cfg, arquivo_fds)
    else:
        cfg = aplicar_auto_benchmark(cfg, arquivo_fds)
    configurar_caso_atual(cfg, arquivo_fds, idx, total)
    verificar_pulo(cfg)
    modo = cfg["modo"].upper()

    # 2. Mesh Splitter (divide + reordena meshes antes de rodar)
    ms_cfg = cfg.get("mesh_splitter", {}) or {}
    divisao_efetiva = bool(
        ms_cfg.get("ativo", False)
        and ms_cfg.get("permitir_divisao", False)
        and not ms_cfg.get("dry_run", False)
        and modo in {"MPI", "HYBRID"}
    )
    if not divisao_efetiva:
        cfg = ajustar_processos_mpi_ao_caso(cfg, arquivo_fds)
    aplicar_mesh_splitter(cfg, arquivo_fds)
    # Uma divisao avancada pode ser recusada por seguranca (transformacoes,
    # restart etc.) ou nao gerar cortes suficientes. Confere novamente com o
    # arquivo efetivamente produzido antes de montar o comando.
    cfg = ajustar_processos_mpi_ao_caso(cfg, arquivo_fds)
    configurar_caso_atual(cfg, arquivo_fds, idx, total)
    verificar_pulo(cfg)

    # 3. Validação, preparação e execução
    validar_simulacao(arquivo_fds, cfg)
    preparar_ambiente_fds(cfg["diretorio_fds"], pasta)
    # Libera FDS/MPI no Firewall do Windows (evita a pergunta "Deseja
    # permitir que este aplicativo se comunique?" a cada pasta nova).
    garantir_firewall_pasta(cfg, pasta)
    aplicar_variaveis_performance(cfg, modo)
    verificar_pulo(cfg)

    comando, status_msg = construir_comando(cfg, pasta, nome_fds)
    logger.info(f"Modo  : {status_msg}")
    logger.info(f"Pasta : {pasta}")
    logger.info("-" * 55)

    inicio = time.time()
    _elapsed_dat = -1.0  # rastreado para tempos_simulacao.dat
    processo = None
    job = None
    cancelamento_caso = threading.Event()
    encerrar_vigia_cancelamento = threading.Event()
    try:
        creationflags = CREATE_NO_WINDOW
        if hasattr(subprocess, 'HIGH_PRIORITY_CLASS'):
            creationflags |= subprocess.HIGH_PRIORITY_CLASS

        processo = subprocess.Popen(
            comando,
            cwd=pasta,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        job = proteger_processo(processo)

        def vigiar_cancelamento_caso() -> None:
            while not encerrar_vigia_cancelamento.wait(0.20):
                if not consumir_pulo_se_for_caso_atual(cfg):
                    continue
                cancelamento_caso.set()
                logger.warning(
                    f"Parada solicitada para {nome_fds}. Encerrando somente "
                    "este caso; a fila seguira para o proximo.",
                    extra={"console_visible": True},
                )
                if job is not None:
                    matar_job(job)
                elif processo is not None:
                    matar_arvore_processos(processo.pid)
                return

        threading.Thread(
            target=vigiar_cancelamento_caso,
            name=f"cancelar-{idx}",
            daemon=True,
        ).start()

        if cfg.get("performance", {}).get("fixar_afinidade_os", True):
            # O Windows nao propaga HIGH_PRIORITY_CLASS nem afinidade para
            # processos-filho: so o mpiexec recebe o que passamos no Popen.
            # Os ranks reais (fds.exe) ficam em prioridade normal e sem
            # afinidade fixada a menos que ajustemos cada um aqui.
            hw_afinidade = detectar_hardware()
            if bool(cfg.get("hybrid_cpu", {}).get("usar_e_cores", False)):
                # usar_e_cores libera tambem os E-cores (mas ainda 1 thread
                # logico por core fisico, evitando HyperThreading).
                indices_p = hw_afinidade.get("logical_indices_todos")
            else:
                indices_p = hw_afinidade.get("logical_indices_efetivos")

            if modo in ("MPI", "HYBRID"):
                n_ranks = cfg["hybrid"]["mpi_procs"] if modo == "HYBRID" else cfg["num_cores"]
            else:
                n_ranks = 1
            def ajustar_afinidade() -> None:
                n_ajustados = aplicar_prioridade_e_afinidade_com_espera(
                    nome_fds, inicio,
                    cores_logicos=indices_p,
                    prioridade=psutil.HIGH_PRIORITY_CLASS,
                    n_esperado=n_ranks,
                    cancelar_evento=cancelamento_caso,
                )
                if n_ajustados:
                    logger.info(
                        f"Prioridade/afinidade de CPU fixada em "
                        f"{n_ajustados} processo(s) FDS"
                        + (f" ({len(indices_p)} core(s) fisico(s))." if indices_p else "."),
                        extra={"console_visible": True},
                    )

            # Nao bloqueia ate 5 s antes de consumir stdout (o pipe poderia
            # encher enquanto os ranks MPI aparecem).
            threading.Thread(target=ajustar_afinidade, daemon=True).start()

        erro_fds = False
        primeira_linha_erro = None
        ultimas_linhas = deque(maxlen=40)
        contexto_erro: list[str] = []
        linhas_apos_erro = 0
        saida_fds_no_console = mostrar_saida_fds_console(cfg)
        intervalo_status = intervalo_status_console(cfg)
        ultimo_status_console = 0.0
        intervalo_tempo = ler_intervalo_tempo_fds(arquivo_fds)  # p/ ETA
        monitor.iniciar_caso(arquivo_fds, intervalo_tempo)

        if not saida_fds_no_console:
            logger.info(
                "FDS iniciado. Aguardando progresso da simulacao...",
                extra={"console_visible": True},
            )

        if processo.stdout is not None:
            for linha in processo.stdout:
                linha = linha.rstrip()
                if not linha:
                    continue
                ultimas_linhas.append(linha)
                logger.info(
                    f"FDS | {linha}",
                    extra={"console_silent": not saida_fds_no_console},
                )
                if linha_indica_erro_fds(linha):
                    erro_fds = True
                    if primeira_linha_erro is None:
                        primeira_linha_erro = linha.strip()
                        contexto_erro = list(ultimas_linhas)[-10:]
                        linhas_apos_erro = 12
                elif linhas_apos_erro > 0:
                    contexto_erro.append(linha)
                    linhas_apos_erro -= 1

                progresso = extrair_time_step_fds(linha)
                agora = time.time()
                if progresso:
                    try:
                        monitor.progresso(
                            arquivo_fds, float(progresso[1]), agora - inicio
                        )
                    except (TypeError, ValueError):
                        pass
                if (
                    progresso
                    and not saida_fds_no_console
                    and agora - ultimo_status_console >= intervalo_status
                ):
                    step, tempo_simulado = progresso
                    decorrido = formatar_duracao(agora - inicio)
                    try:
                        eta_seg = estimar_restante(
                            intervalo_tempo, float(tempo_simulado), agora - inicio
                        )
                    except ValueError:
                        eta_seg = None
                    eta_txt = (
                        f" | restante ~{formatar_duracao(eta_seg)}"
                        if eta_seg is not None else ""
                    )
                    logger.info(
                        f"Rodando: {nome_fds} | passo {step} | "
                        f"tempo simulado {tempo_simulado}s | decorrido {decorrido}"
                        f"{eta_txt}",
                        extra={"console_visible": True},
                    )
                    ultimo_status_console = agora

        returncode = processo.wait()
        elapsed = time.time() - inicio
        h, m, s = int(elapsed//3600), int((elapsed%3600)//60), int(elapsed%60)

        if cancelamento_caso.is_set():
            _elapsed_dat = RESULTADO_PULADO
            logger.warning(
                f"[PULADO] {nome_fds} foi interrompido pelo usuario apos "
                f"{h:02d}h {m:02d}m {s:02d}s. Iniciando o proximo caso.",
                extra={"console_visible": True},
            )
            monitor.cancelar_caso(arquivo_fds)
            registrar_interrupcao(cfg, arquivo_fds)
            return RESULTADO_PULADO

        if returncode != 0 or erro_fds:
            logger.error(
                f"[ERRO] FDS encerrou com falha apos {h:02d}h {m:02d}m {s:02d}s "
                f"(codigo {returncode})."
            )
            if ultimas_linhas:
                logger.error("Ultimas linhas do FDS:")
                for linha in ultimas_linhas:
                    logger.error(f"FDS | {linha}")
            registrar_erro_fds(
                cfg,
                arquivo_fds,
                returncode,
                elapsed,
                primeira_linha_erro,
                contexto_erro or list(ultimas_linhas),
            )
            monitor.finalizar_caso(
                arquivo_fds, False, elapsed,
                erro=primeira_linha_erro or f"FDS encerrou com codigo {returncode}",
            )
            return RESULTADO_FALHA
        logger.info(f"[SUCESSO] {nome_fds} — {h:02d}h {m:02d}m {s:02d}s")
        monitor.finalizar_caso(arquivo_fds, True, elapsed)
        _elapsed_dat = elapsed
        return elapsed

    except FileNotFoundError:
        mensagem = "Executável FDS não encontrado. Verifique 'diretorio_fds' no config.yaml."
        logger.error(mensagem)
        registrar_erro_automacao(cfg, arquivo_fds, mensagem)
        monitor.finalizar_caso(
            arquivo_fds, False, 0.0,
            erro="Executavel FDS nao encontrado (verifique diretorio_fds)",
        )
    except subprocess.CalledProcessError as e:
        elapsed = time.time() - inicio
        logger.error(f"[ERRO] FDS encerrou com código {e.returncode} após {elapsed:.1f}s.")
        registrar_erro_fds(
            cfg, arquivo_fds, e.returncode, elapsed,
            f"FDS encerrou com código {e.returncode}", (),
        )
        monitor.finalizar_caso(
            arquivo_fds, False, elapsed, erro=f"FDS encerrou com codigo {e.returncode}"
        )
    except KeyboardInterrupt:
        if processo is not None:
            logger.warning("Encerrando FDS/MPI (arvore completa de processos)...")
            # O mpiexec (Intel MPI/hydra) costuma sair logo apos lancar os
            # ranks via hydra_pmi_proxy — processo.poll() ja mostraria
            # "encerrado" mesmo com fds.exe ainda rodando, e taskkill /T
            # falharia (exige que o PID raiz ainda exista). O Job Object
            # criado por proteger_processo() nao tem essa limitacao.
            matar_job(job)
            if job is None:
                matar_arvore_processos(processo.pid)
            try:
                processo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        logger.warning("Simulação interrompida pelo usuário.")
        monitor.cancelar_caso(arquivo_fds)
        raise
    finally:
        encerrar_vigia_cancelamento.set()
        if processo is not None and processo.stdout is not None:
            try:
                processo.stdout.close()
            except OSError:
                pass
        fechar_job(job)
        registrar_tempo_dat(_obter_pasta_log(cfg), arquivo_fds, inicio, _elapsed_dat, cfg)

    return RESULTADO_FALHA


# ---------------------------------------------------------------------------
# Relatório final
# ---------------------------------------------------------------------------
def imprimir_relatorio(resultados: list[tuple[str, float]]) -> None:
    logger = logging.getLogger(__name__)
    atualizar_titulo_console("FDS Automation - fila finalizada")
    logger.info("")
    logger.info("=" * 55)
    logger.info("  RELATÓRIO FINAL DA FILA")
    logger.info("=" * 55)

    total_seg = 0.0
    for nome, elapsed in resultados:
        if elapsed == RESULTADO_PULADO:
            status, tempo = "PULOU", "--"
        elif elapsed < 0:
            status, tempo = "FALHOU", "--"
        else:
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = int(elapsed % 60)
            status = "OK"
            tempo  = f"{h:02d}h {m:02d}m {s:02d}s"
            total_seg += elapsed
        logger.info(f"  [{status:^6}] {os.path.basename(nome):<35} {tempo}")

    logger.info("-" * 55)
    h = int(total_seg // 3600)
    m = int((total_seg % 3600) // 60)
    s = int(total_seg % 60)
    logger.info(f"  TEMPO TOTAL (simulações OK): {h:02d}h {m:02d}m {s:02d}s")
    logger.info("=" * 55)


NOME_DAT = "tempos_simulacao.dat"

_CABECALHO_DAT = (
    "# FDS Automation — Registro de Tempos de Processamento\n"
    "# Formato: tab-separado (abra com Excel ou editor de texto)\n"
    "# Arquivo\tData_Inicio\tHora_Inicio\tData_Fim\tHora_Fim"
    "\tDuracao_s\tDuracao\tModo\tCores\tStatus\n"
)


def registrar_tempo_dat(
    pasta_log: str,
    arquivo_fds: str,
    inicio: float,
    elapsed: float,
    cfg: dict,
) -> None:
    """
    Grava uma linha de resultado em tempos_simulacao.dat (append).
    elapsed >= 0 indica sucesso; RESULTADO_PULADO indica interrupcao voluntaria;
    os demais valores negativos indicam falha.
    """
    dt_inicio = datetime.datetime.fromtimestamp(inicio)
    if elapsed >= 0:
        dt_fim = datetime.datetime.fromtimestamp(inicio + elapsed)
        status = "OK"
        duracao_s = int(elapsed)
        duracao_fmt = formatar_duracao(elapsed)
    elif elapsed == RESULTADO_PULADO:
        dt_fim = datetime.datetime.now()
        status = "PULOU"
        duracao_s = 0
        duracao_fmt = "--"
    else:
        dt_fim = datetime.datetime.now()
        status = "FALHOU"
        duracao_s = 0
        duracao_fmt = "--"

    modo = cfg.get("modo", "AUTO").upper()
    if modo == "HYBRID":
        mpi = cfg.get("hybrid", {}).get("mpi_procs", 1)
        omp = cfg.get("hybrid", {}).get("omp_threads", 1)
        cores_desc = f"{mpi}x{omp}"
    elif modo == "SERIAL":
        cores_desc = "1"
    else:
        cores_desc = str(cfg.get("num_cores", 1))

    nome = os.path.basename(arquivo_fds)
    caminho = os.path.join(pasta_log, NOME_DAT)
    try:
        os.makedirs(pasta_log, exist_ok=True)
        novo = not os.path.exists(caminho)
        with open(caminho, "a", encoding="utf-8") as f:
            if novo:
                f.write(_CABECALHO_DAT)
            f.write(
                f"{nome}\t"
                f"{dt_inicio.strftime('%Y-%m-%d')}\t"
                f"{dt_inicio.strftime('%H:%M:%S')}\t"
                f"{dt_fim.strftime('%Y-%m-%d')}\t"
                f"{dt_fim.strftime('%H:%M:%S')}\t"
                f"{duracao_s}\t"
                f"{duracao_fmt}\t"
                f"{modo}\t"
                f"{cores_desc}\t"
                f"{status}\n"
            )
    except Exception:
        pass  # falha no .dat nao deve interromper o fluxo principal


def _abrir_sessao_dat(pasta_log: str, n_simulacoes: int) -> None:
    """Insere separador de sessao no .dat para facilitar comparacao historica."""
    caminho = os.path.join(pasta_log, NOME_DAT)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(pasta_log, exist_ok=True)
        novo = not os.path.exists(caminho)
        with open(caminho, "a", encoding="utf-8") as f:
            if novo:
                f.write(_CABECALHO_DAT)
            f.write(
                f"#\n"
                f"# Sessao {ts} — FDS Automation v{VERSAO}"
                f" — {n_simulacoes} simulacao(es)\n"
                f"#\n"
            )
    except Exception:
        pass


def resolver_diretorio_fds(cfg: dict) -> dict:
    """
    Se 'diretorio_fds' nao existir neste PC, busca FDS e PyroSim
    automaticamente e atualiza cfg na sessao (sem gravar no config.yaml).
    """
    from path_detector import detect_fds_paths, detect_pyrosim_paths

    logger = logging.getLogger(__name__)
    diretorio = str(cfg.get("diretorio_fds", ""))

    if os.path.isdir(diretorio):
        return cfg  # caminho configurado existe

    logger.warning(
        f"diretorio_fds '{diretorio}' nao encontrado neste PC. "
        "Buscando instalacao de FDS/PyroSim automaticamente...",
        extra={"console_visible": True},
    )

    candidatos = detect_fds_paths() + detect_pyrosim_paths()
    for pasta in candidatos:
        tem_fds = (
            os.path.exists(os.path.join(pasta, "fds.exe"))
            or os.path.exists(os.path.join(pasta, "fds_openmp.exe"))
        )
        if tem_fds:
            logger.info(
                f"FDS encontrado automaticamente: {pasta}",
                extra={"console_visible": True},
            )
            cfg = deepcopy(cfg)
            cfg["diretorio_fds"] = pasta
            return cfg

    logger.error(
        "FDS nao encontrado automaticamente. "
        "Atualize 'diretorio_fds' no config.yaml com o caminho correto "
        "e use a opcao '5 - Verificar instalacao do FDS/MPI' para ajuda.",
        extra={"console_visible": True},
    )
    return cfg


def _obter_pasta_log(cfg: dict) -> str:
    return (
        str(cfg["pasta_simulacoes"])
        if "pasta_simulacoes" in cfg and cfg["pasta_simulacoes"]
        else os.path.dirname(
            str((cfg.get("simulacoes") or [cfg.get("arquivo_fds", ".")])[0])
        )
    )


# ---------------------------------------------------------------------------
# Teste de melhor configuração (roda só o otimizador, sem a simulação real)
# ---------------------------------------------------------------------------
def testar_melhor_configuracao(cfg: dict) -> None:
    """
    Roda o otimizador empirico para cada simulacao da fila, sem executar a
    simulacao real. O resultado fica salvo no cache do otimizador
    (.fds_optimizer_cache.json), entao quando a fila for executada de
    verdade ela reaproveita a configuracao ja testada em vez de testar de
    novo. Util para o usuario decidir a melhor configuracao antes de
    comprometer a fila inteira com a simulacao completa.
    """
    pasta_log = _obter_pasta_log(cfg)
    configurar_logging(pasta_log, cfg)
    logger = logging.getLogger(__name__)

    cfg = resolver_diretorio_fds(cfg)
    cfg_teste = deepcopy(cfg)
    cfg_teste.setdefault("otimizador", {})["ativo"] = True

    simulacoes = descobrir_simulacoes(cfg_teste)
    logger.info(
        f"Testando melhor configuracao para {len(simulacoes)} simulacao(es)...",
        extra={"console_visible": True},
    )

    for i, arquivo_fds in enumerate(simulacoes, start=1):
        logger.info("=" * 55, extra={"console_visible": True})
        logger.info(
            f"  TESTE {i}/{len(simulacoes)}: {os.path.basename(arquivo_fds)}",
            extra={"console_visible": True},
        )
        logger.info("=" * 55, extra={"console_visible": True})
        cfg_caso = aplicar_autoconfig(cfg_teste, arquivo_fds)
        aplicar_otimizador_desempenho(cfg_caso, arquivo_fds)

    logger.info(
        "Teste de configuracao concluido. A fila vai reaproveitar esse "
        "resultado (cache) ao rodar de verdade.",
        extra={"console_visible": True},
    )


# ---------------------------------------------------------------------------
# Fila dinâmica — detecta simulações adicionadas depois que a fila começou
# ---------------------------------------------------------------------------
def obter_cfg_fila_dinamica(cfg: dict | None) -> dict:
    if not isinstance(cfg, dict):
        return {}
    return cfg.get("fila_dinamica", {}) or {}


def _chave_arquivo(caminho: str) -> str:
    return os.path.normcase(os.path.abspath(caminho))


def redescobrir_novas_simulacoes(cfg: dict, conhecidos: set[str]) -> list[str]:
    """
    Re-escaneia a pasta principal e retorna os .fds que ainda nao estao na
    fila (subpastas/casos adicionados DEPOIS que a fila comecou a rodar).
    So funciona no modo 'pasta_simulacoes' (lista manual e arquivo unico
    sao fixos por definicao). Best-effort: qualquer erro retorna [].
    """
    fd_cfg = obter_cfg_fila_dinamica(cfg)
    if not bool(fd_cfg.get("ativo", True)):
        return []

    pasta = str(cfg.get("pasta_simulacoes", "") or "")
    if not pasta or not os.path.isdir(pasta):
        return []

    # Ignora arquivos modificados ha poucos segundos: podem ainda estar
    # sendo copiados/sincronizados para a pasta. Ficam para a proxima
    # verificacao (apos a proxima simulacao).
    try:
        idade_min = float(fd_cfg.get("idade_min_seg", 10))
    except (TypeError, ValueError):
        idade_min = 10.0

    agora = time.time()
    novas = []
    try:
        for arquivo in _arquivos_fds_da_pasta(pasta):
            if _chave_arquivo(arquivo) in conhecidos:
                continue
            try:
                if agora - os.path.getmtime(arquivo) < idade_min:
                    continue
            except OSError:
                continue
            novas.append(arquivo)
    except Exception:
        logging.getLogger(__name__).debug(
            "Re-escaneamento da pasta de simulacoes falhou.", exc_info=True
        )
    return novas


def _cfg_para_simulacao_nova(cfg: dict) -> dict:
    """
    Simulacoes detectadas com a fila ja rodando nao passaram pelo teste de
    melhor configuracao feito no inicio. Se 'otimizar_novas' estiver ativo
    (padrao), forca o otimizador para elas — o resultado fica no cache e,
    se o caso ja tiver sido testado antes, reaproveita sem testar de novo.
    """
    if otimizador_ativo(cfg):
        return cfg  # otimizador ja vai rodar normalmente
    if not bool(obter_cfg_fila_dinamica(cfg).get("otimizar_novas", True)):
        return cfg

    cfg_caso = deepcopy(cfg)
    cfg_caso.setdefault("otimizador", {})["ativo"] = True
    logging.getLogger(__name__).info(
        "Simulacao nova detectada com a fila rodando: o otimizador vai "
        "descobrir a melhor configuracao para ela antes de rodar.",
        extra={"console_visible": True},
    )
    return cfg_caso


# ---------------------------------------------------------------------------
# Fila de simulações (reutilizável pelo CLI e pelo menu)
# ---------------------------------------------------------------------------
def executar_fila(cfg: dict) -> bool:
    pasta_log = _obter_pasta_log(cfg)
    configurar_logging(pasta_log, cfg)
    logger = logging.getLogger(__name__)

    cfg = resolver_diretorio_fds(cfg)
    iniciar_sessao_diagnosticos(cfg, pasta_log)
    logger.info(f"FDS Automation v{VERSAO}")

    if not cfg.get("auto_config", False):
        logger.info(f"Modo manual: {cfg['modo'].upper()}")
    else:
        logger.info("Auto-config ATIVADO — hardware será detectado por simulação.")

    if otimizador_ativo(cfg):
        logger.info("Otimizador empirico ATIVADO: testes curtos escolherao a configuracao mais rapida.")

    simulacoes = descobrir_simulacoes(cfg)
    logger.info(f"Total de simulações na fila: {len(simulacoes)}")

    _abrir_sessao_dat(pasta_log, len(simulacoes))
    elevar_prioridade()

    monitor = MonitorPlanilha(cfg)
    monitor.iniciar_fila(simulacoes)

    if bool(obter_cfg_fila_dinamica(cfg).get("ativo", True)) and cfg.get("pasta_simulacoes"):
        logger.info(
            "Fila dinamica ATIVA: novas subpastas com .fds adicionadas a "
            "pasta principal entram automaticamente no final da fila."
        )

    # Fila dinamica: a lista pode CRESCER durante a execucao (novas
    # subpastas detectadas apos cada simulacao), por isso while + indice
    # em vez de um for sobre a lista original.
    fila: list[str] = list(simulacoes)
    conhecidos: set[str] = {_chave_arquivo(p) for p in fila}
    detectadas_depois: set[str] = set()

    resultados: list[tuple[str, float]] = []
    fila_interrompida = False
    try:
        i = 0
        while i < len(fila):
            arquivo_fds = fila[i]
            i += 1

            cfg_caso = cfg
            if _chave_arquivo(arquivo_fds) in detectadas_depois:
                cfg_caso = _cfg_para_simulacao_nova(cfg)

            try:
                elapsed = executar_simulacao(
                    arquivo_fds, cfg_caso, i, len(fila), monitor
                )
            except KeyboardInterrupt:
                raise
            except PularCasoSolicitado:
                elapsed = RESULTADO_PULADO
                logger.warning(
                    f"[PULADO] {os.path.basename(arquivo_fds)} foi "
                    "interrompido pelo usuario durante a preparacao/teste. "
                    "Iniciando o proximo caso.",
                    extra={"console_visible": True},
                )
                monitor.cancelar_caso(arquivo_fds)
                registrar_interrupcao(cfg_caso, arquivo_fds)
                registrar_tempo_dat(
                    pasta_log, arquivo_fds, time.time(),
                    RESULTADO_PULADO, cfg_caso,
                )
            except Exception as exc:
                # Um caso malformado/sem arquivos auxiliares nao deve impedir
                # que os demais casos independentes da fila sejam executados.
                elapsed = RESULTADO_FALHA
                logger.error(
                    f"Falha antes/durante a preparacao de "
                    f"{os.path.basename(arquivo_fds)}: {exc}",
                    exc_info=True,
                )
                monitor.finalizar_caso(
                    arquivo_fds, False, 0.0, erro=str(exc)[:250]
                )
                registrar_erro_automacao(
                    cfg_caso, arquivo_fds, str(exc) or type(exc).__name__
                )
            resultados.append((arquivo_fds, elapsed))
            logar_status_pos_simulacao(arquivo_fds, i, len(fila), elapsed, cfg)

            # O re-escaneamento vem DEPOIS do status (que limpa o console):
            # assim o aviso de novas simulacoes permanece visivel na tela.
            novas = redescobrir_novas_simulacoes(cfg, conhecidos)
            if novas:
                fila.extend(novas)
                for nova in novas:
                    conhecidos.add(_chave_arquivo(nova))
                    detectadas_depois.add(_chave_arquivo(nova))
                monitor.iniciar_fila(novas)
                logger.info(
                    f"{len(novas)} nova(s) simulacao(oes) detectada(s) na pasta "
                    f"e adicionada(s) ao FINAL da fila (total agora: {len(fila)}):",
                    extra={"console_visible": True},
                )
                for nova in novas:
                    logger.info(
                        f"  + {os.path.basename(nova)}",
                        extra={"console_visible": True},
                    )
    except KeyboardInterrupt:
        fila_interrompida = True
        logger.warning("Fila interrompida pelo usuário.")
    except Exception:
        fila_interrompida = True
        logger.error("Erro inesperado na fila de simulacoes:", exc_info=True)

    imprimir_relatorio(resultados)
    concluidas = sum(elapsed >= 0 for _, elapsed in resultados)
    interrompidas = sum(elapsed == RESULTADO_PULADO for _, elapsed in resultados)
    falhas = sum(
        elapsed < 0 and elapsed != RESULTADO_PULADO
        for _, elapsed in resultados
    )
    registrar_resumo_diagnosticos(cfg, concluidas, falhas, interrompidas)
    return (
        not fila_interrompida
        and len(resultados) == len(fila)
        and bool(resultados)
        and falhas == 0
    )


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=f"Gerenciador de Simulações FDS v{VERSAO}",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Caminho para o arquivo de configuração YAML",
    )
    args = parser.parse_args()

    cfg = carregar_config(args.config)
    return 0 if executar_fila(cfg) else 2


if __name__ == "__main__":
    sys.exit(main())
