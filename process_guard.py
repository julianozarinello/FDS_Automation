# process_guard.py — evita que mpiexec/hydra_pmi_proxy/fds.exe fiquem
# orfaos rodando em segundo plano (consumindo CPU) se uma simulacao ou
# teste do otimizador for cancelado, ou se o FDS Automation for encerrado
# abruptamente de fora (kill, queda do terminal, etc.).
#
# IMPORTANTE: o Intel MPI (hydra) costuma encerrar o processo "mpiexec"
# logo depois de lancar os ranks via hydra_pmi_proxy. Quando isso
# acontece, `taskkill /PID <pid> /T` falha com "processo nao encontrado"
# porque exige que o PID raiz ainda exista — mesmo que os descendentes
# (hydra_pmi_proxy, fds.exe) continuem vivos e consumindo CPU. Por isso
# usamos Job Objects do Windows, que nao tem essa limitacao: o processo e
# atribuido ao job no momento da criacao e pode ser encerrado a qualquer
# momento via TerminateJobObject, independente do PID raiz ainda existir.
import ctypes
from ctypes import wintypes
import logging
import platform
import subprocess
import time

import psutil

logger = logging.getLogger(__name__)

_NOMES_FDS = ("fds.exe", "fds_openmp.exe")

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100

_KERNEL32_CONFIGURADO = False


def _obter_kernel32():
    """Configura assinaturas Win32 para nao truncar HANDLEs em Python 64-bit."""
    global _KERNEL32_CONFIGURADO
    kernel32 = ctypes.windll.kernel32
    if _KERNEL32_CONFIGURADO:
        return kernel32

    handle = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = handle
    kernel32.SetInformationJobObject.argtypes = [
        handle, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = handle
    kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [handle, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32_CONFIGURADO = True
    return kernel32


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def proteger_processo(processo):
    """
    Cria um Job Object dedicado para este processo (com a flag
    KILL_ON_JOB_CLOSE) e o associa a ele. O Windows propaga
    automaticamente para qualquer processo-filho que ele vier a criar
    (mpiexec -> hydra_pmi_proxy -> fds.exe), mesmo apos o processo
    original sair.

    Retorna o handle do job (usar com `matar_job`/`fechar_job`) ou None
    se a plataforma/API nao estiver disponivel — quem chama deve tratar
    None como "sem protecao extra", sem interromper a simulacao.
    """
    if processo is None or platform.system() != "Windows":
        return None

    try:
        kernel32 = _obter_kernel32()

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None

        handle = kernel32.OpenProcess(
            _PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, processo.pid
        )
        if not handle:
            kernel32.CloseHandle(job)
            return None

        try:
            if not kernel32.AssignProcessToJobObject(job, handle):
                kernel32.CloseHandle(job)
                return None
        finally:
            kernel32.CloseHandle(handle)

        return job
    except Exception:
        logger.debug("Job Object indisponivel; sem protecao extra contra processos orfaos.")
        return None


def matar_job(job) -> None:
    """
    Encerra imediatamente o processo protegido e todos os seus
    descendentes (mesmo que o processo original ja tenha saido sozinho,
    cenario em que `taskkill /T` falha). Use ao cancelar a fila (Ctrl+C)
    ou ao expirar o timeout de um teste do otimizador.
    """
    if job is None:
        return
    try:
        _obter_kernel32().TerminateJobObject(job, 1)
    except Exception:
        logger.debug("Falha ao encerrar o Job Object.")


def fechar_job(job) -> None:
    """Libera o handle do job apos a simulacao/teste terminar normalmente."""
    if job is None:
        return
    try:
        _obter_kernel32().CloseHandle(job)
    except Exception:
        pass


def matar_arvore_processos(pid: int) -> None:
    """
    Fallback via taskkill, usado apenas quando nao ha um Job Object
    disponivel (ex.: `proteger_processo` retornou None). Tem a limitacao
    conhecida de falhar se o PID raiz ja tiver saido — prefira sempre
    `matar_job` quando possivel.
    """
    if platform.system() != "Windows":
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        logger.debug(f"Falha ao encerrar arvore de processos do PID {pid}.")


def aplicar_prioridade_e_afinidade(
    nome_fds: str,
    desde: float,
    cores_logicos: list[int] | None = None,
    prioridade: int | None = None,
) -> int:
    """
    Localiza os processos fds.exe/fds_openmp.exe da simulacao atual
    (criados a partir de `desde`, com `nome_fds` na linha de comando) e
    fixa prioridade/afinidade de CPU diretamente neles via psutil.

    Casamos pelo nome do arquivo .fds na linha de comando — nao pela
    pasta de trabalho (cwd) — porque o Intel MPI/hydra nao garante que
    os ranks (fds.exe) herdem o mesmo cwd do mpiexec. Como a fila roda
    uma simulacao por vez, nome do arquivo + horario recente já evita
    ambiguidade.

    Por que isso e necessario: o Windows NAO propaga a classe de
    prioridade (HIGH_PRIORITY_CLASS) nem afinidade para processos-filho
    criados depois — so o processo imediato (mpiexec) recebe o que
    passamos no Popen. Os ranks MPI reais (fds.exe), que sao quem
    realmente consome CPU, ficam em prioridade normal e sem afinidade
    fixada a menos que ajustemos cada um individualmente aqui.

    `cores_logicos`, quando fornecido (lista de indices de processador
    logico vindos de hw_detector — normalmente os P-cores, 1 thread por
    core fisico), restringe os processos a essas CPUs, mantendo-os fora
    de E-cores e de threads de HyperThreading irmaos. Quando ha varios
    processos FDS (ranks MPI), os cores sao PARTICIONADOS entre eles em
    fatias contiguas — dar a mascara inteira a todos os ranks desfaria o
    pinning compacto do Intel MPI e deixaria os ranks migrando entre
    nucleos, o que degrada o desempenho.

    Retorna quantos processos foram ajustados. Falha silenciosamente
    (best-effort): nunca deve interromper a simulacao.
    """
    nome_fds_lower = nome_fds.lower()

    encontrados = []
    for proc in psutil.process_iter(["name", "create_time", "cmdline"]):
        try:
            info = proc.info
            if info.get("name") not in _NOMES_FDS:
                continue
            if info.get("create_time", 0) < desde - 1.0:
                continue
            cmdline = info.get("cmdline") or []
            if not any(nome_fds_lower in str(arg).lower() for arg in cmdline):
                continue
            encontrados.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # Ordem estavel (criacao, depois PID) para que as fatias de afinidade
    # fiquem contiguas e deterministicas entre os ranks.
    encontrados.sort(key=lambda p: (p.info.get("create_time", 0), p.pid))
    fatias = _particionar_cores(cores_logicos, len(encontrados))

    ajustados = 0
    for i, proc in enumerate(encontrados):
        try:
            if prioridade is not None:
                proc.nice(prioridade)
            if fatias:
                proc.cpu_affinity(fatias[i])
            ajustados += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            logger.debug("Falha ao ajustar prioridade/afinidade de um processo FDS.")
            continue

    return ajustados


def _particionar_cores(cores_logicos: list[int] | None, n_procs: int) -> list[list[int]] | None:
    """
    Divide a lista de cores logicos em n_procs fatias contiguas (as
    primeiras fatias recebem o core extra quando a divisao nao e exata).
    Com 1 processo (OPENMP) devolve a lista inteira. Se houver mais
    processos que cores, todos recebem a lista completa (nao ha como
    dedicar 1 core por rank nesse caso).
    """
    if not cores_logicos or n_procs <= 0:
        return None
    if n_procs == 1:
        return [list(cores_logicos)]
    if n_procs > len(cores_logicos):
        return [list(cores_logicos) for _ in range(n_procs)]

    base, resto = divmod(len(cores_logicos), n_procs)
    fatias = []
    inicio = 0
    for i in range(n_procs):
        tamanho = base + (1 if i < resto else 0)
        fatias.append(list(cores_logicos[inicio:inicio + tamanho]))
        inicio += tamanho
    return fatias


def aplicar_prioridade_e_afinidade_com_espera(
    nome_fds: str,
    desde: float,
    cores_logicos: list[int] | None = None,
    prioridade: int | None = None,
    tentativas: int = 10,
    intervalo_seg: float = 0.5,
    n_esperado: int | None = None,
    cancelar_evento=None,
) -> int:
    """
    Tenta `aplicar_prioridade_e_afinidade` algumas vezes com pequenas
    pausas, ja que os processos fds.exe podem levar um instante para
    aparecer apos o mpiexec ser lancado (sobretudo em modo MPI/HYBRID).

    `n_esperado` (numero de ranks MPI previsto) faz a espera continuar
    ate que todos os ranks tenham aparecido: como a afinidade e
    particionada entre os processos encontrados, aplicar cedo demais
    (com so parte dos ranks vivos) geraria fatias erradas. Reaplicar a
    cada tentativa e barato e corrige a particao conforme os ranks vao
    surgindo.
    """
    ajustados = 0
    for _ in range(max(1, tentativas)):
        if cancelar_evento is not None and cancelar_evento.is_set():
            break
        ajustados = aplicar_prioridade_e_afinidade(nome_fds, desde, cores_logicos, prioridade)
        if ajustados > 0 and (n_esperado is None or ajustados >= n_esperado):
            return ajustados
        if cancelar_evento is not None:
            if cancelar_evento.wait(intervalo_seg):
                break
        else:
            time.sleep(intervalo_seg)
    return ajustados
