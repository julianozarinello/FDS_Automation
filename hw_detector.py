# hw_detector.py — Detecção automática de hardware para FDS Automation
import os
import copy
import ctypes
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
# Detecção de P-cores / E-cores (CPUs híbridas Intel 12ª gen+)
# ---------------------------------------------------------------------------
def _analisar_cpu_sets(raw: bytes, tamanho: int, cores_fisicos: int) -> dict | None:
    """Interpreta SYSTEM_CPU_SET_INFORMATION sem colapsar processor groups."""
    eficiencia_por_core: dict[tuple[int, int], int] = {}
    logico_por_core: dict[tuple[int, int], tuple[int, int]] = {}
    grupos: set[int] = set()
    offset = 0
    limite = min(len(raw), tamanho)
    while offset + 19 <= limite:
        entry_size = int.from_bytes(raw[offset:offset + 4], "little")
        if entry_size <= 0 or offset + entry_size > limite:
            break

        tipo = int.from_bytes(raw[offset + 4:offset + 8], "little")
        if tipo == 0:
            grupo = int.from_bytes(raw[offset + 12:offset + 14], "little")
            logical_index = raw[offset + 14]
            core_index = raw[offset + 15]
            efficiency_class = raw[offset + 18]
            chave_core = (grupo, core_index)
            grupos.add(grupo)
            atual = eficiencia_por_core.get(chave_core, -1)
            if efficiency_class > atual:
                eficiencia_por_core[chave_core] = efficiency_class
            logico_por_core.setdefault(chave_core, (grupo, logical_index))
        offset += entry_size

    if not eficiencia_por_core:
        return None

    classes = set(eficiencia_por_core.values())
    multiplos_grupos = len(grupos) > 1
    if len(classes) <= 1:
        # psutil.cpu_affinity recebe indices simples e nao representa de forma
        # portavel (Group,Index). Em maquinas >64 CPUs, deixar None preserva o
        # pinning correto do Windows/Intel MPI em vez de repetir indices 0..63.
        indices = None
        if not multiplos_grupos:
            indices = sorted(indice for _grupo, indice in logico_por_core.values())
        return {
            "is_hybrid": False,
            "p_cores": cores_fisicos,
            "e_cores": 0,
            "logical_indices_efetivos": indices or None,
            "logical_indices_todos": indices or None,
            "processor_groups": len(grupos),
        }

    max_classe = max(classes)
    cores_p = [chave for chave, classe in eficiencia_por_core.items() if classe == max_classe]
    e_cores = len(eficiencia_por_core) - len(cores_p)
    indices_p = None
    indices_todos = None
    if not multiplos_grupos:
        indices_p = sorted(logico_por_core[chave][1] for chave in cores_p)
        indices_todos = sorted(indice for _grupo, indice in logico_por_core.values())
    return {
        "is_hybrid": True,
        "p_cores": len(cores_p),
        "e_cores": e_cores,
        "logical_indices_efetivos": indices_p or None,
        "logical_indices_todos": indices_todos or None,
        "processor_groups": len(grupos),
    }


def _detectar_p_e_cores(cores_fisicos: int) -> dict:
    """
    Usa a API nativa do Windows GetSystemCpuSetInformation (Windows 10 1809+)
    para ler a EfficiencyClass de cada núcleo lógico — a mesma informação que
    o Gerenciador de Tarefas usa para colorir núcleos P (performance) e
    E (eficiência). Maior EfficiencyClass = núcleo mais rápido.

    Em qualquer falha (Windows antigo, Linux/Mac, CPU não-híbrida, erro de
    parsing), assume CPU não-híbrida e devolve todos os cores como P-cores.
    """
    fallback = {
        "is_hybrid": False, "p_cores": cores_fisicos, "e_cores": 0,
        "logical_indices_efetivos": None,
        "logical_indices_todos": None,
    }

    if platform.system() != "Windows":
        return fallback

    try:
        kernel32 = ctypes.windll.kernel32

        # Primeira chamada: descobre o tamanho do buffer necessário.
        tamanho = ctypes.c_ulong(0)
        kernel32.GetSystemCpuSetInformation(None, 0, ctypes.byref(tamanho), None, 0)
        if tamanho.value == 0:
            return fallback

        buffer = ctypes.create_string_buffer(tamanho.value)
        ok = kernel32.GetSystemCpuSetInformation(
            buffer, tamanho.value, ctypes.byref(tamanho), None, 0
        )
        if not ok:
            return fallback

        # Layout de SYSTEM_CPU_SET_INFORMATION (variante CpuSetInformation),
        # offsets absolutos a partir do inicio de cada entrada:
        #   ULONG  Size                  @0  (4 bytes)
        #   DWORD  Type                  @4  (0 = CpuSetInformation)
        #   DWORD  Id                    @8
        #   WORD   Group                 @12
        #   BYTE   LogicalProcessorIndex @14
        #   BYTE   CoreIndex             @15
        #   BYTE   LastLevelCacheIndex   @16
        #   BYTE   NumaNodeIndex         @17
        #   BYTE   EfficiencyClass       @18
        #   ... (flags / tag, não usados aqui)
        analisado = _analisar_cpu_sets(buffer.raw, tamanho.value, cores_fisicos)
        if analisado is None:
            return fallback
        if analisado.get("processor_groups", 1) > 1:
            logger.warning(
                "Windows reportou multiplos processor groups; afinidade OS "
                "manual foi desativada e o pinning fica com Windows/Intel MPI."
            )
        return analisado

    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Coleta de informações de hardware
# ---------------------------------------------------------------------------
# O hardware nao muda durante a execucao, mas detectar_hardware() e chamado
# varias vezes por simulacao (autoconfig, otimizador, afinidade). A deteccao
# de sockets em especial gasta 1-2s por chamada (processo externo). O cache
# devolve uma copia para que chamadas como aplicar_autoconfig possam alterar
# o dict sem contaminar as demais.
_HW_CACHE: dict | None = None


def detectar_hardware() -> dict:
    """
    Retorna um dicionário com informações detalhadas do hardware.
    O resultado é detectado uma única vez por execução (cache) e cada
    chamada recebe uma cópia independente.
    """
    global _HW_CACHE
    if _HW_CACHE is None:
        _HW_CACHE = _detectar_hardware_impl()
    return copy.deepcopy(_HW_CACHE)


def _contar_sockets() -> int:
    """
    Conta sockets de CPU. Tenta wmic (Windows <= 11 23H2) e cai para
    PowerShell/CIM (o wmic foi removido no Windows 11 24H2+).
    """
    import subprocess

    # CREATE_NO_WINDOW: evita piscar janelas de cmd na interface grafica
    sem_janela = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        result = subprocess.run(
            ["wmic", "cpu", "get", "NumberOfCores,SocketDesignation", "/format:csv"],
            capture_output=True, text=True, timeout=5,
            creationflags=sem_janela,
        )
        linhas = [l for l in result.stdout.strip().splitlines() if l.strip() and "Node" not in l]
        if linhas:
            return len(linhas)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Processor).Count"],
            capture_output=True, text=True, timeout=15,
            creationflags=sem_janela,
        )
        valor = result.stdout.strip()
        if valor.isdigit() and int(valor) >= 1:
            return int(valor)
    except Exception:
        pass

    return 1


def _detectar_hardware_impl() -> dict:
    info = {}

    # Cores
    info["cores_logicos"]  = psutil.cpu_count(logical=True)  or 1
    info["cores_fisicos"]  = psutil.cpu_count(logical=False) or 1
    info["hyperthreading"] = info["cores_logicos"] > info["cores_fisicos"]

    # P-cores / E-cores (CPUs híbridas)
    pe = _detectar_p_e_cores(info["cores_fisicos"])
    info["is_hybrid"] = pe["is_hybrid"]
    info["p_cores"]   = pe["p_cores"]
    info["e_cores"]   = pe["e_cores"]
    info["cores_efetivos"] = pe["p_cores"] if pe["is_hybrid"] else info["cores_fisicos"]
    # Indices de processador logico (1 por core fisico, evitando os 2
    # threads de HyperThreading do mesmo core) usados para fixar afinidade
    # de CPU dos processos do FDS via psutil. None se a deteccao falhou.
    info["logical_indices_efetivos"] = pe.get("logical_indices_efetivos")
    # Como acima, mas incluindo tambem os E-cores (1 indice logico por core
    # fisico). Usado quando o usuario ativa hybrid_cpu.usar_e_cores.
    info["logical_indices_todos"] = pe.get("logical_indices_todos")

    # Frequência
    freq = psutil.cpu_freq()
    info["cpu_freq_max_mhz"] = round(freq.max, 0) if freq else 0.0

    # RAM
    mem = psutil.virtual_memory()
    info["ram_total_gb"]      = round(mem.total / (1024 ** 3), 1)
    info["ram_disponivel_gb"] = round(mem.available / (1024 ** 3), 1)

    # Sockets / NUMA
    info["num_sockets"] = _contar_sockets() if platform.system() == "Windows" else 1

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

    # `py-cpuinfo` e opcional e nao e incluido em alguns builds PyInstaller.
    # Nessa situacao, platform.processor() ainda costuma trazer textos como
    # "GenuineIntel" ou "AMD64 Family ...". Usar nome + vendor como fallback
    # mantem a recomendacao Intel/AMD correta mesmo sem a dependencia extra.
    identidade_cpu = f"{info['cpu_vendor']} {info['cpu_nome']}".lower()
    info["is_intel"] = "intel" in identidade_cpu or "genuineintel" in identidade_cpu
    info["is_amd"]   = "amd" in identidade_cpu or "authenticamd" in identidade_cpu

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
      - MPI puro se as meshes conseguem ocupar os cores disponíveis.
      - HYBRID se ha mais de uma mesh e sobram cores por mesh.
      - OPENMP puro se n_meshes <= 1 ou caso default.

    A recomendacao inicial nao presume que a marca da CPU determine o modo
    mais rapido. A documentacao do FDS favorece MPI em casos multi-mesh, e o
    otimizador empirico confirma a escolha no computador real.
    """
    cf = hw.get("cores_efetivos", hw["cores_fisicos"])
    rec = {}

    prefixo_hibrido = ""
    if hw.get("is_hybrid"):
        prefixo_hibrido = (
            f"CPU híbrida: {hw['p_cores']} P-core(s) + {hw['e_cores']} E-core(s) "
            f"detectados — usando apenas P-cores. "
        )

    if n_meshes > 1:
        if n_meshes >= cf:
            # MPI puro: 1 processo por mesh
            rec["modo"]      = "MPI"
            mpi_p = min(n_meshes, cf)
            rec["num_cores"] = mpi_p
            rec["hybrid"]    = {"mpi_procs": mpi_p, "omp_threads": 1}
            rec["motivo"]    = (
                f"{prefixo_hibrido}{n_meshes} mesh(es), {cf} core(s) útil(eis) -> "
                f"MPI puro com {mpi_p} processo(s) e MPI_PROCESS balanceado."
            )
        else:
            # Menos meshes do que cores: HYBRID aproveita os cores restantes
            mpi_p = n_meshes
            omp_t = max(1, cf // mpi_p)
            rec["modo"]      = "HYBRID"
            rec["num_cores"] = mpi_p * omp_t
            rec["hybrid"]    = {"mpi_procs": mpi_p, "omp_threads": omp_t}
            rec["motivo"]    = (
                f"{prefixo_hibrido}{n_meshes} mesh(es), {cf} core(s) útil(eis) -> "
                f"HYBRID {mpi_p}xMPI x {omp_t}xOMP = {mpi_p*omp_t} core(s)."
            )
    else:
        # OPENMP puro (mesh unica ou caso sem meshes detectadas)
        rec["modo"]      = "OPENMP"
        rec["num_cores"] = cf
        rec["hybrid"]    = {"mpi_procs": 1, "omp_threads": cf}
        rec["motivo"] = (
            f"{prefixo_hibrido}Mesh única ou padrão -> OPENMP puro com "
            f"{cf} core(s) útil(eis)."
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
    if hw.get("is_hybrid"):
        logger.info(
            f"  P-cores/E-cores : {hw['p_cores']} / {hw['e_cores']} "
            "(E-cores ignorados por padrão)"
        )
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
