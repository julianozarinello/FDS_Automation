import os
import glob
import shutil
import subprocess
from pathlib import Path


# ── Caminhos padrão ───────────────────────────────────────────────────────────

FDS_DEFAULT_PATHS = [
    r"C:\Program Files\firemodels\FDS6\bin",
    r"C:\Program Files\firemodels\FDS6\bin\mpi",
]

PYROSIM_DEFAULT_PATHS = [
    r"C:\Program Files\PyroSim 2023\fds",
    r"C:\Program Files\PyroSim 2023\fds\mpi",
]

# Executáveis críticos que devem ser localizáveis após o setup
REQUIRED_EXECUTABLES = {
    "fds"      : ["fds.exe", "fds_local.exe"],
    "mpiexec"  : ["mpiexec.exe"],
    "hydra"    : ["hydra_service.exe", "hydra_pmi_proxy.exe"],  # Intel MPI
}


# ── Detecção automática ───────────────────────────────────────────────────────

def _drives_disponiveis() -> list[str]:
    """Retorna letras de drives existentes no Windows (ex: ['C:', 'D:'])."""
    drives = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1 << i):
                drives.append(chr(65 + i) + ":")
    except Exception:
        drives = ["C:"]
    return drives


def detect_fds_paths() -> list[str]:
    drives = _drives_disponiveis()
    roots = []
    for d in drives:
        roots += [
            f"{d}\\Program Files\\firemodels",
            f"{d}\\Program Files (x86)\\firemodels",
            f"{d}\\firemodels",
            f"{d}\\FDS",
        ]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for match in glob.glob(os.path.join(root, "**/fds.exe"), recursive=True):
            bin_dir = str(Path(match).parent)
            mpi_dir = str(Path(match).parent / "mpi")
            if bin_dir not in found:
                found.append(bin_dir)
            if os.path.isdir(mpi_dir) and mpi_dir not in found:
                found.append(mpi_dir)
    return found


def detect_pyrosim_paths() -> list[str]:
    drives = _drives_disponiveis()
    roots = []
    for d in drives:
        roots += [
            f"{d}\\Program Files",
            f"{d}\\Program Files (x86)",
        ]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for match in glob.glob(os.path.join(root, "PyroSim*", "fds", "fds.exe")):
            fds_dir = str(Path(match).parent)
            mpi_dir = str(Path(match).parent / "mpi")
            if fds_dir not in found:
                found.append(fds_dir)
            if os.path.isdir(mpi_dir) and mpi_dir not in found:
                found.append(mpi_dir)
    return found


# ── Validação dos executáveis críticos ───────────────────────────────────────

def validate_mpi_ready() -> dict[str, str | None]:
    """
    Após configurar o PATH, verifica se todos os executáveis
    críticos para rodar FDS em MPI estão acessíveis.
    Retorna {nome: caminho_encontrado_ou_None}.
    """
    result = {}
    for name, candidates in REQUIRED_EXECUTABLES.items():
        found_path = None
        for exe in candidates:
            path = shutil.which(exe)
            if path:
                found_path = path
                break
        result[name] = found_path
    return result


def validate_mpi_execution() -> bool:
    """
    Testa se o mpiexec responde corretamente executando 'mpiexec -help'.
    Retorna True se funcionar, False caso contrário.
    """
    try:
        proc = subprocess.run(
            ["mpiexec", "-help"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # mpiexec retorna código 0 ou 1 dependendo da versão — ambos são válidos
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ── PATH da sessão ────────────────────────────────────────────────────────────

def add_paths_to_session(paths: list[str]) -> None:
    current = os.environ.get("PATH", "")
    to_add = [p for p in paths if p.lower() not in current.lower()]
    if to_add:
        os.environ["PATH"] = ";".join(to_add) + ";" + current


def check_paths_in_env(paths: list[str]) -> dict[str, bool]:
    current = [p.strip().lower() for p in os.environ.get("PATH", "").split(";")]
    return {p: p.lower() in current for p in paths}


# ── Fluxo principal ───────────────────────────────────────────────────────────

def setup_fds_mpi_paths() -> dict:
    """
    Configura o PATH da sessão com os caminhos do FDS e MPI.
    Valida ao final se todos os executáveis críticos estão acessíveis.

    Retorna um dict com resultado completo da operação.
    """
    result = {
        "fds_paths"    : [],
        "pyrosim_paths": [],
        "added"        : [],
        "not_found"    : [],
        "executables"  : {},
        "mpi_ok"       : False,
    }

    paths_to_ensure = []

    # ── Detecta FDS ──
    print("[1/4] Verificando instalação do FDS...")
    if any(os.path.isdir(p) for p in FDS_DEFAULT_PATHS):
        print("      ✅ FDS padrão encontrado.")
        paths_to_ensure.extend(FDS_DEFAULT_PATHS)
        result["fds_paths"] = FDS_DEFAULT_PATHS
    else:
        print("      ⚠️  Padrão não encontrado. Buscando instalação alternativa...")
        detected = detect_fds_paths()
        if detected:
            print(f"      ✅ FDS alternativo encontrado: {detected}")
            paths_to_ensure.extend(detected)
            result["fds_paths"] = detected
        else:
            print("      ❌ FDS não encontrado.")

    # ── Detecta PyroSim ──
    print("\n[2/4] Verificando instalação do PyroSim...")
    if any(os.path.isdir(p) for p in PYROSIM_DEFAULT_PATHS):
        print("      ✅ PyroSim padrão encontrado.")
        paths_to_ensure.extend(PYROSIM_DEFAULT_PATHS)
        result["pyrosim_paths"] = PYROSIM_DEFAULT_PATHS
    else:
        print("      ⚠️  Padrão não encontrado. Buscando instalação alternativa...")
        detected = detect_pyrosim_paths()
        if detected:
            print(f"      ✅ PyroSim alternativo encontrado: {detected}")
            paths_to_ensure.extend(detected)
            result["pyrosim_paths"] = detected
        else:
            print("      ❌ PyroSim não encontrado.")

    # ── Adiciona ao PATH ──
    print("\n[3/4] Configurando PATH da sessão...")
    status = check_paths_in_env(paths_to_ensure)
    to_add = []

    for path, already_present in status.items():
        if already_present:
            print(f"      ✅ Já presente : {path}")
        elif os.path.isdir(path):
            print(f"      ➕ Adicionando : {path}")
            to_add.append(path)
            result["added"].append(path)
        else:
            print(f"      ❌ Não existe  : {path}")
            result["not_found"].append(path)

    add_paths_to_session(to_add)

    # ── Valida executáveis MPI ──
    print("\n[4/4] Validando executáveis para MPI...")
    exe_status = validate_mpi_ready()
    result["executables"] = exe_status

    all_critical_ok = True
    for name, path in exe_status.items():
        if path:
            print(f"      ✅ {name:10s} → {path}")
        else:
            # hydra não é crítico em todas as versões do MPI
            if name == "hydra":
                print(f"      ⚠️  {name:10s} → não encontrado (opcional)")
            else:
                print(f"      ❌ {name:10s} → NÃO ENCONTRADO (crítico para MPI)")
                all_critical_ok = False

    # Teste funcional do mpiexec
    if all_critical_ok:
        mpi_ok = validate_mpi_execution()
        result["mpi_ok"] = mpi_ok
        if mpi_ok:
            print("\n      ✅ mpiexec respondeu corretamente — MPI pronto!")
        else:
            print("\n      ❌ mpiexec não respondeu — verifique a instalação do MPI.")
    else:
        print("\n      ❌ Executáveis críticos ausentes — MPI não pode ser iniciado.")

    return result


# ── Execução ──────────────────────────────────────────────────────────────────

# ── Função para rodar o script inteiro (com os prints e resumos) ──────────────

def executar_verificacao_path():
    print("=" * 55)
    print("  Setup de PATH para FDS com MPI")
    print("=" * 55 + "\n")

    resultado = setup_fds_mpi_paths()

    print("\n" + "=" * 55)
    print("  RESUMO FINAL")
    print("=" * 55)
    print(f"  MPI pronto para uso : {'✅ SIM' if resultado['mpi_ok'] else '❌ NÃO'}")
    print(f"  Caminhos adicionados: {len(resultado['added'])}")
    print(f"  Caminhos ausentes   : {len(resultado['not_found'])}")

    if not resultado["mpi_ok"]:
        print("\n  ⚠️  Sugestões:")
        print("     - Verifique se o FDS ou PyroSim está instalado corretamente")
        print("     - Confirme que o MPI foi incluído durante a instalação")
        print("     - Rode o instalador do FDS com a opção MPI habilitada")

# Mantemos isso aqui para que o script ainda funcione caso você clique diretamente nele
if __name__ == "__main__":
    executar_verificacao_path()
