# benchmark_runner.py — Suíte de benchmark isolada para FDS Automation
#
# OBJETIVO:
#   Testar múltiplas configurações de paralelismo em simulações reais,
#   coletar scores de velocidade e gerar relatório comparativo.
#   NÃO interfere no fluxo principal (main.py) e NÃO modifica os .fds originais.
#
# USO:
#   python tests/benchmark_runner.py
#   python tests/benchmark_runner.py --config tests/benchmark_config.yaml
#   python tests/benchmark_runner.py --config tests/benchmark_config.yaml --saida resultados.json
#
# SAÍDA:
#   - Relatório no console ao final
#   - Arquivo JSON com todos os resultados (para análise posterior)

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from datetime import datetime

import yaml

# ---------------------------------------------------------------------------
# Adiciona o diretório raiz ao path para importar os módulos do projeto
# ---------------------------------------------------------------------------
_RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)

from hw_detector import detectar_hardware
from fds_reader import contar_meshes
from mesh_splitter import processar_meshes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("benchmark")

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
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

_EXTENSOES_SAIDA_FDS = {
    ".bak", ".bf", ".end", ".err", ".exe", ".dll",
    ".ge1", ".iso", ".log", ".out", ".part", ".prt5",
    ".q", ".restart", ".sf", ".smv", ".s3d", ".status", ".stop",
}


# ---------------------------------------------------------------------------
# Carregamento de configuração
# ---------------------------------------------------------------------------
def carregar_config(caminho: str) -> dict:
    with open(caminho, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def descobrir_simulacoes(cfg: dict) -> list[str]:
    if cfg.get("pasta_simulacoes"):
        pasta = str(cfg["pasta_simulacoes"])
        arquivos = []
        for raiz, dirs, nomes in os.walk(pasta):
            dirs.sort()
            for nome in sorted(nomes):
                if nome.lower().endswith(".fds"):
                    arquivos.append(os.path.join(raiz, nome))
        arquivos.sort(key=lambda p: os.path.relpath(p, pasta).lower())
        return arquivos

    if cfg.get("simulacoes"):
        return [str(p) for p in cfg["simulacoes"]]

    if cfg.get("arquivo_fds"):
        return [str(cfg["arquivo_fds"])]

    raise ValueError("Nenhuma fonte de arquivos FDS definida no benchmark_config.yaml.")


# ---------------------------------------------------------------------------
# Geração de candidatos
# ---------------------------------------------------------------------------
def _safe_int(v, d: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def _safe_float(v, d: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def gerar_candidatos(cfg_bench: dict, hw: dict, n_meshes: int) -> list[dict]:
    """
    Gera a lista completa de candidatos a testar com base no benchmark_config.yaml.
    Cada candidato é um dict com: modo, num_cores, hybrid, mesh_splitter, peso_espacial, _label.
    """
    modos = [str(m).upper() for m in cfg_bench.get("modos", ["OPENMP", "MPI", "HYBRID"])]
    pesos = cfg_bench.get("pesos_espaciais", [0.0, 0.10, 0.15, 0.25])
    cores = max(1, _safe_int(hw.get("cores_fisicos"), 1))
    diretorio_fds = str(cfg_bench.get("diretorio_fds", ""))
    ms_margem = _safe_float(cfg_bench.get("mesh_splitter_margem_pct", 10.0), 10.0)
    ms_ativo = bool(cfg_bench.get("mesh_splitter_ativo", True))

    candidatos = []
    vistos = set()

    def adicionar(label: str, modo: str, num_cores: int, mpi: int, omp: int, peso: float) -> None:
        chave = (modo, num_cores, mpi, omp, round(peso, 4))
        if chave in vistos:
            return
        vistos.add(chave)
        candidatos.append({
            "_label": label,
            "diretorio_fds": diretorio_fds,
            "modo": modo,
            "num_cores": max(1, num_cores),
            "hybrid": {"mpi_procs": max(1, mpi), "omp_threads": max(1, omp)},
            "mesh_splitter": {
                "ativo": ms_ativo and modo in ("MPI", "HYBRID"),
                "margem_pct": ms_margem,
                "dry_run": False,
                "pular_se_otimizado": False,
                "peso_espacial": peso,
            },
            "performance": cfg_bench.get("performance", {
                "kmp_affinity": True,
                "omp_spin": True,
                "numa_opt": True,
            }),
        })

    # --- OPENMP ---
    if "OPENMP" in modos:
        adicionar(f"OPENMP-{cores}t", "OPENMP", cores, 1, cores, 0.0)
        if cores > 2 and cfg_bench.get("testar_frações_openmp", False):
            adicionar(f"OPENMP-{round(cores*0.75)}t", "OPENMP", round(cores * 0.75), 1, round(cores * 0.75), 0.0)
            adicionar(f"OPENMP-{cores//2}t", "OPENMP", cores // 2, 1, cores // 2, 0.0)

    # --- MPI ---
    if "MPI" in modos and n_meshes > 1:
        limite_mpi = min(cores, n_meshes)
        for fator in (1.0, 0.75, 0.50, 0.25):
            n = max(1, round(limite_mpi * fator))
            for peso in pesos:
                adicionar(f"MPI-{n}p-w{peso:g}", "MPI", n, n, 1, peso)

    # --- HYBRID ---
    if "HYBRID" in modos and n_meshes > 1 and cores >= 4:
        limite_mpi = min(cores, n_meshes)
        pares = [
            (max(1, min(limite_mpi, cores // 2)), 2),
            (max(1, min(limite_mpi, cores // 4)), 4),
            (max(1, min(limite_mpi, round(math.sqrt(cores)))), max(1, round(math.sqrt(cores)))),
        ]
        for mpi, omp in pares:
            if mpi * omp > cores:
                omp = max(1, cores // mpi)
            for peso in pesos:
                adicionar(f"HYB-{mpi}x{omp}-w{peso:g}", "HYBRID", mpi * omp, mpi, omp, peso)

    return candidatos


# ---------------------------------------------------------------------------
# Preparação do ambiente de benchmark (cópia isolada)
# ---------------------------------------------------------------------------
def _deve_copiar(origem: str, arquivo_fds: str, limite_mb: float) -> bool:
    ext = os.path.splitext(origem)[1].lower()
    if ext in _EXTENSOES_SAIDA_FDS and os.path.abspath(origem) != os.path.abspath(arquivo_fds):
        return False
    try:
        if os.path.getsize(origem) > int(limite_mb * 1024 * 1024):
            return False
    except OSError:
        return False
    return True


def preparar_pasta_benchmark(arquivo_fds: str, tmp_dir: str, limite_mb: float = 250.0) -> tuple[str, str]:
    origem_dir = os.path.dirname(arquivo_fds)
    destino_dir = os.path.join(tmp_dir, "case")
    os.makedirs(destino_dir, exist_ok=True)

    for raiz, dirs, arquivos in os.walk(origem_dir):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
        rel = os.path.relpath(raiz, origem_dir)
        dest_raiz = os.path.join(destino_dir, rel) if rel != "." else destino_dir
        os.makedirs(dest_raiz, exist_ok=True)
        for nome in arquivos:
            src = os.path.join(raiz, nome)
            if _deve_copiar(src, arquivo_fds, limite_mb):
                shutil.copy2(src, os.path.join(dest_raiz, nome))

    tmp_fds = os.path.join(destino_dir, os.path.relpath(arquivo_fds, origem_dir))
    if not os.path.exists(tmp_fds):
        tmp_fds = os.path.join(destino_dir, os.path.basename(arquivo_fds))
        shutil.copy2(arquivo_fds, tmp_fds)

    return tmp_fds, destino_dir


# ---------------------------------------------------------------------------
# Montagem de comando e ambiente
# ---------------------------------------------------------------------------
def construir_comando(cand: dict, nome_fds: str) -> list[str]:
    modo = cand["modo"].upper()
    d = cand["diretorio_fds"]
    fds_openmp = os.path.join(d, "fds_openmp.exe")
    fds_exe = os.path.join(d, "fds.exe")
    mpiexec = os.path.join(d, "mpi", "mpiexec.exe")
    if not os.path.exists(mpiexec):
        mpiexec = os.path.join(d, "mpiexec.exe")

    if modo == "OPENMP":
        return [fds_openmp, nome_fds]

    if modo == "MPI":
        n = cand["num_cores"]
        return [mpiexec, "-n", str(n),
                "-genv", "I_MPI_PIN", "1",
                "-genv", "I_MPI_PIN_ORDER", "compact",
                "-bind-to", "core", fds_exe, nome_fds]

    if modo == "HYBRID":
        mpi = cand["hybrid"]["mpi_procs"]
        omp = cand["hybrid"]["omp_threads"]
        return [mpiexec, "-n", str(mpi),
                "-genv", "I_MPI_PIN", "1",
                "-genv", "I_MPI_PIN_DOMAIN", f"omp:{omp}",
                "-genv", "I_MPI_PIN_ORDER", "compact",
                fds_exe, nome_fds]

    return [os.path.join(d, "fds.exe"), nome_fds]


def montar_env(cand: dict) -> dict:
    env = os.environ.copy()
    modo = cand["modo"].upper()
    perf = cand.get("performance") or {}

    if modo == "OPENMP":
        env["OMP_NUM_THREADS"] = str(cand["num_cores"])
    elif modo == "HYBRID":
        env["OMP_NUM_THREADS"] = str(cand["hybrid"]["omp_threads"])
    else:
        env["OMP_NUM_THREADS"] = "1"

    if perf.get("kmp_affinity", True):
        env["KMP_AFFINITY"] = "granularity=fine,compact,1,0"
        env["OMP_PROC_BIND"] = "close"
    if perf.get("omp_spin", True):
        env["GOMP_SPINCOUNT"] = "10000000"
        env["OMP_WAIT_POLICY"] = "active"
        env["KMP_BLOCKTIME"] = "200"
    if perf.get("numa_opt", True):
        env["OMP_PLACES"] = "cores"

    env["MKL_NUM_THREADS"] = "1"
    env["MKL_DYNAMIC"] = "FALSE"
    env["OMP_DYNAMIC"] = "FALSE"
    env["OMP_DISPLAY_ENV"] = "FALSE"

    d = cand["diretorio_fds"]
    mpi_dir = os.path.join(d, "mpi")
    env["PATH"] = os.pathsep.join([d, mpi_dir, env.get("PATH", "")])
    return env


# ---------------------------------------------------------------------------
# Execução e scoring
# ---------------------------------------------------------------------------
def _encerrar(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _score(saida: str, elapsed: float, returncode, timeout: bool) -> tuple[float, str]:
    if any(m in saida.upper() for m in FDS_ERROR_MARKERS):
        return 0.0, "erro FDS"
    if returncode not in (0, None) and not timeout:
        return 0.0, f"codigo {returncode}"

    ultimo = None
    for linha in saida.splitlines():
        m = _RE_FDS_TIME_STEP.search(linha)
        if m:
            ultimo = (m.group(1), m.group(2))

    if not ultimo or elapsed <= 0:
        return 0.0, "sem progresso"

    step, t_sim = ultimo
    try:
        sim_s = float(t_sim)
    except ValueError:
        return 0.0, "tempo invalido"

    return sim_s / elapsed, f"passo {step} | {t_sim}s simulados"


def executar_candidato(
    cand: dict,
    arquivo_fds: str,
    timeout_seg: float,
    limite_mb: float,
) -> dict:
    """
    Executa um candidato em pasta temporária isolada e retorna o resultado.
    O arquivo .fds original NUNCA é modificado.
    """
    with tempfile.TemporaryDirectory(prefix="fds_bench_") as tmp_dir:
        tmp_fds, pasta = preparar_pasta_benchmark(arquivo_fds, tmp_dir, limite_mb)

        ms_cfg = cand.get("mesh_splitter") or {}
        if ms_cfg.get("ativo", False):
            modo = cand["modo"].upper()
            n_cores = cand["hybrid"]["mpi_procs"] if modo == "HYBRID" else cand["num_cores"]
            processar_meshes(
                tmp_fds,
                n_cores,
                _safe_float(ms_cfg.get("margem_pct"), 10.0),
                False,
                False,
                _safe_float(ms_cfg.get("peso_espacial"), 0.15),
            )

        comando = construir_comando(cand, os.path.basename(tmp_fds))
        env = montar_env(cand)

        inicio = time.time()
        proc = subprocess.Popen(
            comando, cwd=pasta,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
        )

        timeout = False
        saida = ""
        try:
            saida, _ = proc.communicate(timeout=timeout_seg)
        except subprocess.TimeoutExpired:
            timeout = True
            _encerrar(proc)
            try:
                saida, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                _encerrar(proc)

        elapsed = time.time() - inicio
        returncode = proc.returncode

    score, detalhe = _score(saida or "", elapsed, returncode, timeout)
    return {
        "label": cand["_label"],
        "modo": cand["modo"],
        "num_cores": cand["num_cores"],
        "hybrid": cand["hybrid"],
        "peso_espacial": (cand.get("mesh_splitter") or {}).get("peso_espacial", 0.0),
        "score": score,
        "detalhe": detalhe,
        "elapsed_seg": round(elapsed, 2),
        "timeout": timeout,
        "returncode": returncode,
    }


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------
SEP = "=" * 65
SEP2 = "-" * 65


def imprimir_relatorio(resultados_por_caso: dict) -> None:
    print()
    print(SEP)
    print("  RELATÓRIO FINAL DO BENCHMARK")
    print(SEP)

    for caso, resultados in resultados_por_caso.items():
        nome = os.path.basename(caso)
        print()
        print(f"  CASO: {nome}")
        print(SEP2)
        print(f"  {'#':<4} {'Configuração':<30} {'Score':>10}  {'Detalhe'}")
        print(SEP2)

        ordenados = sorted(resultados, key=lambda r: r["score"], reverse=True)
        for i, r in enumerate(ordenados, start=1):
            score_str = f"{r['score']:.6g}" if r["score"] > 0 else "  FALHOU"
            medalha = " ★" if i == 1 and r["score"] > 0 else "  "
            print(f"  {i:<4} {r['label']:<30} {score_str:>10}  {r['detalhe']}{medalha}")

        melhor = ordenados[0] if ordenados and ordenados[0]["score"] > 0 else None
        if melhor:
            print()
            print(f"  ► MELHOR para {nome}: {melhor['label']}")
            print(f"    Score : {melhor['score']:.6g}")
            print(f"    Modo  : {melhor['modo']} | cores={melhor['num_cores']}")
            ms = melhor.get("peso_espacial", 0.0)
            if melhor["modo"] in ("MPI", "HYBRID"):
                print(f"    Peso espacial: {ms:g}")
            if melhor["modo"] == "HYBRID":
                h = melhor["hybrid"]
                print(f"    MPI x OMP : {h['mpi_procs']} x {h['omp_threads']}")
        print(SEP2)

    print()
    print(SEP)
    print("  RESUMO GLOBAL (média de score por configuração)")
    print(SEP2)

    scores_por_label: dict[str, list[float]] = {}
    for resultados in resultados_por_caso.values():
        for r in resultados:
            if r["score"] > 0:
                scores_por_label.setdefault(r["label"], []).append(r["score"])

    if scores_por_label:
        medias = [(label, sum(v)/len(v)) for label, v in scores_por_label.items()]
        medias.sort(key=lambda x: x[1], reverse=True)
        for i, (label, media) in enumerate(medias, start=1):
            medalha = " ★" if i == 1 else "  "
            print(f"  {i:<4} {label:<30} média={media:.6g}{medalha}")
    print(SEP)


def salvar_json(resultados_por_caso: dict, hw: dict, caminho: str) -> None:
    dados = {
        "benchmark_data": datetime.now().isoformat(timespec="seconds"),
        "hardware": hw,
        "casos": {
            os.path.basename(caso): resultados
            for caso, resultados in resultados_por_caso.items()
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    logger.info(f"Resultados salvos em: {caminho}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark isolado de configurações FDS — não altera arquivos originais."
    )
    parser.add_argument(
        "--config", default="tests/benchmark_config.yaml",
        help="Caminho para o benchmark_config.yaml",
    )
    parser.add_argument(
        "--saida", default="tests/benchmark_resultados.json",
        help="Caminho do arquivo JSON com os resultados",
    )
    args = parser.parse_args()

    cfg = carregar_config(args.config)
    cfg_bench = cfg.get("benchmark", cfg)
    timeout_seg = _safe_float(cfg_bench.get("tempo_teste_seg", 60.0), 60.0)
    limite_mb = _safe_float(cfg_bench.get("copiar_arquivos_ate_mb", 250.0), 250.0)

    simulacoes = descobrir_simulacoes(cfg_bench)
    if not simulacoes:
        logger.error("Nenhum arquivo .fds encontrado.")
        sys.exit(1)

    hw = detectar_hardware()
    logger.info(f"Hardware: {hw.get('cpu_nome')} | {hw.get('cores_fisicos')} cores físicos")
    logger.info(f"Simulações: {len(simulacoes)} | Timeout por teste: {timeout_seg}s")

    resultados_por_caso: dict[str, list] = {}

    for idx_caso, arquivo_fds in enumerate(simulacoes, start=1):
        nome = os.path.basename(arquivo_fds)
        n_meshes = contar_meshes(arquivo_fds)
        candidatos = gerar_candidatos(cfg_bench, hw, n_meshes)

        print()
        print(SEP)
        print(f"  CASO {idx_caso}/{len(simulacoes)}: {nome}")
        print(f"  Meshes originais: {n_meshes} | Candidatos: {len(candidatos)}")
        print(SEP)

        resultados_caso = []
        for idx_cand, cand in enumerate(candidatos, start=1):
            label = cand["_label"]
            logger.info(f"  [{idx_cand}/{len(candidatos)}] Testando: {label}")
            try:
                resultado = executar_candidato(cand, arquivo_fds, timeout_seg, limite_mb)
            except Exception as exc:
                resultado = {
                    "label": label, "modo": cand["modo"],
                    "num_cores": cand["num_cores"], "hybrid": cand["hybrid"],
                    "peso_espacial": 0.0, "score": 0.0,
                    "detalhe": f"excecao: {exc}",
                    "elapsed_seg": 0.0, "timeout": False, "returncode": None,
                }
            s = resultado["score"]
            logger.info(f"    Score: {s:.6g} ({resultado['detalhe']})")
            resultados_caso.append(resultado)

        resultados_por_caso[arquivo_fds] = resultados_caso

    imprimir_relatorio(resultados_por_caso)
    salvar_json(resultados_por_caso, hw, args.saida)


if __name__ == "__main__":
    main()
