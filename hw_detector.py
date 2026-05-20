# hw_detector.py — Detecção automática de hardware para FDS Automation
import os
import logging
import platform

import psutil  # pip install psutil

try:
    import cpuinfo  # pip install py-cpuinfo
    _CPUINFO_OK = True
except ImportError:
    _CPUINFO_OK = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coleta de informações de hardware
# ---------------------------------------------------------------------------
def detectar_hardware() -> dict:
    """
    Retorna um dicionário com informações detalhadas do hardware.
    """
    info = {}

    # Cores
    info["cores_logicos"]  = psutil.cpu_count(logical=True)  or 1
    info["cores_fisicos"]  = psutil.cpu_count(logical=False) or 1
    info["hyperthreading"] = info["cores_logicos"] > info["cores_fisicos"]

    # Frequência
    freq = psutil.cpu_freq()
    info["cpu_freq_max_mhz"] = round(freq.max, 0) if freq else 0.0

    # RAM
    mem = psutil.virtual_memory()
    info["ram_total_gb"]      = round(mem.total / (1024 ** 3), 1)
    info["ram_disponivel_gb"] = round(mem.available / (1024 ** 3), 1)

    # Sockets / NUMA
    try:
        import subprocess
        result = subprocess.run(
            ["wmic", "cpu", "get", "NumberOfCores,SocketDesignation", "/format:csv"],
            capture_output=True, text=True, timeout=5
        )
        linhas = [l for l in result.stdout.strip().splitlines() if l.strip() and "Node" not in l]
        info["num_sockets"] = len(linhas) if linhas else 1
    except Exception:
        info["num_sockets"] = 1

    info["multi_socket"] = info["num_sockets"] > 1

    # Nome da CPU
    if _CPUINFO_OK:
        try:
            ci = cpuinfo.get_cpu_info()
            info["cpu_nome"] = ci.get("brand_raw", platform.processor())
            info["cpu_vendor"] = ci.get("vendor_id_raw", "unknown").lower()
        except Exception:
            info["cpu_nome"]   = platform.processor()
            info["cpu_vendor"] = "unknown"
    else:
        info["cpu_nome"]   = platform.processor()
        info["cpu_vendor"] = "unknown"

    info["is_intel"] = "intel" in info["cpu_vendor"] or "genuineintel" in info["cpu_vendor"]
    info["is_amd"]   = "amd"   in info["cpu_vendor"] or "authenticamd"  in info["cpu_vendor"]

    return info


# ---------------------------------------------------------------------------
# Recomendação de configuração
# ---------------------------------------------------------------------------
def recomendar_config(hw: dict, n_meshes: int = 0) -> dict:
    """
    Com base no hardware detectado e no número de meshes do arquivo .fds,
    retorna a configuração recomendada de paralelismo.

    Regras:
      - Sempre usa apenas cores FÍSICOS (HT prejudica o FDS).
      - MPI puro se n_meshes > 1 e n_meshes <= cores_fisicos.
      - HYBRID se n_meshes > 1 e sobram cores por mesh.
      - OPENMP puro se n_meshes <= 1 ou caso default.
      - Em CPUs AMD, prefere OPENMP (driver OpenMP GNU é mais eficiente).
    """
    cf  = hw["cores_fisicos"]
    rec = {}

    if n_meshes > 1 and not hw["is_amd"]:
        if n_meshes <= cf:
            # MPI puro: 1 processo por mesh
            rec["modo"]      = "MPI"
            rec["num_cores"] = n_meshes
            rec["hybrid"]    = {"mpi_procs": n_meshes, "omp_threads": 1}
            rec["motivo"]    = (
                f"{n_meshes} mesh(es) detectada(s) → MPI puro "
                f"({n_meshes} processo(s), 1 thread cada)."
            )
        else:
            # Mais meshes do que cores: HYBRID com distribuição equilibrada
            omp_t = max(1, cf // n_meshes)
            mpi_p = min(n_meshes, cf // omp_t)
            rec["modo"]      = "HYBRID"
            rec["num_cores"] = mpi_p * omp_t
            rec["hybrid"]    = {"mpi_procs": mpi_p, "omp_threads": omp_t}
            rec["motivo"]    = (
                f"{n_meshes} mesh(es), {cf} core(s) físico(s) → "
                f"HYBRID {mpi_p}×MPI × {omp_t}×OMP = {mpi_p*omp_t} core(s)."
            )
    else:
        # OPENMP puro (AMD, mesh única ou padrão)
        rec["modo"]      = "OPENMP"
        rec["num_cores"] = cf
        rec["hybrid"]    = {"mpi_procs": 1, "omp_threads": cf}
        if hw["is_amd"]:
            rec["motivo"] = (
                f"CPU AMD detectada → OPENMP puro com {cf} core(s) físico(s) "
                "(OpenMP GNU mais eficiente em AMD)."
            )
        else:
            rec["motivo"] = (
                f"Mesh única ou padrão → OPENMP puro com {cf} core(s) físico(s)."
            )

    return rec


# ---------------------------------------------------------------------------
# Log do relatório de hardware
# ---------------------------------------------------------------------------
def logar_hardware(hw: dict, rec: dict) -> None:
    sep = "-" * 55
    logger.info(sep)
    logger.info("  DETECÇÃO DE HARDWARE")
    logger.info(sep)
    logger.info(f"  CPU       : {hw['cpu_nome']}")
    logger.info(f"  Sockets   : {hw['num_sockets']}")
    logger.info(f"  Físicos   : {hw['cores_fisicos']} core(s)")
    logger.info(f"  Lógicos   : {hw['cores_logicos']} thread(s)")
    logger.info(f"  Hyperthrd : {'Sim (desabilitado para FDS)' if hw['hyperthreading'] else 'Não'}")
    logger.info(f"  RAM total : {hw['ram_total_gb']} GB")
    logger.info(f"  RAM livre : {hw['ram_disponivel_gb']} GB")
    logger.info(sep)
    logger.info("  CONFIGURAÇÃO RECOMENDADA")
    logger.info(sep)
    logger.info(f"  Modo      : {rec['modo']}")
    logger.info(f"  Núcleos   : {rec['num_cores']}")
    if rec['modo'] in ('MPI', 'HYBRID'):
        logger.info(f"  MPI procs : {rec['hybrid']['mpi_procs']}")
        logger.info(f"  OMP thrd  : {rec['hybrid']['omp_threads']}")
    logger.info(f"  Motivo    : {rec['motivo']}")
    logger.info(sep)
