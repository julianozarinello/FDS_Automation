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
import tempfile
from copy import deepcopy

from fds_reader import (
    encontrar_namelists,
    mascarar_strings_fortran,
    motivos_contagem_mesh_indeterminada,
    remover_comentarios_fortran,
)

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
    def __init__(
        self,
        id_original: int,
        xb: list,
        ijk: list,
        linha_raw: str,
        extras: str = "",
        mpi_process: int | None = None,
    ):
        self.id_original = id_original
        self.xb          = xb          # [x1,x2,y1,y2,z1,z2]
        self.ijk         = ijk          # [ni,nj,nk]
        self.linha_raw   = linha_raw
        self.extras      = extras       # outros campos da linha &MESH
        self.mpi_process = mpi_process

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

    @property
    def centro(self) -> tuple[float, float, float]:
        return (
            (self.xb[0] + self.xb[1]) / 2,
            (self.xb[2] + self.xb[3]) / 2,
            (self.xb[4] + self.xb[5]) / 2,
        )

    def para_linha_fds(self) -> str:
        # 17 digitos significativos evitam o arredondamento que formatos
        # curtos causavam em dominios grandes e garantem round-trip IEEE-754:
        # ler -> regravar -> ler devolve exatamente a mesma coordenada.
        xb_str  = ", ".join(f"{v:.17g}" for v in self.xb)
        ijk_str = ", ".join(str(v) for v in self.ijk)
        campos = [f"XB={xb_str}", f"IJK={ijk_str}"]

        extras = self.extras.strip().strip(",").strip()
        if extras:
            campos.append(extras)

        if self.mpi_process is not None:
            campos.append(f"MPI_PROCESS={self.mpi_process}")

        return f"&MESH {', '.join(campos)} /"


# ---------------------------------------------------------------------------
# Leitura de meshes do arquivo .fds
# ---------------------------------------------------------------------------
_RE_ATRIBUICAO = re.compile(
    r"(?<![A-Z0-9_%])([A-Z][A-Z0-9_%]*(?:\s*\([^)]*\))?)\s*=",
    re.IGNORECASE,
)


def _expandir_repeticoes_fortran(s: str, conversor) -> list:
    """Expande a notacao de repeticao de namelist (ex.: 3*4 -> 4,4,4)."""
    valores = []
    for token in (x for x in re.split(r'[,\s]+', s.strip()) if x):
        if "*" in token:
            repeticoes_txt, valor_txt = token.split("*", 1)
            repeticoes = int(repeticoes_txt)
            if repeticoes < 0 or not valor_txt:
                raise ValueError(f"repeticao invalida: {token}")
            valor = conversor(valor_txt.replace("D", "E").replace("d", "e"))
            valores.extend([valor] * repeticoes)
        else:
            valores.append(conversor(token.replace("D", "E").replace("d", "e")))
    return valores


def _parse_floats(s: str) -> list:
    return _expandir_repeticoes_fortran(s, float)


def _parse_ints(s: str) -> list:
    return _expandir_repeticoes_fortran(s, int)


def _campos_namelist(corpo: str) -> list[dict[str, str]]:
    """Tokeniza atribuicoes fora de strings sem alterar textos livres.

    Valores vetoriais do FDS usam virgulas, portanto nao e seguro separar um
    namelist simplesmente por ``,``. Em vez disso, cada campo termina onde a
    proxima atribuicao ``NOME=`` comeca; strings ficam mascaradas durante essa
    busca. Assim ``ID='A,B / XB=texto'`` continua sendo um unico campo.
    """
    mascara = mascarar_strings_fortran(corpo)
    correspondencias = list(_RE_ATRIBUICAO.finditer(mascara))
    if not correspondencias:
        return []

    prefixo = mascara[:correspondencias[0].start()]
    if prefixo.strip(" \t\r\n,"):
        raise ValueError(f"texto inesperado antes do primeiro campo: {prefixo!r}")

    campos: list[dict[str, str]] = []
    for indice, atual in enumerate(correspondencias):
        fim = (
            correspondencias[indice + 1].start()
            if indice + 1 < len(correspondencias)
            else len(corpo)
        )
        nome_com_indice = atual.group(1).strip()
        nome = nome_com_indice.split("(", 1)[0].strip().upper()
        valor = corpo[atual.end():fim].strip().rstrip(",").rstrip()
        texto = corpo[atual.start():fim].strip().strip(",").strip()
        campos.append({"nome": nome, "valor": valor, "texto": texto})
    return campos


def _tem_campo_extra(mesh: Mesh, campo: str) -> bool:
    try:
        return any(
            item["nome"] == campo.upper() for item in _campos_namelist(mesh.extras)
        )
    except ValueError:
        return False


def _valor_logico_verdadeiro(valor: str) -> bool:
    return valor.strip().strip(",").upper() in {"T", ".TRUE.", "TRUE"}


def _motivos_bloqueio_divisao(caminho_fds: str, meshes: list[Mesh]) -> list[str]:
    """Recursos cuja semantica depende da ordem/quantidade de meshes."""
    motivos: list[str] = []

    campos_transformacao = ("TRNX_ID", "TRNY_ID", "TRNZ_ID")
    if any(
        _tem_campo_extra(mesh, campo)
        for mesh in meshes
        for campo in campos_transformacao
    ):
        motivos.append("TRNX_ID/TRNY_ID/TRNZ_ID em MESH")

    for mesh in meshes:
        try:
            campos = _campos_namelist(mesh.extras)
        except ValueError:
            campos = []
        if any(
            campo["nome"] == "CYLINDRICAL"
            and _valor_logico_verdadeiro(campo["valor"])
            for campo in campos
        ):
            motivos.append("mesh CYLINDRICAL")
            break

    with open(caminho_fds, "r", encoding="latin-1") as f:
        conteudo = f.read()

    # No fonte atual do FDS, estes sao os namelists que aceitam MESH_NUMBER.
    # Inserir filhas deslocaria o indice e poderia mudar transformacoes ou
    # fazer uma slice apontar apenas para a filha/mesh errada.
    for nome_dependente in ("TRNX", "TRNY", "TRNZ", "SLCF"):
        for bloco in encontrar_namelists(conteudo, nome_dependente):
            corpo = remover_comentarios_fortran(bloco.corpo)
            try:
                campos = _campos_namelist(corpo)
            except ValueError:
                campos = []
            if any(campo["nome"] == "MESH_NUMBER" for campo in campos):
                motivos.append(f"{nome_dependente} com MESH_NUMBER")
                break

    if encontrar_namelists(conteudo, "CSVF"):
        motivos.append("CSVF associado a meshes")

    for bloco in encontrar_namelists(conteudo, "MISC"):
        corpo = remover_comentarios_fortran(bloco.corpo)
        try:
            campos = _campos_namelist(corpo)
        except ValueError:
            campos = []
        for campo in campos:
            if campo["nome"] == "RESTART" and _valor_logico_verdadeiro(campo["valor"]):
                motivos.append("RESTART ativo")
            elif campo["nome"] == "RESTART_CHID":
                valor = campo["valor"].strip().strip("'\"").strip()
                if valor and valor.lower() != "null":
                    motivos.append("RESTART_CHID definido")

    # Mantem a mensagem curta e deterministica quando mais de uma linha usa
    # o mesmo recurso.
    return list(dict.fromkeys(motivos))


def ler_meshes(caminho_fds: str) -> list[Mesh]:
    """Lê e retorna todas as meshes do arquivo .fds."""
    with open(caminho_fds, 'r', encoding='latin-1') as f:
        conteudo = f.read()

    meshes = []
    erros = []
    for idx, bloco in enumerate(encontrar_namelists(conteudo, "MESH"), start=1):
        # Comentarios podem conter '/', XB/IJK de exemplo ou texto livre.
        # Eles nao participam do namelist e nao devem virar campos na linha
        # regravada. Strings permanecem intactas, inclusive ID='A/B'.
        corpo = remover_comentarios_fortran(bloco.corpo)

        try:
            campos = _campos_namelist(corpo)
        except ValueError as exc:
            erros.append(f"mesh #{idx}: namelist invalido ({exc})")
            continue
        por_nome: dict[str, list[dict[str, str]]] = {}
        for campo in campos:
            por_nome.setdefault(campo["nome"], []).append(campo)

        if len(por_nome.get("XB", [])) != 1 or len(por_nome.get("IJK", [])) != 1:
            erros.append(f"mesh #{idx}: XB ou IJK nao encontrado")
            continue

        try:
            xb = _parse_floats(por_nome["XB"][0]["valor"])
            ijk = _parse_ints(por_nome["IJK"][0]["valor"])
        except (TypeError, ValueError) as exc:
            erros.append(f"mesh #{idx}: valores invalidos ({exc})")
            continue

        if len(xb) != 6 or len(ijk) != 3:
            erros.append(f"mesh #{idx}: formato invalido (XB={xb}, IJK={ijk})")
            continue
        if any(n <= 0 for n in ijk):
            erros.append(f"mesh #{idx}: IJK deve conter inteiros positivos ({ijk})")
            continue
        if any(not math.isfinite(v) for v in xb) or any(
            xb[i] >= xb[i + 1] for i in (0, 2, 4)
        ):
            erros.append(f"mesh #{idx}: limites XB invalidos ({xb})")
            continue

        mpi_process = None
        campos_mpi = por_nome.get("MPI_PROCESS", [])
        if len(campos_mpi) > 1:
            erros.append(f"mesh #{idx}: MPI_PROCESS definido mais de uma vez")
            continue
        if campos_mpi:
            valor_mpi = campos_mpi[0]["valor"].strip()
            if not re.fullmatch(r"[+-]?\d+", valor_mpi):
                erros.append(f"mesh #{idx}: MPI_PROCESS invalido ({valor_mpi!r})")
                continue
            mpi_process = int(valor_mpi)

        # Extrai campos extras (tudo exceto XB, IJK e MPI_PROCESS)
        extras = ", ".join(
            campo["texto"]
            for campo in campos
            if campo["nome"] not in {"XB", "IJK", "MPI_PROCESS"}
        )

        meshes.append(Mesh(idx, xb, ijk, bloco.texto, extras, mpi_process))

    if erros:
        # Nunca continue com uma lista parcial: a reescrita substitui todos os
        # namelists &MESH e uma entrada ignorada seria apagada silenciosamente.
        resumo = "; ".join(erros[:5])
        if len(erros) > 5:
            resumo += f"; e mais {len(erros) - 5} erro(s)"
        raise ValueError(
            "Mesh Splitter cancelado para preservar o arquivo original: " + resumo
        )

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
      3. Exige pelo menos 4 celulas em cada filha (n >= 8)
    """
    candidatos = []
    for eixo in range(3):
        n = mesh.ijk[eixo]
        if n < 8:
            # Dimensoes 2 ou 3 frequentemente falham na inicializacao da
            # equacao de Poisson do FDS. O eixo unitario de um caso 2D nao e
            # candidato a corte; nos demais, cada filha fica com >=4.
            continue
        # O maior eixo vem primeiro; paridade serve apenas como desempate.
        # O bonus fixo anterior podia preferir 4 celulas pares a 101 impares,
        # criando subdominios muito finos e mais comunicacao entre meshes.
        candidatos.append((n, 1 if n % 2 == 0 else 0, eixo))

    if not candidatos:
        return -1  # mesh muito pequena, não pode ser cortada

    candidatos.sort(reverse=True)
    return candidatos[0][2]


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

    mesh_a = Mesh(mesh.id_original, xb_a, ijk_a, "", mesh.extras, mesh.mpi_process)
    mesh_b = Mesh(mesh.id_original, xb_b, ijk_b, "", mesh.extras, mesh.mpi_process)
    return mesh_a, mesh_b


# ---------------------------------------------------------------------------
# Algoritmo principal de divisão
# ---------------------------------------------------------------------------
def dividir_meshes(
    meshes: list[Mesh],
    n_cores: int,
    margem_pct: float = 10.0,
    max_meshes_por_core: int = 4,
) -> list[Mesh]:
    """
    Divide as meshes ate haver trabalho para todos os processos e, depois,
    continua somente enquanto cada corte melhorar o balanceamento previsto.

    Parâmetros:
        meshes     : lista de Mesh lidas do arquivo
        n_cores    : número de núcleos MPI disponíveis
        margem_pct : % de desequilíbrio máximo aceito entre núcleos (default 10%)

    Retorna:
        lista de Mesh (pode ter mais itens que a original)
    """
    if n_cores <= 0:
        raise ValueError("n_cores deve ser maior que zero")

    resultado = deepcopy(meshes)
    margem_pct = max(0.0, float(margem_pct))
    max_meshes_por_core = max(1, int(max_meshes_por_core))
    limite_meshes = max(len(resultado), n_cores * max_meshes_por_core)
    max_iter = max(50, limite_meshes - len(resultado))

    def desequilibrio(lista: list[Mesh]) -> float:
        if len(lista) < n_cores:
            return float("inf")
        grupos = _particionar_contiguo_por_carga(lista, n_cores)
        return _desequilibrio_por_somas(_somas_grupos(grupos))

    for _ in range(max_iter):
        atual = desequilibrio(resultado)
        if len(resultado) >= n_cores and atual <= margem_pct:
            break
        if len(resultado) >= limite_meshes:
            logger.info(
                f"  Limite conservador de {limite_meshes} meshes atingido "
                f"({max_meshes_por_core} por processo)."
            )
            break

        candidatos = [
            (i, mesh) for i, mesh in enumerate(resultado)
            if _melhor_eixo(mesh) >= 0
        ]
        if not candidatos:
            logger.warning("Nenhuma mesh pode ser dividida mais. Encerrando divisao.")
            break

        candidatos.sort(key=lambda item: item[1].n_celulas, reverse=True)
        escolha = None

        if len(resultado) < n_cores:
            # Primeiro garante ao menos uma mesh por processo MPI.
            idx_cortar, mesh_cortar = candidatos[0]
            eixo = _melhor_eixo(mesh_cortar)
            m_a, m_b = _cortar_mesh(mesh_cortar, eixo)
            escolha = (idx_cortar, mesh_cortar, eixo, m_a, m_b, None)
        else:
            # Com processos ja alimentados, simula cada corte e escolhe apenas
            # aquele que reduz o desequilibrio. Isso evita criar fronteiras de
            # comunicacao que nao trazem ganho de carga.
            melhor_chave = None
            for idx_cortar, mesh_cortar in candidatos:
                eixo = _melhor_eixo(mesh_cortar)
                m_a, m_b = _cortar_mesh(mesh_cortar, eixo)
                tentativa = (
                    resultado[:idx_cortar] + [m_a, m_b] + resultado[idx_cortar + 1:]
                )
                novo_desbal = desequilibrio(tentativa)
                chave = (novo_desbal, max(m_a.n_celulas, m_b.n_celulas))
                if melhor_chave is None or chave < melhor_chave:
                    melhor_chave = chave
                    escolha = (
                        idx_cortar, mesh_cortar, eixo, m_a, m_b, novo_desbal,
                    )

            if escolha is None or escolha[5] >= atual - 1e-9:
                logger.info("  Nenhum corte adicional melhoraria o balanceamento.")
                break

        idx_cortar, mesh_cortar, eixo, m_a, m_b, _ = escolha
        nomes_eixo = ["X", "Y", "Z"]
        logger.info(
            f"  Cortando mesh #{mesh_cortar.id_original} no eixo {nomes_eixo[eixo]} "
            f"({mesh_cortar.ijk[eixo]} celulas -> "
            f"{mesh_cortar.ijk[eixo] // 2} + "
            f"{mesh_cortar.ijk[eixo] - mesh_cortar.ijk[eixo] // 2})"
        )
        resultado = resultado[:idx_cortar] + [m_a, m_b] + resultado[idx_cortar + 1:]

    if len(resultado) >= n_cores:
        desbal = desequilibrio(resultado)
        logger.info(f"  Desequilibrio entre processos: {desbal:.1f}% (limite: {margem_pct}%)")
        if desbal > margem_pct:
            logger.warning(
                f"  Melhor desequilibrio obtido ({desbal:.1f}%) ainda esta "
                f"acima da margem ({margem_pct}%)."
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


def _desequilibrio_por_somas(somas: list[int]) -> float:
    if not somas:
        return 0.0
    max_c = max(somas)
    min_c = min(somas)
    return (max_c - min_c) / max_c * 100 if max_c else 0.0


def _somas_grupos(grupos: list[list[Mesh]]) -> list[int]:
    return [sum(m.n_celulas for m in grp) for grp in grupos]


def _particionar_contiguo_por_carga(meshes: list[Mesh], n_cores: int) -> list[list[Mesh]]:
    """
    Particiona a ordem existente em blocos contiguos, minimizando a maior
    carga por processo. Nao reordena &MESH: em modelos com sobreposicao, a
    ordem pode participar da precedencia numerica do FDS.
    """
    if n_cores <= 0:
        raise ValueError("n_cores deve ser maior que zero")
    if not meshes:
        return [[] for _ in range(n_cores)]

    n_grupos = min(n_cores, len(meshes))
    pesos = [mesh.n_celulas for mesh in meshes]

    def grupos_necessarios(limite: int) -> int:
        grupos = 1
        soma = 0
        for peso in pesos:
            if soma and soma + peso > limite:
                grupos += 1
                soma = peso
            else:
                soma += peso
        return grupos

    baixo = max(max(pesos), math.ceil(sum(pesos) / n_grupos))
    alto = sum(pesos)
    while baixo < alto:
        meio = (baixo + alto) // 2
        if grupos_necessarios(meio) <= n_grupos:
            alto = meio
        else:
            baixo = meio + 1
    limite_otimo = baixo

    grupos: list[list[Mesh]] = [[] for _ in range(n_grupos)]
    idx_grupo = n_grupos - 1
    soma = 0
    for idx_mesh in range(len(meshes) - 1, -1, -1):
        mesh = meshes[idx_mesh]
        # Deixa ao menos uma mesh para cada grupo ainda a esquerda.
        precisa_separar = (
            bool(grupos[idx_grupo])
            and (
                soma + mesh.n_celulas > limite_otimo
                or idx_mesh + 1 <= idx_grupo
            )
        )
        if precisa_separar:
            idx_grupo -= 1
            soma = 0
        grupos[idx_grupo].append(mesh)
        soma += mesh.n_celulas

    for grupo in grupos:
        grupo.reverse()

    if n_grupos < n_cores:
        grupos.extend([[] for _ in range(n_cores - n_grupos)])
    return grupos


def _grupos_por_mpi_process(meshes: list[Mesh], n_cores: int) -> list[list[Mesh]] | None:
    grupos = [[] for _ in range(n_cores)]
    for mesh in meshes:
        if mesh.mpi_process is None:
            return None
        if mesh.mpi_process < 0 or mesh.mpi_process >= n_cores:
            return None
        grupos[mesh.mpi_process].append(mesh)
    return grupos


def meshes_ja_otimizadas(meshes: list[Mesh], n_cores: int, margem_pct: float) -> bool:
    if len(meshes) < n_cores:
        return False

    grupos = _grupos_por_mpi_process(meshes, n_cores)
    if grupos is None:
        return False
    if any(not grp for grp in grupos):
        return False

    # O proprio FDS Automation grava um bloco contiguo por MPI_PROCESS.
    # Carga equilibrada com ranks intercalados nao deve ser tratada como o
    # formato canonico, pois uma nova execucao pode depender dessa ordenacao.
    processos = [mesh.mpi_process for mesh in meshes]
    if processos != sorted(processos):
        logger.info("  MPI_PROCESS existente, mas fora de blocos contiguos.")
        return False

    somas = _somas_grupos(grupos)
    desbal = _desequilibrio_por_somas(somas)
    logger.info(f"  MPI_PROCESS existente: desequilíbrio {desbal:.1f}%")
    return desbal <= margem_pct


def _diagonal_dominio(meshes: list[Mesh]) -> float:
    if not meshes:
        return 1.0

    xmin = min(m.xb[0] for m in meshes)
    xmax = max(m.xb[1] for m in meshes)
    ymin = min(m.xb[2] for m in meshes)
    ymax = max(m.xb[3] for m in meshes)
    zmin = min(m.xb[4] for m in meshes)
    zmax = max(m.xb[5] for m in meshes)
    diagonal = math.sqrt((xmax - xmin) ** 2 + (ymax - ymin) ** 2 + (zmax - zmin) ** 2)
    return diagonal or 1.0


def _distancia_normalizada_ao_grupo(mesh: Mesh, grupo: list[Mesh], diagonal: float) -> float:
    if not grupo:
        return 0.0

    cx, cy, cz = mesh.centro
    menor = min(
        math.sqrt((cx - gx) ** 2 + (cy - gy) ** 2 + (cz - gz) ** 2)
        for gx, gy, gz in (m.centro for m in grupo)
    )
    return menor / diagonal


def _montar_grupos_balanceados(
    meshes: list[Mesh],
    n_cores: int,
    peso_espacial: float,
) -> list[list[Mesh]]:
    grupos = [[] for _ in range(n_cores)]
    somas = [0] * n_cores
    total = sum(m.n_celulas for m in meshes)
    alvo = max(1.0, total / max(1, n_cores))
    diagonal = _diagonal_dominio(meshes)

    for mesh in sorted(meshes, key=lambda m: m.n_celulas, reverse=True):
        vazios = [i for i, grp in enumerate(grupos) if not grp]
        if vazios:
            idx = vazios[0]
        else:
            def score(i: int) -> float:
                carga = (somas[i] + mesh.n_celulas) / alvo
                distancia = _distancia_normalizada_ao_grupo(mesh, grupos[i], diagonal)
                return carga + peso_espacial * distancia

            idx = min(range(n_cores), key=score)

        grupos[idx].append(mesh)
        somas[idx] += mesh.n_celulas

    return grupos


# ---------------------------------------------------------------------------
# Reordenação das meshes para atribuição crescente por núcleo
# ---------------------------------------------------------------------------
def reordenar_meshes_por_nucleo(
    meshes: list[Mesh],
    n_cores: int,
    peso_espacial: float = 0.15,
) -> list[Mesh]:
    """
    Preserva a ordem dos namelists &MESH e atribui MPI_PROCESS em blocos
    contiguos, minimizando a maior carga acumulada por processo.

    O parametro peso_espacial e mantido por compatibilidade de configuracao;
    nao e usado para mover meshes, pois reordenar entradas pode alterar a
    precedencia de meshes sobrepostas no FDS.
    """
    grupos = _particionar_contiguo_por_carga(meshes, n_cores)

    for idx_proc, grp in enumerate(grupos):
        for mesh in grp:
            mesh.mpi_process = idx_proc

    # Log de distribuição
    sep = "-" * 55
    logger.info(sep)
    logger.info("  DISTRIBUIÇÃO DE MESHES POR NÚCLEO")
    logger.info(sep)
    for i, grp in enumerate(grupos):
        total_celulas = sum(m.n_celulas for m in grp)
        logger.info(f"  Núcleo {i:>3}: {len(grp):>3} mesh(es)  |  {total_celulas:>10} células")
    logger.info(sep)

    # A concatenacao devolve exatamente a ordem de entrada, pois os grupos
    # sao fatias contiguas do vetor original.
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
    if not novas_meshes:
        raise ValueError("Reescrita recusada: a lista de meshes esta vazia.")

    with open(caminho_fds, 'r', encoding='latin-1') as f:
        conteudo = f.read()

    # Cada mesh nova deve apontar para uma entrada que realmente existe. Ao
    # substituir uma a uma, filhas de uma divisao permanecem no ponto da mesh
    # mae e registros intercalados (&OBST, &VENT...) nao mudam de posicao.
    correspondencias = encontrar_namelists(conteudo, "MESH")
    total_originais = len(correspondencias)
    por_original: dict[int, list[Mesh]] = {}
    for mesh in novas_meshes:
        if mesh.id_original < 1 or mesh.id_original > total_originais:
            raise ValueError(
                f"Reescrita recusada: mesh referencia origem inexistente "
                f"#{mesh.id_original}."
            )
        por_original.setdefault(mesh.id_original, []).append(mesh)

    faltantes = [
        idx for idx in range(1, total_originais + 1)
        if idx not in por_original
    ]
    if faltantes:
        raise ValueError(
            "Reescrita recusada: faltam substitutas para a(s) mesh(es) "
            + ", ".join(f"#{idx}" for idx in faltantes)
            + "."
        )

    # Reconstrucao pelos spans do scanner: somente namelists reais sao
    # substituidos. Texto "&MESH" comentado e barras dentro de strings ficam
    # fora dos spans e permanecem byte a byte no restante do arquivo.
    partes: list[str] = []
    cursor = 0
    for indice_original, bloco in enumerate(correspondencias, start=1):
        partes.append(conteudo[cursor:bloco.inicio])
        partes.append("\n".join(
            mesh.para_linha_fds() for mesh in por_original[indice_original]
        ))
        cursor = bloco.fim
    partes.append(conteudo[cursor:])
    novo_conteudo = "".join(partes)

    # O primeiro backup representa o modelo recebido do usuario e nunca deve
    # ser sobrescrito por uma versao que o proprio programa ja modificou.
    # Ele so e criado depois de todas as validacoes acima.
    caminho_bak = caminho_fds + ".bak"
    if not os.path.exists(caminho_bak):
        shutil.copy2(caminho_fds, caminho_bak)
        logger.info(f"  Backup original criado: {os.path.basename(caminho_bak)}")
    else:
        logger.info(f"  Backup original preservado: {os.path.basename(caminho_bak)}")

    # Escrita atomica: uma queda no meio da gravacao deixa o .fds anterior
    # intacto. O temporario fica no mesmo volume para os.replace ser atomico.
    temporario = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="latin-1", newline="", delete=False,
            dir=os.path.dirname(os.path.abspath(caminho_fds)),
            prefix=os.path.basename(caminho_fds) + ".", suffix=".tmp",
        ) as f:
            temporario = f.name
            f.write(novo_conteudo)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporario, caminho_fds)
        temporario = None
    finally:
        if temporario:
            try:
                os.remove(temporario)
            except OSError:
                pass

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
    pular_se_otimizado: bool = True,
    peso_espacial: float = 0.15,
    max_meshes_por_core: int = 4,
    permitir_divisao: bool = False,
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

    # MULT_ID e CATF so revelam a lista completa depois do pre-processamento
    # interno do FDS. Alterar MPI_PROCESS com a contagem literal poderia
    # atribuir todas as copias ao mesmo rank ou ignorar meshes auxiliares.
    motivos_indeterminados = motivos_contagem_mesh_indeterminada(caminho_fds)
    if motivos_indeterminados:
        logger.warning(
            "  Mesh Splitter ignorado por %s; arquivo original preservado.",
            "/".join(motivos_indeterminados),
        )
        return len(meshes_originais)

    if permitir_divisao:
        motivos_bloqueio = _motivos_bloqueio_divisao(
            caminho_fds, meshes_originais
        )
        if motivos_bloqueio:
            logger.warning(
                "  Divisao avancada desativada neste caso: %s. "
                "O balanceamento sem criar interfaces continua disponivel.",
                "; ".join(motivos_bloqueio),
            )
            permitir_divisao = False

    if pular_se_otimizado and meshes_ja_otimizadas(meshes_originais, n_cores, margem_pct):
        logger.info("  Meshes ja otimizadas para esta configuracao. Reescrita ignorada.")
        return len(meshes_originais)

    if permitir_divisao:
        # Mesmo quando ja ha meshes suficientes, um subdominio muito maior
        # pode justificar cortes adicionais. Cada corte cria uma nova
        # interface numerica; por isso o fluxo principal controla esta opcao
        # separadamente e a mantem avancada/opt-in.
        novas_meshes = dividir_meshes(
            meshes_originais,
            n_cores,
            margem_pct,
            max_meshes_por_core=max_meshes_por_core,
        )
    else:
        novas_meshes = deepcopy(meshes_originais)
        if len(novas_meshes) < n_cores:
            logger.warning(
                f"  Ha {len(novas_meshes)} mesh(es) para {n_cores} processos. "
                "A criacao de novas interfaces esta desativada; o arquivo nao "
                "sera reescrito e o chamador deve reduzir os processos MPI."
            )
            return len(novas_meshes)
        else:
            logger.info("  Divisao de meshes desativada; apenas balanceando MPI_PROCESS.")
    novas_meshes = reordenar_meshes_por_nucleo(novas_meshes, n_cores, peso_espacial)

    logger.info(f"  Meshes finais    : {len(novas_meshes)}")
    logger.info(f"  Células totais   : {sum(m.n_celulas for m in novas_meshes)}")

    if not dry_run:
        reescrever_fds_com_meshes(caminho_fds, novas_meshes)
    else:
        logger.info("  [DRY-RUN] Arquivo NÃO modificado.")

    return len(novas_meshes)
