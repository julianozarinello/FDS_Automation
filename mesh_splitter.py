# mesh_splitter.py — Divisão e reordenação automática de meshes para FDS Automation
# Regras FDS respeitadas:
#   - IJK deve conter apenas números inteiros positivos
#   - Cada dimensão deve ser múltiplo de 2 (recomendado; obrigatório apenas que > 0)
#   - Meshes atribuídas a um mesmo núcleo MPI devem ter IDs consecutivos e crescentes
#   - O eixo de corte é escolhido para maximizar células por núcleo com mínimo desequilíbrio
import re
import os
import math
import shutil
import logging
from copy import deepcopy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Estrutura de dados de uma mesh
# ---------------------------------------------------------------------------
class Mesh:
    """
    Representa uma linha &MESH do arquivo .fds.
    Atributos:
        id_original : índice 1-based na ordem original do arquivo
        xb          : [x1, x2, y1, y2, z1, z2]
        ijk         : [ni, nj, nk]
        linha_raw   : texto original da linha (preservado para campos extras)
        extras      : outros parâmetros além de XB e IJK (ex: MULT_ID, ID, etc.)
    """
    def __init__(self, id_original: int, xb: list, ijk: list, linha_raw: str, extras: str = ""):
        self.id_original = id_original
        self.xb          = xb          # [x1,x2,y1,y2,z1,z2]
        self.ijk         = ijk          # [ni,nj,nk]
        self.linha_raw   = linha_raw
        self.extras      = extras       # outros campos da linha &MESH

    @property
    def n_celulas(self) -> int:
        return self.ijk[0] * self.ijk[1] * self.ijk[2]

    @property
    def dx(self) -> float:
        return (self.xb[1] - self.xb[0]) / self.ijk[0] if self.ijk[0] else 1.0

    @property
    def dy(self) -> float:
        return (self.xb[3] - self.xb[2]) / self.ijk[1] if self.ijk[1] else 1.0

    @property
    def dz(self) -> float:
        return (self.xb[5] - self.xb[4]) / self.ijk[2] if self.ijk[2] else 1.0

    def para_linha_fds(self) -> str:
        xb_str  = ", ".join(f"{v:.6g}" for v in self.xb)
        ijk_str = ", ".join(str(v) for v in self.ijk)
        extras  = (" " + self.extras.strip()) if self.extras.strip() else ""
        return f"&MESH XB={xb_str}, IJK={ijk_str}{extras} /"


# ---------------------------------------------------------------------------
# Leitura de meshes do arquivo .fds
# ---------------------------------------------------------------------------
_RE_MESH  = re.compile(r'&MESH\b(.*?)/', re.IGNORECASE | re.DOTALL)
_RE_XB    = re.compile(r'XB\s*=\s*([\d\s.,eE+\-]+)', re.IGNORECASE)
_RE_IJK   = re.compile(r'IJK\s*=\s*([\d\s,]+)', re.IGNORECASE)
_RE_EXTRA = re.compile(
    r'(?:XB|IJK)\s*=\s*[\d\s.,eE+\-]+(?:,)?',
    re.IGNORECASE
)


def _parse_floats(s: str) -> list:
    return [float(x) for x in re.split(r'[,\s]+', s.strip()) if x]


def _parse_ints(s: str) -> list:
    return [int(x) for x in re.split(r'[,\s]+', s.strip()) if x]


def ler_meshes(caminho_fds: str) -> list[Mesh]:
    """Lê e retorna todas as meshes do arquivo .fds."""
    with open(caminho_fds, 'r', encoding='latin-1') as f:
        conteudo = f.read()

    meshes = []
    for idx, m in enumerate(_RE_MESH.finditer(conteudo), start=1):
        corpo = m.group(1)

        xb_m  = _RE_XB.search(corpo)
        ijk_m = _RE_IJK.search(corpo)
        if not xb_m or not ijk_m:
            logger.warning(f"Mesh #{idx}: XB ou IJK não encontrado, pulando.")
            continue

        xb  = _parse_floats(xb_m.group(1))
        ijk = _parse_ints(ijk_m.group(1))

        if len(xb) != 6 or len(ijk) != 3:
            logger.warning(f"Mesh #{idx}: formato inválido (XB={xb}, IJK={ijk}), pulando.")
            continue

        # Extrai campos extras (tudo exceto XB e IJK)
        extras = _RE_EXTRA.sub("", corpo).strip().rstrip(",").strip()

        meshes.append(Mesh(idx, xb, ijk, m.group(0), extras))

    return meshes


# ---------------------------------------------------------------------------
# Escolha do melhor eixo de corte
# ---------------------------------------------------------------------------
def _melhor_eixo(mesh: Mesh) -> int:
    """
    Retorna o índice do eixo (0=X, 1=Y, 2=Z) mais adequado para corte.
    Critérios (em ordem):
      1. O eixo com maior número de células (mais para dividir)
      2. Número de células deve ser par (para corte exato ao meio)
      3. Evita eixo com menos de 4 células (resultado seria inválido)
    """
    candidatos = []
    for eixo in range(3):
        n = mesh.ijk[eixo]
        if n < 4:
            continue  # mínimo 2 células por metade
        score = n  # prefere o maior
        # Bônus se for par (corte limpo)
        if n % 2 == 0:
            score += 1000
        candidatos.append((score, eixo))

    if not candidatos:
        return -1  # mesh muito pequena, não pode ser cortada

    candidatos.sort(reverse=True)
    return candidatos[0][1]


# ---------------------------------------------------------------------------
# Corte de uma mesh ao meio
# ---------------------------------------------------------------------------
def _cortar_mesh(mesh: Mesh, eixo: int) -> tuple["Mesh", "Mesh"]:
    """
    Divide a mesh ao meio no eixo indicado.
    Retorna duas novas Mesh com XB e IJK ajustados.
    As células são divididas o mais igualmente possível.
    O tamanho de célula (dx/dy/dz) é preservado.
    """
    xb  = list(mesh.xb)
    ijk = list(mesh.ijk)

    n_total = ijk[eixo]
    n1      = n_total // 2
    n2      = n_total - n1

    # Coordenada do ponto de corte (mantém tamanho de célula)
    if eixo == 0:
        delta = mesh.dx
        coord_inicio = xb[0]
        coord_meio   = coord_inicio + n1 * delta
        coord_fim    = xb[1]

        xb_a = [coord_inicio, coord_meio, xb[2], xb[3], xb[4], xb[5]]
        xb_b = [coord_meio,   coord_fim,  xb[2], xb[3], xb[4], xb[5]]

    elif eixo == 1:
        delta = mesh.dy
        coord_inicio = xb[2]
        coord_meio   = coord_inicio + n1 * delta
        coord_fim    = xb[3]

        xb_a = [xb[0], xb[1], coord_inicio, coord_meio, xb[4], xb[5]]
        xb_b = [xb[0], xb[1], coord_meio,   coord_fim,  xb[4], xb[5]]

    else:  # eixo == 2 (Z)
        delta = mesh.dz
        coord_inicio = xb[4]
        coord_meio   = coord_inicio + n1 * delta
        coord_fim    = xb[5]

        xb_a = [xb[0], xb[1], xb[2], xb[3], coord_inicio, coord_meio]
        xb_b = [xb[0], xb[1], xb[2], xb[3], coord_meio,   coord_fim]

    ijk_a = list(ijk); ijk_a[eixo] = n1
    ijk_b = list(ijk); ijk_b[eixo] = n2

    mesh_a = Mesh(mesh.id_original, xb_a, ijk_a, "", mesh.extras)
    mesh_b = Mesh(mesh.id_original, xb_b, ijk_b, "", mesh.extras)
    return mesh_a, mesh_b


# ---------------------------------------------------------------------------
# Algoritmo principal de divisão
# ---------------------------------------------------------------------------
def dividir_meshes(meshes: list[Mesh], n_cores: int, margem_pct: float = 10.0) -> list[Mesh]:
    """
    Divide as meshes até que o número total seja suficiente para distribuir
    de forma equilibrada entre n_cores, respeitando a margem de tolerância.

    Parâmetros:
        meshes     : lista de Mesh lidas do arquivo
        n_cores    : número de núcleos MPI disponíveis
        margem_pct : % de desequilíbrio máximo aceito entre núcleos (default 10%)

    Retorna:
        lista de Mesh (pode ter mais itens que a original)
    """
    resultado = deepcopy(meshes)

    # Precisamos de n_cores meshes (1 por núcleo) como alvo mínimo
    alvo = n_cores

    iteracao = 0
    MAX_ITER = 50

    while len(resultado) < alvo and iteracao < MAX_ITER:
        iteracao += 1

        # Escolhe a mesh com mais células para cortar
        candidatos = [
            (i, m) for i, m in enumerate(resultado)
            if _melhor_eixo(m) >= 0
        ]
        if not candidatos:
            logger.warning("Nenhuma mesh pode ser dividida mais. Encerrando divisão.")
            break

        # Ordena pelo maior número de células
        candidatos.sort(key=lambda x: x[1].n_celulas, reverse=True)
        idx_cortar, mesh_cortar = candidatos[0]

        eixo = _melhor_eixo(mesh_cortar)
        nomes_eixo = ["X", "Y", "Z"]
        logger.info(
            f"  Cortando mesh #{mesh_cortar.id_original} no eixo {nomes_eixo[eixo]} "
            f"({mesh_cortar.ijk[eixo]} células → "
            f"{mesh_cortar.ijk[eixo]//2} + {mesh_cortar.ijk[eixo] - mesh_cortar.ijk[eixo]//2})"
        )

        m_a, m_b = _cortar_mesh(mesh_cortar, eixo)
        resultado = resultado[:idx_cortar] + [m_a, m_b] + resultado[idx_cortar+1:]

    # Verifica equilíbrio de células por núcleo
    if len(resultado) >= n_cores:
        celulas_por_mesh = sorted([m.n_celulas for m in resultado], reverse=True)
        # Distribui greedy para verificar equilíbrio
        cargas = _distribuir_greedy(resultado, n_cores)
        max_c  = max(sum(ms) for ms in cargas)
        min_c  = min(sum(ms) for ms in cargas)
        desbal = (max_c - min_c) / max_c * 100 if max_c else 0

        logger.info(f"  Desequilíbrio entre núcleos: {desbal:.1f}% (limite: {margem_pct}%)")

        if desbal > margem_pct:
            logger.warning(
                f"  Desequilíbrio ({desbal:.1f}%) acima da margem ({margem_pct}%). "
                "Considere aumentar 'mesh_margem_pct' no config.yaml."
            )

    return resultado


def _distribuir_greedy(meshes: list[Mesh], n_cores: int) -> list[list[int]]:
    """
    Distribui meshes entre núcleos usando greedy (núcleo com menos carga recebe próxima mesh).
    Retorna lista de listas com contagens de células por núcleo.
    """
    cargas = [[] for _ in range(n_cores)]
    somas  = [0] * n_cores

    for mesh in sorted(meshes, key=lambda m: m.n_celulas, reverse=True):
        idx_min = somas.index(min(somas))
        cargas[idx_min].append(mesh.n_celulas)
        somas[idx_min] += mesh.n_celulas

    return cargas


# ---------------------------------------------------------------------------
# Reordenação das meshes para atribuição crescente por núcleo
# ---------------------------------------------------------------------------
def reordenar_meshes_por_nucleo(meshes: list[Mesh], n_cores: int) -> list[Mesh]:
    """
    Reordena a lista de meshes de forma que cada núcleo receba um bloco
    contíguo e crescente de IDs — exigência do FDS para MPI.

    Algoritmo:
      1. Ordena meshes por número de células (decrescente)
      2. Distribui por greedy entre núcleos (balanceamento de carga)
      3. Dentro de cada núcleo, ordena meshes por posição espacial
         (X → Y → Z) para manter coerência geométrica
      4. Concatena os grupos: núcleo 0, núcleo 1, ..., núcleo N-1
         → resultado é uma lista com IDs implicitamente crescentes
    """
    # Greedy: associa cada mesh a um núcleo
    somas   = [0] * n_cores
    grupos  = [[] for _ in range(n_cores)]

    for mesh in sorted(meshes, key=lambda m: m.n_celulas, reverse=True):
        idx_min = somas.index(min(somas))
        grupos[idx_min].append(mesh)
        somas[idx_min] += mesh.n_celulas

    # Dentro de cada núcleo, ordena por posição espacial (X, Y, Z)
    for grp in grupos:
        grp.sort(key=lambda m: (m.xb[0], m.xb[2], m.xb[4]))

    # Log de distribuição
    sep = "-" * 55
    logger.info(sep)
    logger.info("  DISTRIBUIÇÃO DE MESHES POR NÚCLEO")
    logger.info(sep)
    for i, grp in enumerate(grupos):
        total_celulas = sum(m.n_celulas for m in grp)
        logger.info(f"  Núcleo {i:>3}: {len(grp):>3} mesh(es)  |  {total_celulas:>10} células")
    logger.info(sep)

    # Concatena em ordem crescente de núcleo
    return [m for grp in grupos for m in grp]


# ---------------------------------------------------------------------------
# Reescrita do arquivo .fds
# ---------------------------------------------------------------------------
def reescrever_fds_com_meshes(caminho_fds: str, novas_meshes: list[Mesh]) -> str:
    """
    Substitui todas as linhas &MESH no arquivo .fds pelas novas meshes.
    Cria automaticamente um backup (.fds.bak) antes de modificar.
    Retorna o caminho do backup.
    """
    # Backup
    caminho_bak = caminho_fds + ".bak"
    shutil.copy2(caminho_fds, caminho_bak)
    logger.info(f"  Backup criado: {os.path.basename(caminho_bak)}")

    with open(caminho_fds, 'r', encoding='latin-1') as f:
        conteudo = f.read()

    # Remove todas as linhas &MESH existentes (incluindo multi-linha)
    conteudo_sem_meshes = _RE_MESH.sub("__MESH_PLACEHOLDER__", conteudo)

    # Monta bloco de novas meshes
    linhas_mesh = "\n".join(m.para_linha_fds() for m in novas_meshes)

    # Substitui o primeiro placeholder pelo bloco completo, remove os demais
    primeira = True
    def substituir(match):
        nonlocal primeira
        if primeira:
            primeira = False
            return linhas_mesh
        return ""  # remove placeholders extras

    novo_conteudo = re.sub("__MESH_PLACEHOLDER__", substituir, conteudo_sem_meshes)

    with open(caminho_fds, 'w', encoding='latin-1') as f:
        f.write(novo_conteudo)

    logger.info(f"  Arquivo reescrito com {len(novas_meshes)} mesh(es).")
    return caminho_bak


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------
def processar_meshes(
    caminho_fds: str,
    n_cores: int,
    margem_pct: float = 10.0,
    dry_run: bool = False,
) -> int:
    """
    Orquestra leitura → divisão → reordenação → reescrita.

    Parâmetros:
        caminho_fds : caminho absoluto do arquivo .fds
        n_cores     : número de processos MPI
        margem_pct  : tolerância de desequilíbrio em %
        dry_run     : se True, apenas loga sem modificar o arquivo

    Retorna:
        número final de meshes
    """
    sep = "=" * 55
    logger.info(sep)
    logger.info("  MESH SPLITTER")
    logger.info(sep)
    logger.info(f"  Arquivo   : {os.path.basename(caminho_fds)}")
    logger.info(f"  Núcleos   : {n_cores}")
    logger.info(f"  Margem    : {margem_pct}%")
    logger.info(f"  Modo      : {'DRY-RUN (sem alterações)' if dry_run else 'ATIVO'}")
    logger.info(sep)

    meshes_originais = ler_meshes(caminho_fds)
    if not meshes_originais:
        logger.warning("Nenhuma mesh encontrada. Mesh Splitter ignorado.")
        return 0

    logger.info(f"  Meshes originais : {len(meshes_originais)}")
    logger.info(f"  Células originais: {sum(m.n_celulas for m in meshes_originais)}")

    # Se já tem meshes suficientes, apenas reordena
    if len(meshes_originais) >= n_cores:
        logger.info("  Meshes já suficientes. Apenas reordenando.")
        novas_meshes = reordenar_meshes_por_nucleo(meshes_originais, n_cores)
    else:
        novas_meshes = dividir_meshes(meshes_originais, n_cores, margem_pct)
        novas_meshes = reordenar_meshes_por_nucleo(novas_meshes, n_cores)

    logger.info(f"  Meshes finais    : {len(novas_meshes)}")
    logger.info(f"  Células totais   : {sum(m.n_celulas for m in novas_meshes)}")

    if not dry_run:
        reescrever_fds_com_meshes(caminho_fds, novas_meshes)
    else:
        logger.info("  [DRY-RUN] Arquivo NÃO modificado.")

    return len(novas_meshes)
