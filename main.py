# FDS Automation v1.12
import os
import subprocess
import time
import logging
import argparse

import yaml   # pip install pyyaml
import psutil # pip install psutil

from fds_reader import preparar_ambiente_fds, contar_meshes

VERSAO = "1.12"


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


def normalizar_lista_simulacoes(cfg: dict) -> list[str]:
    """
    Retorna sempre uma lista de caminhos .fds.
    Aceita tanto 'arquivo_fds' (string única) quanto 'simulacoes' (lista).
    """
    if "simulacoes" in cfg and cfg["simulacoes"]:
        return [str(p) for p in cfg["simulacoes"]]
    if "arquivo_fds" in cfg and cfg["arquivo_fds"]:
        return [str(cfg["arquivo_fds"])]
    raise ValueError("Nenhum arquivo FDS definido. Use 'arquivo_fds' ou 'simulacoes' no config.yaml.")


# ---------------------------------------------------------------------------
# Variáveis de ambiente para performance
# ---------------------------------------------------------------------------
def aplicar_variaveis_performance(cfg: dict, modo: str) -> None:
    """
    Define variáveis de ambiente que melhoram o desempenho de execuções
    OpenMP e MPI antes de lançar o processo FDS.
    """
    perf = cfg.get("performance", {})

    # Número de threads OpenMP
    if modo == "OPENMP":
        os.environ["OMP_NUM_THREADS"] = str(cfg["num_cores"])
    elif modo == "HYBRID":
        os.environ["OMP_NUM_THREADS"] = str(cfg["hybrid"]["omp_threads"])
    else:
        os.environ["OMP_NUM_THREADS"] = "1"

    # Afinidade de threads Intel MKL/OpenMP: threads ficam nos núcleos físicos
    # evita migração de thread entre núcleos (reduz latência de cache)
    if perf.get("kmp_affinity", True):
        os.environ["KMP_AFFINITY"]    = "granularity=fine,compact,1,0"
        os.environ["OMP_PROC_BIND"]   = "close"

    # Mantém threads em spin-wait por mais tempo antes de dormir
    # (elimina latência de wake-up em iterações rápidas)
    if perf.get("omp_spin", True):
        os.environ["GOMP_SPINCOUNT"]  = "10000000"
        os.environ["OMP_WAIT_POLICY"] = "active"
        os.environ["KMP_BLOCKTIME"]   = "200"  # ms

    # Desativa alocação NUMA não-local (evita acesso cross-socket)
    if perf.get("numa_opt", True):
        os.environ["OMP_PLACES"] = "cores"


# ---------------------------------------------------------------------------
# Prioridade de processo
# ---------------------------------------------------------------------------
def elevar_prioridade() -> None:
    """Eleva a prioridade do processo Python (e consequentemente do FDS)
    para HIGH no Windows, reduzindo interferência de outros processos."""
    logger = logging.getLogger(__name__)
    try:
        proc = psutil.Process(os.getpid())
        proc.nice(psutil.HIGH_PRIORITY_CLASS)  # Windows
        logger.info("Prioridade de processo elevada para HIGH.")
    except (AttributeError, psutil.AccessDenied):
        try:
            proc.nice(-10)  # Linux/macOS fallback
            logger.info("Prioridade de processo elevada (nice -10).")
        except psutil.AccessDenied:
            logger.warning(
                "Não foi possível elevar a prioridade (execute como Administrador "
                "para habilitar esta otimização)."
            )


# ---------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------
def validar_simulacao(arquivo_fds: str, cfg: dict) -> None:
    logger = logging.getLogger(__name__)

    if not os.path.exists(arquivo_fds):
        raise FileNotFoundError(f"Arquivo FDS não encontrado: {arquivo_fds}")

    diretorio_fds = cfg["diretorio_fds"]
    if not os.path.exists(diretorio_fds):
        raise FileNotFoundError(
            f"Diretório do FDS não encontrado: {diretorio_fds}\n"
            "Verifique o campo 'diretorio_fds' no config.yaml."
        )

    modo = cfg["modo"].upper()
    modos_validos = {"SERIAL", "OPENMP", "MPI", "HYBRID"}
    if modo not in modos_validos:
        raise ValueError(f"Modo inválido: '{modo}'. Escolha entre: {modos_validos}")

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
        # -bind-to core: cada processo MPI é fixado a um núcleo físico
        status = f"MPI | {n} processo(s)"
        return [mpiexec, "-n", str(n), "-bind-to", "core", fds_exe, nome_fds], status

    elif modo == "HYBRID":
        mpi_procs   = cfg["hybrid"]["mpi_procs"]
        omp_threads = cfg["hybrid"]["omp_threads"]
        status = (
            f"HYBRID | {mpi_procs} processo(s) MPI × {omp_threads} thread(s) OpenMP "
            f"= {mpi_procs * omp_threads} núcleo(s)"
        )
        return (
            [mpiexec, "-n", str(mpi_procs), "-bind-to", "core",
             "-map-by", f"socket:PE={omp_threads}", fds_exe, nome_fds],
            status,
        )

    else:  # SERIAL
        return [fds_exe, nome_fds], "SERIAL"


# ---------------------------------------------------------------------------
# Execução de uma simulação
# ---------------------------------------------------------------------------
def executar_simulacao(arquivo_fds: str, cfg: dict, idx: int, total: int) -> float:
    """
    Valida, prepara e executa uma simulação FDS.
    Retorna o tempo decorrido em segundos (ou -1 se falhou).
    """
    logger       = logging.getLogger(__name__)
    pasta        = os.path.dirname(arquivo_fds)
    nome_fds     = os.path.basename(arquivo_fds)
    modo         = cfg["modo"].upper()

    logger.info("=" * 55)
    logger.info(f"  SIMULAÇÃO {idx}/{total}: {nome_fds}")
    logger.info("=" * 55)

    validar_simulacao(arquivo_fds, cfg)
    preparar_ambiente_fds(cfg["diretorio_fds"], pasta)
    aplicar_variaveis_performance(cfg, modo)

    comando, status_msg = construir_comando(cfg, pasta, nome_fds)
    logger.info(f"Modo        : {status_msg}")
    logger.info(f"Pasta       : {pasta}")
    logger.info("-" * 55)

    inicio = time.time()
    try:
        subprocess.run(comando, cwd=pasta, check=True)
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
        raise  # propaga para interromper a fila também

    return -1.0


# ---------------------------------------------------------------------------
# Relatório final da fila
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
            status = "FALHOU"
            tempo  = "--"
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

    cfg         = carregar_config(args.config)
    simulacoes  = normalizar_lista_simulacoes(cfg)
    pasta_log   = os.path.dirname(simulacoes[0])

    configurar_logging(pasta_log)
    logger = logging.getLogger(__name__)

    logger.info(f"FDS Automation v{VERSAO}")
    logger.info(f"{len(simulacoes)} simulação(ões) na fila | Modo: {cfg['modo'].upper()}")

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
