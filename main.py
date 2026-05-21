# FDS Automation v1.16
import os
import subprocess
import time
import logging
import argparse
import sys
import re
import shutil
import tempfile
from collections import deque
from copy import deepcopy

import yaml    # pip install pyyaml
import psutil  # pip install psutil

from fds_reader   import preparar_ambiente_fds, contar_meshes, contar_meshes_com_mpi_process
from hw_detector  import detectar_hardware, recomendar_config, logar_hardware
from mesh_splitter import processar_meshes
from optimizer    import aplicar_otimizador_desempenho, otimizador_ativo

VERSAO = "1.16"

FDS_ERROR_MARKERS = (
    "ERROR(",
    "ERROR:",
    "FDS WAS IMPROPERLY SET-UP",
    "FDS STOPPED",
)

_RE_FDS_TIME_STEP = re.compile(
    r"Time Step:\s*(\d+),\s*Simulation Time:\s*([0-9.Ee+\-]+)\s*s",
    re.IGNORECASE,
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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[file_handler, console_handler],
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

        arquivos = []
        for raiz, dirs, nomes in os.walk(pasta):
            dirs.sort()
            for nome in sorted(nomes):
                if nome.lower().endswith(".fds"):
                    arquivos.append(os.path.join(raiz, nome))

        arquivos.sort(key=lambda p: os.path.relpath(p, pasta).lower())

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
    n_meshes = contar_meshes(arquivo_fds)
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

    n_cores    = (
        cfg["hybrid"]["mpi_procs"] if modo == "HYBRID" else cfg["num_cores"]
    )
    margem_pct = float(ms_cfg.get("margem_pct", 10.0))
    dry_run    = bool(ms_cfg.get("dry_run",    False))
    pular_se_otimizado = bool(ms_cfg.get("pular_se_otimizado", True))
    peso_espacial = float(ms_cfg.get("peso_espacial", 0.15))

    processar_meshes(
        arquivo_fds,
        n_cores,
        margem_pct,
        dry_run,
        pular_se_otimizado,
        peso_espacial,
    )


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

    # Evita conflito entre MKL e OpenMP (crítico para Intel Fortran)
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["MKL_DYNAMIC"]     = "FALSE"
    os.environ["OMP_DYNAMIC"]     = "FALSE"
    os.environ["OMP_DISPLAY_ENV"] = "FALSE"

    # Intel MPI pin (modo MPI/HYBRID)
    if modo in ("MPI", "HYBRID"):
        os.environ["I_MPI_PIN"]       = "1"
        os.environ["I_MPI_PIN_ORDER"] = "compact"


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
        n_meshes = contar_meshes(arquivo_fds)
        n_procs  = cfg["hybrid"]["mpi_procs"] if modo == "HYBRID" else cfg["num_cores"]
        if n_meshes > n_procs:
            n_mpi_process = contar_meshes_com_mpi_process(arquivo_fds)
            if n_mpi_process == n_meshes:
                logger.info(
                    f"Validação MPI OK: {n_meshes} mesh(es) distribuida(s) "
                    f"em {n_procs} processo(s) via MPI_PROCESS."
                )
            else:
                logger.warning(
                    f"{n_meshes} mesh(es) detectada(s), mas apenas {n_procs} "
                    f"processo(s) MPI configurado(s). O FDS exige MPI_PROCESS "
                    "em cada &MESH nessa situacao."
                )
        elif n_meshes > 0 and n_procs != n_meshes:
            logger.warning(
                f"{n_meshes} mesh(es) detectada(s), mas {n_procs} processo(s) MPI "
                "configurado(s). Ideal: 1 processo por mesh."
            )
        elif n_meshes > 0:
            logger.info(f"Validação MPI OK: {n_procs} processo(s) x {n_meshes} mesh(es).")


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
             fds_exe, nome_fds],
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
             fds_exe, nome_fds],
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
    cores = max(1, int(hw.get("cores_fisicos", 1)))
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
        )
        return

    processo.terminate()
    try:
        processo.wait(timeout=10)
    except subprocess.TimeoutExpired:
        processo.kill()


def _score_saida_benchmark(saida: str, elapsed: float) -> tuple[float, str]:
    if any(marcador in saida.upper() for marcador in FDS_ERROR_MARKERS):
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
        )

        try:
            saida, _ = processo.communicate(timeout=timeout_seg)
        except subprocess.TimeoutExpired:
            _encerrar_processo_arvore(processo)
            saida, _ = processo.communicate(timeout=10)

    elapsed = time.time() - inicio
    return _score_saida_benchmark(saida or "", elapsed)


def aplicar_auto_benchmark(cfg: dict, arquivo_fds: str) -> dict:
    bench_cfg = obter_cfg_benchmark(cfg)
    if not bench_cfg.get("ativo", False):
        return cfg
    if not os.path.exists(cfg.get("diretorio_fds", "")):
        logging.getLogger(__name__).warning(
            "Auto-benchmark ignorado: diretorio_fds nao encontrado."
        )
        return cfg

    logger = logging.getLogger(__name__)
    timeout_seg = float(bench_cfg.get("tempo_max_seg", 45))
    n_meshes = contar_meshes(arquivo_fds)
    hw = detectar_hardware()
    candidatos = gerar_candidatos_benchmark(cfg, hw, n_meshes)

    if len(candidatos) <= 1:
        logger.info("Auto-benchmark ignorado: apenas um candidato disponivel.")
        return cfg

    logger.info("Auto-benchmark iniciado em copia temporaria do caso.")
    resultados = []
    for cand in candidatos:
        _, status = construir_comando_binarios_instalados(cand, cand["diretorio_fds"], os.path.basename(arquivo_fds))
        try:
            score, detalhe = executar_benchmark_candidato(cand, arquivo_fds, timeout_seg)
        except Exception as exc:
            score, detalhe = 0.0, f"falhou: {exc}"
        logger.info(f"  Benchmark {status}: score={score:.6g} ({detalhe})")
        resultados.append((score, cand, status, detalhe))

    resultados.sort(key=lambda item: item[0], reverse=True)
    melhor_score, melhor_cfg, melhor_status, melhor_detalhe = resultados[0]
    if melhor_score <= 0:
        logger.warning("Auto-benchmark nao mediu progresso valido. Mantendo auto-config.")
        return cfg

    logger.info(
        f"Auto-benchmark selecionou {melhor_status} "
        f"(score={melhor_score:.6g}; {melhor_detalhe})."
    )
    return melhor_cfg


def linha_indica_erro_fds(linha: str) -> bool:
    linha_upper = linha.upper()
    return any(marcador in linha_upper for marcador in FDS_ERROR_MARKERS)


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

    if not sys.stdout.isatty():
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
def executar_simulacao(arquivo_fds: str, cfg: dict, idx: int, total: int) -> float:
    logger   = logging.getLogger(__name__)
    pasta    = os.path.dirname(arquivo_fds)
    nome_fds = os.path.basename(arquivo_fds)

    logar_status_fila(arquivo_fds, idx, total, cfg)

    logger.info("=" * 55)
    logger.info(f"  SIMULAÇÃO {idx}/{total}: {nome_fds}")
    logger.info("=" * 55)

    # 1. Auto-configuração por hardware
    cfg = aplicar_autoconfig(cfg, arquivo_fds)
    if otimizador_ativo(cfg):
        cfg = aplicar_otimizador_desempenho(cfg, arquivo_fds)
    else:
        cfg = aplicar_auto_benchmark(cfg, arquivo_fds)
    modo = cfg["modo"].upper()

    # 2. Mesh Splitter (divide + reordena meshes antes de rodar)
    aplicar_mesh_splitter(cfg, arquivo_fds)

    # 3. Validação, preparação e execução
    validar_simulacao(arquivo_fds, cfg)
    preparar_ambiente_fds(cfg["diretorio_fds"], pasta)
    aplicar_variaveis_performance(cfg, modo)

    comando, status_msg = construir_comando(cfg, pasta, nome_fds)
    logger.info(f"Modo  : {status_msg}")
    logger.info(f"Pasta : {pasta}")
    logger.info("-" * 55)

    inicio = time.time()
    processo = None
    try:
        creationflags = 0
        if hasattr(subprocess, 'HIGH_PRIORITY_CLASS'):
            creationflags = subprocess.HIGH_PRIORITY_CLASS

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

        erro_fds = False
        ultimas_linhas = deque(maxlen=30)
        saida_fds_no_console = mostrar_saida_fds_console(cfg)
        intervalo_status = intervalo_status_console(cfg)
        ultimo_status_console = 0.0

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

                progresso = extrair_time_step_fds(linha)
                agora = time.time()
                if (
                    progresso
                    and not saida_fds_no_console
                    and agora - ultimo_status_console >= intervalo_status
                ):
                    step, tempo_simulado = progresso
                    decorrido = formatar_duracao(agora - inicio)
                    logger.info(
                        f"Rodando: {nome_fds} | passo {step} | "
                        f"tempo simulado {tempo_simulado}s | decorrido {decorrido}",
                        extra={"console_visible": True},
                    )
                    ultimo_status_console = agora

        returncode = processo.wait()
        elapsed = time.time() - inicio
        h, m, s = int(elapsed//3600), int((elapsed%3600)//60), int(elapsed%60)

        if returncode != 0 or erro_fds:
            logger.error(
                f"[ERRO] FDS encerrou com falha apos {h:02d}h {m:02d}m {s:02d}s "
                f"(codigo {returncode})."
            )
            if ultimas_linhas:
                logger.error("Ultimas linhas do FDS:")
                for linha in ultimas_linhas:
                    logger.error(f"FDS | {linha}")
            return -1.0
        logger.info(f"[SUCESSO] {nome_fds} — {h:02d}h {m:02d}m {s:02d}s")
        return elapsed

    except FileNotFoundError:
        logger.error("Executável FDS não encontrado. Verifique 'diretorio_fds' no config.yaml.")
    except subprocess.CalledProcessError as e:
        elapsed = time.time() - inicio
        logger.error(f"[ERRO] FDS encerrou com código {e.returncode} após {elapsed:.1f}s.")
    except KeyboardInterrupt:
        if processo is not None and processo.poll() is None:
            logger.warning("Encerrando processo FDS em execucao...")
            processo.terminate()
            try:
                processo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                processo.kill()
        logger.warning("Simulação interrompida pelo usuário.")
        raise

    return -1.0


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
        if elapsed < 0:
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


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def main():
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

    pasta_log = (
        str(cfg["pasta_simulacoes"])
        if "pasta_simulacoes" in cfg and cfg["pasta_simulacoes"]
        else os.path.dirname(
            str((cfg.get("simulacoes") or [cfg.get("arquivo_fds", ".")])[0])
        )
    )
    configurar_logging(pasta_log, cfg)
    logger = logging.getLogger(__name__)

    logger.info(f"FDS Automation v{VERSAO}")

    if not cfg.get("auto_config", False):
        logger.info(f"Modo manual: {cfg['modo'].upper()}")
    else:
        logger.info("Auto-config ATIVADO — hardware será detectado por simulação.")

    if otimizador_ativo(cfg):
        logger.info("Otimizador empirico ATIVADO: testes curtos escolherao a configuracao mais rapida.")

    simulacoes = descobrir_simulacoes(cfg)
    logger.info(f"Total de simulações na fila: {len(simulacoes)}")

    elevar_prioridade()

    resultados: list[tuple[str, float]] = []
    try:
        for i, arquivo_fds in enumerate(simulacoes, start=1):
            elapsed = executar_simulacao(arquivo_fds, cfg, i, len(simulacoes))
            resultados.append((arquivo_fds, elapsed))
            logar_status_pos_simulacao(arquivo_fds, i, len(simulacoes), elapsed, cfg)
    except KeyboardInterrupt:
        logger.warning("Fila interrompida pelo usuário.")

    imprimir_relatorio(resultados)


if __name__ == "__main__":
    main()
