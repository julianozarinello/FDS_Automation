# optimizer.py - empirical performance tuner for FDS Automation
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from copy import deepcopy
from typing import Any

import psutil

from fds_reader import contar_meshes, motivos_contagem_mesh_indeterminada
from hw_detector import detectar_hardware
from mesh_splitter import processar_meshes
from process_guard import (
    proteger_processo,
    matar_job,
    fechar_job,
    aplicar_prioridade_e_afinidade_com_espera,
)
from case_control import (
    PularCasoSolicitado,
    consumir_pulo_se_for_caso_atual,
    verificar_pulo,
)

logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 3

# Impede que cada teste do otimizador (fds.exe/mpiexec) abra uma janela
# de cmd na frente da interface grafica. A saida ja e capturada por pipe.
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

_EXTENSOES_SAIDA_FDS = {
    ".bak",
    ".bf",
    ".end",
    ".err",
    ".exe",
    ".dll",
    ".ge1",
    ".iso",
    ".log",
    ".out",
    ".part",
    ".prt5",
    ".q",
    ".restart",
    ".sf",
    ".smv",
    ".s3d",
    ".status",
    ".stop",
}

_DIRETORIOS_IGNORADOS = {
    ".git",
    "__pycache__",
}


def obter_cfg_otimizador(cfg: dict | None) -> dict:
    if not isinstance(cfg, dict):
        return {}
    return cfg.get("otimizador", {}) or {}


def otimizador_ativo(cfg: dict | None) -> bool:
    return bool(obter_cfg_otimizador(cfg).get("ativo", False))


def _safe_int(valor: Any, padrao: int) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return padrao


def _safe_float(valor: Any, padrao: float) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return padrao


def _normalizar_modos(opt_cfg: dict) -> list[str]:
    modos = opt_cfg.get("testar_modos", ["SERIAL", "OPENMP", "MPI", "HYBRID"])
    if not isinstance(modos, list):
        modos = ["SERIAL", "OPENMP", "MPI", "HYBRID"]

    vistos = set()
    normalizados = []
    for modo in modos:
        modo = str(modo).upper()
        if modo not in {"SERIAL", "OPENMP", "MPI", "HYBRID"}:
            continue
        if modo in vistos:
            continue
        vistos.add(modo)
        normalizados.append(modo)
    return normalizados or ["SERIAL", "OPENMP", "MPI", "HYBRID"]


def _peso_atual(cfg: dict) -> float:
    return _safe_float((cfg.get("mesh_splitter") or {}).get("peso_espacial", 0.15), 0.15)


def _pesos_para_testar(cfg: dict, opt_cfg: dict) -> list[float]:
    # A ordem de &MESH agora e preservada por seguranca numerica. O antigo
    # peso espacial so tinha efeito ao mover meshes e, portanto, testar varias
    # copias dele desperdicava candidatos/tempo sem mudar o arquivo final.
    return [_peso_atual(cfg)]


def _cfg_candidato(
    cfg: dict,
    modo: str,
    num_cores: int,
    mpi_procs: int,
    omp_threads: int,
    peso_espacial: float | None,
    etiqueta: str,
) -> dict:
    cand = deepcopy(cfg)
    cand["modo"] = modo
    cand["num_cores"] = max(1, int(num_cores))
    cand["hybrid"] = {
        "mpi_procs": max(1, int(mpi_procs)),
        "omp_threads": max(1, int(omp_threads)),
    }

    if peso_espacial is not None:
        ms_cfg = deepcopy(cand.get("mesh_splitter") or {})
        ms_cfg["peso_espacial"] = float(peso_espacial)
        ms_cfg["pular_se_otimizado"] = False
        cand["mesh_splitter"] = ms_cfg

    cand["_optimizer_label"] = etiqueta
    return cand


def _snapshot_cfg(cand: dict) -> dict:
    ms_cfg = cand.get("mesh_splitter") or {}
    return {
        "modo": str(cand.get("modo", "OPENMP")).upper(),
        "num_cores": _safe_int(cand.get("num_cores"), 1),
        "hybrid": {
            "mpi_procs": _safe_int((cand.get("hybrid") or {}).get("mpi_procs"), 1),
            "omp_threads": _safe_int((cand.get("hybrid") or {}).get("omp_threads"), 1),
        },
        "mesh_splitter": {
            "peso_espacial": _safe_float(ms_cfg.get("peso_espacial"), 0.15),
            "pular_se_otimizado": bool(ms_cfg.get("pular_se_otimizado", True)),
        },
    }


def _aplicar_snapshot(cfg: dict, snapshot: dict) -> dict:
    novo = deepcopy(cfg)
    novo["modo"] = str(snapshot.get("modo", novo.get("modo", "OPENMP"))).upper()
    novo["num_cores"] = _safe_int(snapshot.get("num_cores"), _safe_int(novo.get("num_cores"), 1))

    hybrid = snapshot.get("hybrid") or {}
    novo["hybrid"] = {
        "mpi_procs": _safe_int(hybrid.get("mpi_procs"), 1),
        "omp_threads": _safe_int(hybrid.get("omp_threads"), 1),
    }

    ms_snapshot = snapshot.get("mesh_splitter") or {}
    ms_cfg = deepcopy(novo.get("mesh_splitter") or {})
    if "peso_espacial" in ms_snapshot:
        ms_cfg["peso_espacial"] = _safe_float(ms_snapshot.get("peso_espacial"), 0.15)
    if novo["modo"] in {"MPI", "HYBRID"}:
        ms_cfg["pular_se_otimizado"] = bool(ms_snapshot.get("pular_se_otimizado", False))
    novo["mesh_splitter"] = ms_cfg
    return novo


def _chave_candidato(cand: dict) -> tuple:
    snap = _snapshot_cfg(cand)
    if snap["modo"] in {"SERIAL", "OPENMP"}:
        return (
            snap["modo"],
            snap["num_cores"],
            0,
            0,
            0.0,
        )
    return (
        snap["modo"],
        snap["num_cores"],
        snap["hybrid"]["mpi_procs"],
        snap["hybrid"]["omp_threads"],
        round(snap["mesh_splitter"]["peso_espacial"], 4),
    )


def descrever_candidato(cand: dict) -> str:
    modo = str(cand.get("modo", "OPENMP")).upper()
    hybrid = cand.get("hybrid") or {}
    peso = _peso_atual(cand)

    if modo == "OPENMP":
        return f"OPENMP | {cand.get('num_cores')} thread(s)"
    if modo == "MPI":
        return f"MPI | {cand.get('num_cores')} processo(s) | peso {peso:g}"
    if modo == "HYBRID":
        mpi = hybrid.get("mpi_procs")
        omp = hybrid.get("omp_threads")
        return f"HYBRID | {mpi} MPI x {omp} OMP | peso {peso:g}"
    mkl = _safe_int(cand.get("num_cores"), 1)
    return "SERIAL" if mkl <= 1 else f"SERIAL | MKL ate {mkl} thread(s)"


def _adicionar_valor(lista: list[int], valor: int, limite: int) -> None:
    valor = max(1, min(int(valor), limite))
    if valor not in lista:
        lista.append(valor)


def _valores_openmp(cfg: dict, cores: int, opt_cfg: dict) -> list[int]:
    valores: list[int] = []
    if str(cfg.get("modo", "")).upper() == "OPENMP":
        _adicionar_valor(valores, _safe_int(cfg.get("num_cores"), cores), cores)

    # A documentacao do FDS relata retorno decrescente apos ~4 threads.
    # Testar 2/4 e o total custa pouco e evita assumir que "mais = melhor".
    _adicionar_valor(valores, min(4, cores), cores)
    _adicionar_valor(valores, min(2, cores), cores)
    _adicionar_valor(valores, 1, cores)
    _adicionar_valor(valores, cores, cores)
    if bool(opt_cfg.get("testar_threads_openmp", False)) and cores > 2:
        _adicionar_valor(valores, round(cores * 0.75), cores)
        _adicionar_valor(valores, max(1, cores // 2), cores)
    return valores


def _valores_mpi(
    cfg: dict, cores: int, n_meshes: int, permitir_divisao: bool = False,
) -> list[int]:
    limite = cores if permitir_divisao else min(cores, max(1, n_meshes))
    limite = max(1, limite)
    valores: list[int] = []

    if str(cfg.get("modo", "")).upper() == "MPI":
        _adicionar_valor(valores, _safe_int(cfg.get("num_cores"), limite), limite)

    for fator in (1.0, 0.80, 0.60, 0.50, 0.25):
        _adicionar_valor(valores, round(limite * fator), limite)

    return valores


def _pares_hybrid(
    cfg: dict, cores: int, n_meshes: int, permitir_divisao: bool = False,
) -> list[tuple[int, int]]:
    limite_mpi = cores if permitir_divisao else min(cores, max(1, n_meshes))
    limite_mpi = max(1, limite_mpi)
    pares: list[tuple[int, int]] = []

    def adicionar(mpi: int, omp: int | None = None) -> None:
        mpi = max(1, min(int(mpi), limite_mpi))
        if omp is None:
            omp = max(1, cores // mpi)
        omp = max(1, min(int(omp), cores))
        if mpi * omp > cores:
            omp = max(1, cores // mpi)
        par = (mpi, omp)
        if par not in pares:
            pares.append(par)

    if str(cfg.get("modo", "")).upper() == "HYBRID":
        hybrid = cfg.get("hybrid") or {}
        adicionar(
            _safe_int(hybrid.get("mpi_procs"), min(limite_mpi, max(1, cores // 2))),
            _safe_int(hybrid.get("omp_threads"), 1),
        )

    adicionar(min(limite_mpi, max(1, cores // 2)), 2)
    adicionar(min(limite_mpi, max(1, cores // 4)), 4)
    adicionar(min(limite_mpi, max(1, cores // 5)), 5)
    adicionar(min(limite_mpi, max(1, round(cores ** 0.5))))
    return pares


def _ordenar_com_peso_principal(valores: list[int] | list[tuple[int, int]], pesos: list[float]) -> list[tuple[Any, float]]:
    if not pesos:
        pesos = [0.15]

    combinados: list[tuple[Any, float]] = []
    if not valores:
        return combinados

    peso_principal = pesos[0]
    valor_principal = valores[0]
    usados: set[tuple[str, float]] = set()

    def adicionar(valor: Any, peso: float) -> None:
        chave = (repr(valor), round(float(peso), 6))
        if chave in usados:
            return
        usados.add(chave)
        combinados.append((valor, peso))

    adicionar(valor_principal, peso_principal)
    for idx, valor in enumerate(valores[1:], start=1):
        adicionar(valor, peso_principal)
        if idx < len(pesos):
            adicionar(valor_principal, pesos[idx])

    for peso in pesos[1:]:
        adicionar(valor_principal, peso)
        for valor in valores[1:]:
            adicionar(valor, peso)
    return combinados


def gerar_candidatos_otimizador(cfg: dict, hw: dict, n_meshes: int) -> list[dict]:
    opt_cfg = obter_cfg_otimizador(cfg)
    max_tentativas = max(1, _safe_int(opt_cfg.get("max_tentativas"), 12))
    modos = _normalizar_modos(opt_cfg)
    cores = max(1, _safe_int(hw.get("cores_efetivos", hw.get("cores_fisicos")), 1))
    pesos = _pesos_para_testar(cfg, opt_cfg)
    ms_cfg = cfg.get("mesh_splitter") or {}
    permitir_divisao = bool(
        ms_cfg.get("ativo", False) and ms_cfg.get("permitir_divisao", False)
    )

    candidatos: list[dict] = []
    vistos: set[tuple] = set()

    def adicionar(cand: dict) -> None:
        chave = _chave_candidato(cand)
        if chave in vistos:
            return
        vistos.add(chave)
        candidatos.append(cand)

    atual = deepcopy(cfg)
    atual["_optimizer_label"] = "config atual"
    adicionar(atual)

    grupos: dict[str, list[dict]] = {modo: [] for modo in modos}

    if "SERIAL" in grupos:
        grupos["SERIAL"].append(
            _cfg_candidato(cfg, "SERIAL", 1, 1, 1, None, "serial")
        )
        try:
            mkl_forcado = int(
                (cfg.get("performance") or {}).get("mkl_threads", 0) or 0
            )
        except (TypeError, ValueError):
            mkl_forcado = 0
        if cores > 1 and mkl_forcado <= 0:
            grupos["SERIAL"].append(
                _cfg_candidato(
                    cfg, "SERIAL", cores, 1, 1, None, "serial-mkl"
                )
            )

    if "OPENMP" in grupos:
        for threads in _valores_openmp(cfg, cores, opt_cfg):
            grupos["OPENMP"].append(
                _cfg_candidato(cfg, "OPENMP", threads, 1, threads, None, "openmp")
            )

    if "MPI" in grupos and (n_meshes > 1 or permitir_divisao):
        for mpi, peso in _ordenar_com_peso_principal(
            _valores_mpi(cfg, cores, n_meshes, permitir_divisao), pesos
        ):
            grupos["MPI"].append(
                _cfg_candidato(cfg, "MPI", mpi, mpi, 1, peso, "mpi")
            )

    if "HYBRID" in grupos and (n_meshes > 1 or permitir_divisao) and cores >= 2:
        for (mpi, omp), peso in _ordenar_com_peso_principal(
            _pares_hybrid(cfg, cores, n_meshes, permitir_divisao), pesos
        ):
            grupos["HYBRID"].append(
                _cfg_candidato(cfg, "HYBRID", mpi * omp, mpi, omp, peso, "hybrid")
            )

    indices = {modo: 0 for modo in modos}
    while len(candidatos) < max_tentativas:
        avancou = False
        for modo in modos:
            grupo = grupos.get(modo, [])
            while indices[modo] < len(grupo):
                cand = grupo[indices[modo]]
                indices[modo] += 1
                antes = len(candidatos)
                adicionar(cand)
                if len(candidatos) > antes:
                    avancou = True
                    break
            if len(candidatos) >= max_tentativas:
                break
        if not avancou:
            break

    return candidatos


def _hash_arquivo(caminho: str) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def _mtime_arquivo(caminho: str) -> int:
    try:
        return int(os.path.getmtime(caminho))
    except OSError:
        return 0


def _fingerprint_executavel(caminho: str) -> dict:
    try:
        stat = os.stat(caminho)
        return {"mtime_ns": stat.st_mtime_ns, "tamanho": stat.st_size}
    except OSError:
        return {"mtime_ns": 0, "tamanho": 0}


def _caminho_cache(cfg: dict, arquivo_fds: str) -> str:
    opt_cfg = obter_cfg_otimizador(cfg)
    if opt_cfg.get("cache_arquivo"):
        return str(opt_cfg["cache_arquivo"])

    pasta_base = str(cfg.get("pasta_simulacoes") or "")
    if not pasta_base or not os.path.isdir(pasta_base):
        pasta_base = os.path.dirname(arquivo_fds)
    return os.path.join(pasta_base, ".fds_optimizer_cache.json")


def _payload_chave_cache(cfg: dict, arquivo_fds: str, hw: dict, n_meshes: int) -> dict:
    opt_cfg = obter_cfg_otimizador(cfg)
    diretorio_fds = os.path.abspath(str(cfg.get("diretorio_fds", "")))
    ms_cfg = cfg.get("mesh_splitter") or {}
    perf_cfg = cfg.get("performance") or {}
    hybrid_cfg = cfg.get("hybrid") or {}
    caminho_mpiexec = os.path.join(diretorio_fds, "mpi", "mpiexec.exe")
    if not os.path.exists(caminho_mpiexec):
        caminho_mpiexec = os.path.join(diretorio_fds, "mpiexec.exe")
    return {
        "schema": CACHE_SCHEMA_VERSION,
        "arquivo_hash": _hash_arquivo(arquivo_fds),
        "arquivo_nome": os.path.basename(arquivo_fds),
        "n_meshes": n_meshes,
        "hardware": {
            "cpu_nome": hw.get("cpu_nome"),
            "cores_fisicos": hw.get("cores_fisicos"),
            "cores_logicos": hw.get("cores_logicos"),
            "cores_efetivos": hw.get("cores_efetivos"),
            "is_hybrid": hw.get("is_hybrid"),
            "p_cores": hw.get("p_cores"),
            "e_cores": hw.get("e_cores"),
            "ram_total_gb": hw.get("ram_total_gb"),
        },
        "fds": {
            "diretorio": os.path.normcase(diretorio_fds),
            "fds": _fingerprint_executavel(os.path.join(diretorio_fds, "fds.exe")),
            "openmp": _fingerprint_executavel(
                os.path.join(diretorio_fds, "fds_openmp.exe")
            ),
            "mpiexec": _fingerprint_executavel(caminho_mpiexec),
        },
        "config_base": {
            "auto_config": bool(cfg.get("auto_config", False)),
            "modo": str(cfg.get("modo", "OPENMP")).upper(),
            "num_cores": _safe_int(cfg.get("num_cores"), 1),
            "hybrid": {
                "mpi_procs": _safe_int(hybrid_cfg.get("mpi_procs"), 1),
                "omp_threads": _safe_int(hybrid_cfg.get("omp_threads"), 1),
            },
            "usar_e_cores": bool(
                (cfg.get("hybrid_cpu") or {}).get("usar_e_cores", False)
            ),
        },
        "mesh_splitter": {
            "ativo": bool(ms_cfg.get("ativo", False)),
            "permitir_divisao": bool(ms_cfg.get("permitir_divisao", False)),
            "margem_pct": _safe_float(ms_cfg.get("margem_pct"), 10.0),
            "max_meshes_por_core": _safe_int(ms_cfg.get("max_meshes_por_core"), 4),
            "peso_espacial": _peso_atual(cfg),
        },
        "performance": {
            chave: perf_cfg.get(chave)
            for chave in (
                "kmp_affinity", "omp_spin", "numa_opt", "fixar_afinidade_os",
                "omp_stacksize", "mkl_threads", "mpi_shm",
            )
        },
        "otimizador": {
            "modos": _normalizar_modos(opt_cfg),
            "max_tentativas": _safe_int(opt_cfg.get("max_tentativas"), 12),
            "tempo_teste_seg": _safe_float(opt_cfg.get("tempo_teste_seg"), 60.0),
            "pesos": _pesos_para_testar(cfg, opt_cfg),
            "testar_threads_openmp": bool(opt_cfg.get("testar_threads_openmp", False)),
            "melhoria_min_pct": _safe_float(opt_cfg.get("melhoria_min_pct"), 3.0),
            "copiar_arquivos_auxiliares": bool(
                opt_cfg.get("copiar_arquivos_auxiliares", True)
            ),
            "copiar_arquivos_ate_mb": _safe_float(
                opt_cfg.get("copiar_arquivos_ate_mb"), 250.0
            ),
        },
    }


def _chave_cache(cfg: dict, arquivo_fds: str, hw: dict, n_meshes: int) -> str:
    payload = _payload_chave_cache(cfg, arquivo_fds, hw, n_meshes)
    bruto = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(bruto).hexdigest()


def _ler_cache(caminho: str) -> dict:
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"schema": CACHE_SCHEMA_VERSION, "entries": {}}

    if not isinstance(dados, dict):
        return {"schema": CACHE_SCHEMA_VERSION, "entries": {}}
    if dados.get("schema") != CACHE_SCHEMA_VERSION:
        return {"schema": CACHE_SCHEMA_VERSION, "entries": {}}
    if not isinstance(dados.get("entries"), dict):
        dados["entries"] = {}
    dados["schema"] = CACHE_SCHEMA_VERSION
    return dados


def _salvar_cache(caminho: str, dados: dict, max_entradas: int) -> None:
    entries = dados.setdefault("entries", {})
    if len(entries) > max_entradas:
        ordenados = sorted(entries.items(), key=lambda item: item[1].get("updated_at", 0))
        for chave, _ in ordenados[: len(entries) - max_entradas]:
            entries.pop(chave, None)

    pasta = os.path.dirname(os.path.abspath(caminho))
    os.makedirs(pasta, exist_ok=True)
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=pasta,
            prefix=os.path.basename(caminho) + ".", suffix=".tmp",
        ) as f:
            temporario = f.name
            json.dump(dados, f, indent=2, sort_keys=True, ensure_ascii=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporario, caminho)
        temporario = None
    finally:
        if temporario:
            try:
                os.remove(temporario)
            except OSError:
                pass


def _construir_comando(cfg: dict, diretorio_fds: str, nome_fds: str) -> list[str]:
    modo = str(cfg.get("modo", "OPENMP")).upper()
    fds_openmp = os.path.join(diretorio_fds, "fds_openmp.exe")
    fds_exe = os.path.join(diretorio_fds, "fds.exe")
    mpiexec = os.path.join(diretorio_fds, "mpi", "mpiexec.exe")
    if not os.path.exists(mpiexec):
        mpiexec = os.path.join(diretorio_fds, "mpiexec.exe")

    if modo == "OPENMP":
        return [fds_openmp, nome_fds]

    if modo == "MPI":
        n = _safe_int(cfg.get("num_cores"), 1)
        return [
            mpiexec,
            "-n",
            str(n),
            "-genv",
            "I_MPI_PIN",
            "1",
            "-genv",
            "I_MPI_PIN_ORDER",
            "compact",
            "-bind-to",
            "core",
            fds_exe,
            nome_fds,
        ]

    if modo == "HYBRID":
        hybrid = cfg.get("hybrid") or {}
        mpi_procs = _safe_int(hybrid.get("mpi_procs"), 1)
        omp_threads = _safe_int(hybrid.get("omp_threads"), 1)
        return [
            mpiexec,
            "-n",
            str(mpi_procs),
            "-genv",
            "I_MPI_PIN",
            "1",
            "-genv",
            "I_MPI_PIN_DOMAIN",
            f"omp:{omp_threads}",
            "-genv",
            "I_MPI_PIN_ORDER",
            "compact",
            fds_openmp,
            nome_fds,
        ]

    return [fds_exe, nome_fds]


def _montar_env(cfg: dict) -> dict:
    env = os.environ.copy()
    modo = str(cfg.get("modo", "OPENMP")).upper()
    hybrid = cfg.get("hybrid") or {}

    if modo == "OPENMP":
        env["OMP_NUM_THREADS"] = str(_safe_int(cfg.get("num_cores"), 1))
    elif modo == "HYBRID":
        env["OMP_NUM_THREADS"] = str(_safe_int(hybrid.get("omp_threads"), 1))
    else:
        env["OMP_NUM_THREADS"] = "1"

    perf = cfg.get("performance") or {}
    if perf.get("kmp_affinity", True):
        env["KMP_AFFINITY"] = "granularity=fine,compact,1,0"
        env["OMP_PROC_BIND"] = "close"
    if perf.get("omp_spin", True):
        env["GOMP_SPINCOUNT"] = "10000000"
        env["OMP_WAIT_POLICY"] = "active"
        env["KMP_BLOCKTIME"] = "200"
    if perf.get("numa_opt", True):
        env["OMP_PLACES"] = "cores"

    # Espelha aplicar_variaveis_performance (main.py): os testes precisam
    # rodar nas mesmas condicoes da simulacao real para o score ser valido.
    env["OMP_STACKSIZE"] = str(perf.get("omp_stacksize", "16M"))

    try:
        mkl_cfg = int(perf.get("mkl_threads", 0) or 0)
    except (TypeError, ValueError):
        mkl_cfg = 0
    if mkl_cfg > 0:
        mkl_threads = mkl_cfg
    elif modo == "MPI":
        mkl_threads = 1
    elif modo == "HYBRID":
        mkl_threads = _safe_int(hybrid.get("omp_threads"), 1)
    else:
        mkl_threads = _safe_int(cfg.get("num_cores"), 1)
    env["MKL_NUM_THREADS"] = str(max(1, mkl_threads))
    env["MKL_DYNAMIC"] = "FALSE"
    env["OMP_DYNAMIC"] = "FALSE"
    env["OMP_DISPLAY_ENV"] = "FALSE"

    if modo in {"MPI", "HYBRID"}:
        env["I_MPI_PIN"] = "1"
        env["I_MPI_PIN_ORDER"] = "compact"
        if perf.get("mpi_shm", True):
            env["I_MPI_FABRICS"] = "shm"

    diretorio_fds = str(cfg.get("diretorio_fds", ""))
    mpi_dir = os.path.join(diretorio_fds, "mpi")
    env["PATH"] = os.pathsep.join([diretorio_fds, mpi_dir, env.get("PATH", "")])
    return env


def _deve_copiar_auxiliar(origem: str, arquivo_fds: str, opt_cfg: dict) -> bool:
    if os.path.abspath(origem) == os.path.abspath(arquivo_fds):
        return True

    ext = os.path.splitext(origem)[1].lower()
    if ext in _EXTENSOES_SAIDA_FDS:
        return False

    limite_mb = _safe_float(opt_cfg.get("copiar_arquivos_ate_mb"), 250.0)
    limite_bytes = int(max(1.0, limite_mb) * 1024 * 1024)
    try:
        if os.path.getsize(origem) > limite_bytes:
            return False
    except OSError:
        return False
    return True


def _preparar_pasta_benchmark(arquivo_fds: str, raiz_tmp: str, opt_cfg: dict) -> tuple[str, str]:
    origem_dir = os.path.dirname(arquivo_fds)
    destino_dir = os.path.join(raiz_tmp, "case")
    os.makedirs(destino_dir, exist_ok=True)

    copiar_auxiliares = bool(opt_cfg.get("copiar_arquivos_auxiliares", True))
    if not copiar_auxiliares:
        destino_fds = os.path.join(destino_dir, os.path.basename(arquivo_fds))
        shutil.copy2(arquivo_fds, destino_fds)
        return destino_fds, destino_dir

    for raiz, dirs, arquivos in os.walk(origem_dir):
        dirs[:] = [d for d in dirs if d not in _DIRETORIOS_IGNORADOS]
        rel_raiz = os.path.relpath(raiz, origem_dir)
        if rel_raiz == ".":
            rel_raiz = ""
        destino_raiz = os.path.join(destino_dir, rel_raiz)
        os.makedirs(destino_raiz, exist_ok=True)

        for nome in arquivos:
            origem = os.path.join(raiz, nome)
            if not _deve_copiar_auxiliar(origem, arquivo_fds, opt_cfg):
                continue
            destino = os.path.join(destino_raiz, nome)
            shutil.copy2(origem, destino)

    destino_fds = os.path.join(destino_dir, os.path.relpath(arquivo_fds, origem_dir))
    if not os.path.exists(destino_fds):
        destino_fds = os.path.join(destino_dir, os.path.basename(arquivo_fds))
        shutil.copy2(arquivo_fds, destino_fds)
    return destino_fds, destino_dir


def _encerrar_processo_arvore(processo: subprocess.Popen, job=None) -> None:
    # O Intel MPI/hydra costuma encerrar o "mpiexec" logo apos lancar os
    # ranks via hydra_pmi_proxy. taskkill /T falha nesse caso (exige que
    # o PID raiz ainda exista), entao usamos o Job Object criado por
    # proteger_processo() — ele mata a arvore inteira independente disso.
    if job is not None:
        matar_job(job)
        try:
            processo.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(processo.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        return

    if processo.poll() is None:
        processo.terminate()
        try:
            processo.wait(timeout=10)
        except subprocess.TimeoutExpired:
            processo.kill()


def _extrair_time_step(linha: str) -> tuple[str, str] | None:
    match = _RE_FDS_TIME_STEP.search(linha)
    if not match:
        return None
    return match.group(1), match.group(2)


def _linha_indica_erro_fds(linha: str) -> bool:
    linha_upper = linha.upper()
    return bool(_RE_FDS_ERROR.match(linha)) or any(
        frase in linha_upper for frase in FDS_FATAL_PHRASES
    )


def _score_saida(
    saida: str,
    elapsed: float,
    returncode: int | None,
    timeout: bool,
    amostras: list[tuple[float, float]] | None = None,
) -> tuple[float, str]:
    if any(_linha_indica_erro_fds(linha) for linha in saida.splitlines()):
        return 0.0, "erro FDS"

    if returncode not in (0, None) and not timeout:
        return 0.0, f"encerrou com codigo {returncode}"

    # Duas ou mais amostras permitem medir apenas o avanco depois da partida,
    # sem distorcer casos com T_BEGIN/restart diferente de zero.
    if amostras and len(amostras) >= 2:
        t0, sim0 = amostras[0]
        t1, sim1 = amostras[-1]
        if t1 > t0 and sim1 > sim0:
            score = (sim1 - sim0) / (t1 - t0)
            return score, f"avanco {sim1 - sim0:g}s em {t1 - t0:.1f}s"

    ultimo = None
    for linha in saida.splitlines():
        progresso = _extrair_time_step(linha)
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


def _aplicar_mesh_splitter_benchmark(cand: dict, tmp_fds: str) -> None:
    modo = str(cand.get("modo", "OPENMP")).upper()
    ms_cfg = cand.get("mesh_splitter") or {}
    if modo not in {"MPI", "HYBRID"} or not ms_cfg.get("ativo", False):
        return

    if modo == "HYBRID":
        n_cores = _safe_int((cand.get("hybrid") or {}).get("mpi_procs"), 1)
    else:
        n_cores = _safe_int(cand.get("num_cores"), 1)

    processar_meshes(
        tmp_fds,
        n_cores,
        _safe_float(ms_cfg.get("margem_pct"), 10.0),
        False,
        False,
        _safe_float(ms_cfg.get("peso_espacial"), 0.15),
        max(1, _safe_int(ms_cfg.get("max_meshes_por_core"), 4)),
        bool(ms_cfg.get("permitir_divisao", False)),
    )


def executar_benchmark_candidato(
    cand: dict,
    arquivo_fds: str,
    timeout_seg: float,
    opt_cfg: dict,
) -> dict:
    nome_fds = os.path.basename(arquivo_fds)
    verificar_pulo(cand)

    # Processos MPI podem liberar o arquivo .out alguns milissegundos depois
    # do PID raiz. A limpeza do temporario nao deve substituir um resultado
    # (especialmente a excecao de cancelamento) por WinError 32.
    with tempfile.TemporaryDirectory(
        prefix="fds_opt_", ignore_cleanup_errors=True
    ) as tmp_dir:
        tmp_fds, pasta = _preparar_pasta_benchmark(arquivo_fds, tmp_dir, opt_cfg)
        _aplicar_mesh_splitter_benchmark(cand, tmp_fds)

        comando = _construir_comando(cand, str(cand["diretorio_fds"]), os.path.basename(tmp_fds))
        env = _montar_env(cand)

        inicio = time.time()
        creationflags = CREATE_NO_WINDOW
        if hasattr(subprocess, "HIGH_PRIORITY_CLASS"):
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
            env=env,
            creationflags=creationflags,
        )
        job = proteger_processo(processo)
        cancelado = threading.Event()
        encerrar_vigia = threading.Event()

        def _vigiar_cancelamento() -> None:
            while not encerrar_vigia.wait(0.20):
                if consumir_pulo_se_for_caso_atual(cand):
                    cancelado.set()
                    logger.warning(
                        f"Interrompendo o teste de desempenho de {nome_fds}; "
                        "a fila seguira para o proximo caso.",
                        extra={"console_visible": True},
                    )
                    _encerrar_processo_arvore(processo, job)
                    return

        threading.Thread(
            target=_vigiar_cancelamento,
            name="cancelar-benchmark",
            daemon=True,
        ).start()

        # Espelha a afinidade/prioridade aplicada na simulacao real sem atrasar
        # a leitura do stdout. A deteccao de hardware e cacheada.
        if (cand.get("performance") or {}).get("fixar_afinidade_os", True):
            hw_afinidade = detectar_hardware()
            usar_e = bool((cand.get("hybrid_cpu") or {}).get("usar_e_cores", False))
            indices = hw_afinidade.get(
                "logical_indices_todos" if usar_e else "logical_indices_efetivos"
            )
            modo = str(cand.get("modo", "OPENMP")).upper()
            if modo == "HYBRID":
                n_ranks = _safe_int((cand.get("hybrid") or {}).get("mpi_procs"), 1)
            elif modo == "MPI":
                n_ranks = _safe_int(cand.get("num_cores"), 1)
            else:
                n_ranks = 1
            threading.Thread(
                target=aplicar_prioridade_e_afinidade_com_espera,
                args=(nome_fds, inicio, indices, psutil.HIGH_PRIORITY_CLASS),
                kwargs={"n_esperado": n_ranks, "cancelar_evento": cancelado},
                daemon=True,
            ).start()

        # O candidato roda em segundo plano por ate timeout_seg. Sem ler o
        # stdout aos poucos e logar o progresso, o console fica em silencio
        # total durante esse tempo (ate 45s por candidato, varios candidatos
        # na fila), o que parece uma simulacao travada/sem rodar.
        timeout_atingido = {"valor": False}

        def _ao_expirar() -> None:
            timeout_atingido["valor"] = True
            _encerrar_processo_arvore(processo, job)

        timer = threading.Timer(timeout_seg, _ao_expirar)
        timer.daemon = True
        timer.start()

        linhas: list[str] = []
        amostras: list[tuple[float, float]] = []
        ultimo_log = inicio
        try:
            try:
                if processo.stdout is not None:
                    for linha in processo.stdout:
                        linha = linha.rstrip()
                        if not linha:
                            continue
                        linhas.append(linha)

                        progresso = _extrair_time_step(linha)
                        agora = time.time()
                        if progresso:
                            try:
                                amostras.append((agora, float(progresso[1])))
                            except ValueError:
                                pass
                        if progresso and agora - ultimo_log >= 5.0:
                            step, tempo_simulado = progresso
                            logger.info(
                                f"  Testando: passo {step}, {tempo_simulado}s simulados "
                                f"({agora - inicio:.0f}s/{timeout_seg:g}s)",
                                extra={"console_visible": True},
                            )
                            ultimo_log = agora
                processo.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _encerrar_processo_arvore(processo, job)
                try:
                    processo.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        except KeyboardInterrupt:
            _encerrar_processo_arvore(processo, job)
            raise
        finally:
            encerrar_vigia.set()
            timer.cancel()
            fechar_job(job)

        if cancelado.is_set():
            raise PularCasoSolicitado()

        timeout = timeout_atingido["valor"]
        saida = "\n".join(linhas)
        elapsed = time.time() - inicio
        returncode = processo.returncode

    score, detalhe = _score_saida(
        saida or "", elapsed, returncode, timeout, amostras=amostras
    )
    return {
        "score": score,
        "detalhe": detalhe,
        "elapsed": elapsed,
        "returncode": returncode,
        "timeout": timeout,
        "descricao": descrever_candidato(cand),
        "snapshot": _snapshot_cfg(cand),
    }


def aplicar_otimizador_desempenho(cfg: dict, arquivo_fds: str) -> dict:
    try:
        return _aplicar_otimizador_desempenho_impl(cfg, arquivo_fds)
    except (KeyboardInterrupt, PularCasoSolicitado):
        raise
    except Exception as exc:
        logger.error(
            f"Otimizador encerrou com erro inesperado: {exc}. "
            "Usando configuracao atual do config.yaml.",
            exc_info=True,
        )
        return cfg


def _aplicar_otimizador_desempenho_impl(cfg: dict, arquivo_fds: str) -> dict:
    opt_cfg = obter_cfg_otimizador(cfg)
    if not opt_cfg.get("ativo", False):
        return cfg

    diretorio_fds = str(cfg.get("diretorio_fds", ""))
    if not os.path.exists(diretorio_fds):
        logger.warning("Otimizador ignorado: diretorio_fds nao encontrado.")
        return cfg

    motivos = motivos_contagem_mesh_indeterminada(arquivo_fds)
    if motivos:
        logger.warning(
            "Otimizador automatico ignorado por %s: a quantidade completa "
            "de meshes so e conhecida dentro do FDS. Mantendo a configuracao "
            "conservadora/manual do caso.",
            "/".join(motivos),
            extra={"console_visible": True},
        )
        return cfg

    timeout_seg = max(5.0, _safe_float(opt_cfg.get("tempo_teste_seg"), 60.0))
    melhoria_min_pct = max(0.0, _safe_float(opt_cfg.get("melhoria_min_pct"), 3.0))
    usar_cache = bool(opt_cfg.get("usar_cache", True))
    max_cache = max(1, _safe_int(opt_cfg.get("cache_max_entradas"), 100))

    n_meshes = contar_meshes(arquivo_fds)
    hw = detectar_hardware()
    candidatos = gerar_candidatos_otimizador(cfg, hw, n_meshes)
    if len(candidatos) <= 1:
        logger.info("Otimizador ignorado: apenas um candidato disponivel.")
        return cfg

    cache_path = _caminho_cache(cfg, arquivo_fds)
    cache_key = _chave_cache(cfg, arquivo_fds, hw, n_meshes)
    cache = _ler_cache(cache_path) if usar_cache else {"schema": CACHE_SCHEMA_VERSION, "entries": {}}
    entrada_cache = cache.get("entries", {}).get(cache_key)

    if usar_cache and entrada_cache and entrada_cache.get("snapshot"):
        logger.info(
            f"Otimizador: usando cache para {os.path.basename(arquivo_fds)} "
            f"({entrada_cache.get('descricao', 'configuracao salva')}).",
            extra={"console_visible": True},
        )
        return _aplicar_snapshot(cfg, entrada_cache["snapshot"])

    logger.info(
        f"Otimizador iniciado: {len(candidatos)} candidato(s), "
        f"{timeout_seg:g}s por teste, caso preservado em copia temporaria.",
        extra={"console_visible": True},
    )

    resultados = []
    for idx, cand in enumerate(candidatos, start=1):
        verificar_pulo(cand)
        descricao = descrever_candidato(cand)
        logger.info(
            f"Otimizador {idx}/{len(candidatos)}: testando {descricao}",
            extra={"console_visible": True},
        )
        try:
            resultado = executar_benchmark_candidato(cand, arquivo_fds, timeout_seg, opt_cfg)
        except PularCasoSolicitado:
            raise
        except Exception as exc:
            resultado = {
                "score": 0.0,
                "detalhe": f"falhou: {exc}",
                "elapsed": 0.0,
                "returncode": None,
                "timeout": False,
                "descricao": descricao,
                "snapshot": _snapshot_cfg(cand),
            }

        logger.info(
            f"  Resultado: score={resultado['score']:.6g} ({resultado['detalhe']})",
            extra={"console_visible": True},
        )
        resultados.append(resultado)

    resultados.sort(key=lambda item: item["score"], reverse=True)
    melhor = resultados[0]
    baseline = next(
        (r for r in resultados if r["snapshot"] == _snapshot_cfg(candidatos[0])),
        resultados[-1],
    )

    if melhor["score"] <= 0:
        logger.warning("Otimizador nao mediu progresso valido. Mantendo configuracao atual.")
        return cfg

    ganho_pct = 0.0
    if baseline["score"] > 0:
        ganho_pct = (melhor["score"] / baseline["score"] - 1.0) * 100.0
        if melhor["snapshot"] != baseline["snapshot"] and ganho_pct < melhoria_min_pct:
            logger.info(
                f"Otimizador: melhor ganho medido foi {ganho_pct:.1f}%, "
                f"abaixo do minimo de {melhoria_min_pct:.1f}%. Mantendo configuracao atual.",
                extra={"console_visible": True},
            )
            melhor = baseline

    logger.info(
        f"Otimizador selecionou {melhor['descricao']} "
        f"(score={melhor['score']:.6g}, ganho={ganho_pct:.1f}%).",
        extra={"console_visible": True},
    )

    if usar_cache:
        cache.setdefault("entries", {})[cache_key] = {
            "updated_at": time.time(),
            "arquivo": os.path.basename(arquivo_fds),
            "descricao": melhor["descricao"],
            "score": melhor["score"],
            "detalhe": melhor["detalhe"],
            "snapshot": melhor["snapshot"],
            "resultados": [
                {
                    "descricao": r["descricao"],
                    "score": r["score"],
                    "detalhe": r["detalhe"],
                    "elapsed": r["elapsed"],
                }
                for r in resultados
            ],
        }
        try:
            _salvar_cache(cache_path, cache, max_cache)
        except OSError as exc:
            logger.warning(f"Otimizador: nao foi possivel salvar cache: {exc}")

    return _aplicar_snapshot(cfg, melhor["snapshot"])
