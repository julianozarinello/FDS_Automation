import os
import subprocess
import time
import logging
import argparse

import yaml  # pip install pyyaml

from fds_reader import preparar_ambiente_fds, contar_meshes


# ---------------------------------------------------------------------------
# Configuração de logging (console + arquivo fds_run.log)
# ---------------------------------------------------------------------------
def configurar_logging(pasta_projeto: str) -> None:
    log_path = os.path.join(pasta_projeto, "fds_run.log")
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
# Carregamento de configuração
# ---------------------------------------------------------------------------
def carregar_config(caminho_config: str) -> dict:
    if not os.path.exists(caminho_config):
        raise FileNotFoundError(
            f"Arquivo de configuração não encontrado: {caminho_config}\n"
            "Crie um config.yaml baseado no exemplo do repositório."
        )
    with open(caminho_config, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Validações de pré-execução
# ---------------------------------------------------------------------------
def validar_entradas(cfg: dict) -> None:
    logger = logging.getLogger(__name__)

    arquivo_fds = cfg["arquivo_fds"]
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

    # Validação MPI: número de processos deve ser igual ao número de meshes
    if modo in ("MPI", "HYBRID"):
        n_meshes = contar_meshes(arquivo_fds)
        n_procs = (
            cfg["hybrid"]["mpi_procs"] if modo == "HYBRID" else cfg["num_cores"]
        )
        if n_meshes > 0 and n_procs != n_meshes:
            logger.warning(
                f"{n_meshes} mesh(es) detectada(s), mas {n_procs} processo(s) MPI configurado(s). "
                "No MPI, o ideal é 1 processo por mesh para máxima eficiência."
            )
        elif n_meshes > 0:
            logger.info(f"Validação MPI OK: {n_procs} processo(s) para {n_meshes} mesh(es).")


# ---------------------------------------------------------------------------
# Construção do comando de execução
# ---------------------------------------------------------------------------
def construir_comando(cfg: dict, pasta_projeto: str) -> tuple[list, str]:
    modo = cfg["modo"].upper()
    nome_fds = os.path.basename(cfg["arquivo_fds"])

    fds_openmp = os.path.join(pasta_projeto, "fds_openmp.exe")
    fds_exe    = os.path.join(pasta_projeto, "fds.exe")
    mpiexec    = os.path.join(pasta_projeto, "mpiexec.exe")

    if modo == "OPENMP":
        os.environ["OMP_NUM_THREADS"] = str(cfg["num_cores"])
        status = f"Modo OPENMP | {cfg['num_cores']} thread(s)"
        return [fds_openmp, nome_fds], status

    elif modo == "MPI":
        os.environ["OMP_NUM_THREADS"] = "1"
        status = f"Modo MPI | {cfg['num_cores']} processo(s)"
        return [mpiexec, "-n", str(cfg["num_cores"]), fds_exe, nome_fds], status

    elif modo == "HYBRID":
        mpi_procs   = cfg["hybrid"]["mpi_procs"]
        omp_threads = cfg["hybrid"]["omp_threads"]
        os.environ["OMP_NUM_THREADS"] = str(omp_threads)
        status = (
            f"Modo HYBRID | {mpi_procs} processo(s) MPI × {omp_threads} thread(s) OpenMP "
            f"= {mpi_procs * omp_threads} núcleo(s)"
        )
        return [mpiexec, "-n", str(mpi_procs), fds_exe, nome_fds], status

    else:  # SERIAL
        os.environ["OMP_NUM_THREADS"] = "1"
        status = "Modo SERIAL"
        return [fds_exe, nome_fds], status


# ---------------------------------------------------------------------------
# Ponto de entrada principal
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Gerenciador de Simulações FDS",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Caminho para o arquivo de configuração YAML",
    )
    args = parser.parse_args()

    # Carrega configuração
    cfg = carregar_config(args.config)
    pasta_projeto = os.path.dirname(cfg["arquivo_fds"])

    # Inicia logging (agora que sabemos a pasta do projeto)
    configurar_logging(pasta_projeto)
    logger = logging.getLogger(__name__)

    logger.info("=" * 50)
    logger.info("  GERENCIADOR DE SIMULAÇÕES FDS")
    logger.info("=" * 50)
    logger.info(f"Arquivo FDS : {cfg['arquivo_fds']}")
    logger.info(f"Modo        : {cfg['modo'].upper()}")

    # Valida entradas
    validar_entradas(cfg)

    # Prepara ambiente (copia binários FDS)
    preparar_ambiente_fds(cfg["diretorio_fds"], pasta_projeto)

    # Monta o comando
    comando, status_msg = construir_comando(cfg, pasta_projeto)
    logger.info(f"Iniciando   : {status_msg}")
    logger.info("-" * 50)

    # Executa e mede o tempo
    inicio = time.time()
    try:
        subprocess.run(comando, cwd=pasta_projeto, check=True)
        elapsed = time.time() - inicio
        horas   = int(elapsed // 3600)
        minutos = int((elapsed % 3600) // 60)
        segundos = int(elapsed % 60)
        logger.info("-" * 50)
        logger.info(f"[SUCESSO] Simulação finalizada em {horas:02d}h {minutos:02d}m {segundos:02d}s")

    except FileNotFoundError:
        logger.error(
            "Executável FDS não encontrado na pasta do projeto. "
            "Verifique o campo 'diretorio_fds' no config.yaml."
        )
    except subprocess.CalledProcessError as e:
        elapsed = time.time() - inicio
        logger.error(
            f"[ERRO] FDS encerrou com código {e.returncode} após "
            f"{elapsed:.1f}s. Verifique o arquivo .fds e o log acima."
        )
    except KeyboardInterrupt:
        logger.warning("Simulação interrompida pelo usuário.")


if __name__ == "__main__":
    main()
