# FDS Automation v1.15
import os
import subprocess
import time
import logging
import argparse

import yaml    # pip install pyyaml
import psutil  # pip install psutil

from fds_reader   import preparar_ambiente_fds, contar_meshes
from hw_detector  import detectar_hardware, recomendar_config, logar_hardware
from mesh_splitter import processar_meshes

VERSAO = "1.15"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def configurar_logging(pasta_log: str) -> None:
    os.makedirs(pasta_log, exist_ok=True)
    log_path = os.path.join(pasta_log, "fds_run.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
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

        arquivos = sorted(
            os.path.join(pasta, f)
            for f in os.listdir(pasta)
            if f.lower().endswith(".fds")
        )

        if not arquivos:
            raise FileNotFoundError(f"Nenhum arquivo .fds encontrado em: {pasta}")

        logger.info(f"{len(arquivos)} arquivo(s) .fds encontrado(s) em '{pasta}':")
        for arq in arquivos:
            logger.info(f"  ‣ {os.path.basename(arq)}")
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

    processar_meshes(arquivo_fds, n_cores, margem_pct, dry_run)


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
        if n_meshes > 0 and n_procs != n_meshes:
            logger.warning(
                f"{n_meshes} mesh(es) detectada(s), mas {n_procs} processo(s) MPI "
                "configurado(s). Ideal: 1 processo por mesh."
            )
        elif n_meshes > 0:
            logger.info(f"Validação MPI OK: {n_procs} processo(s) × {n_meshes} mesh(es).")


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
            f"HYBRID | {mpi_procs} processo(s) MPI × {omp_threads} thread(s) OpenMP "
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


# ---------------------------------------------------------------------------
# Execução de uma simulação
# ---------------------------------------------------------------------------
def executar_simulacao(arquivo_fds: str, cfg: dict, idx: int, total: int) -> float:
    logger   = logging.getLogger(__name__)
    pasta    = os.path.dirname(arquivo_fds)
    nome_fds = os.path.basename(arquivo_fds)

    logger.info("=" * 55)
    logger.info(f"  SIMULAÇÃO {idx}/{total}: {nome_fds}")
    logger.info("=" * 55)

    # 1. Auto-configuração por hardware
    cfg = aplicar_autoconfig(cfg, arquivo_fds)
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
    try:
        creationflags = 0
        if hasattr(subprocess, 'HIGH_PRIORITY_CLASS'):
            creationflags = subprocess.HIGH_PRIORITY_CLASS

        subprocess.run(
            comando, cwd=pasta, check=True,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        elapsed = time.time() - inicio
        h, m, s = int(elapsed//3600), int((elapsed%3600)//60), int(elapsed%60)
        logger.info(f"[SUCESSO] {nome_fds} — {h:02d}h {m:02d}m {s:02d}s")
        return elapsed

    except FileNotFoundError:
        logger.error("Executável FDS não encontrado. Verifique 'diretorio_fds' no config.yaml.")
    except subprocess.CalledProcessError as e:
        elapsed = time.time() - inicio
        logger.error(f"[ERRO] FDS encerrou com código {e.returncode} após {elapsed:.1f}s.")
    except KeyboardInterrupt:
        logger.warning("Simulação interrompida pelo usuário.")
        raise

    return -1.0


# ---------------------------------------------------------------------------
# Relatório final
# ---------------------------------------------------------------------------
def imprimir_relatorio(resultados: list[tuple[str, float]]) -> None:
    logger = logging.getLogger(__name__)
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
    configurar_logging(pasta_log)
    logger = logging.getLogger(__name__)

    logger.info(f"FDS Automation v{VERSAO}")

    if not cfg.get("auto_config", False):
        logger.info(f"Modo manual: {cfg['modo'].upper()}")
    else:
        logger.info("Auto-config ATIVADO — hardware será detectado por simulação.")

    simulacoes = descobrir_simulacoes(cfg)
    logger.info(f"Total de simulações na fila: {len(simulacoes)}")

    elevar_prioridade()

    resultados: list[tuple[str, float]] = []
    try:
        for i, arquivo_fds in enumerate(simulacoes, start=1):
            elapsed = executar_simulacao(arquivo_fds, cfg, i, len(simulacoes))
            resultados.append((arquivo_fds, elapsed))
    except KeyboardInterrupt:
        logger.warning("Fila interrompida pelo usuário.")

    imprimir_relatorio(resultados)


if __name__ == "__main__":
    main()
