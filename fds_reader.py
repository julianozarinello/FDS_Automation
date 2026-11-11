# Subrotina responsável por preparar o ambiente e ler o arquivo FDS
import os
import re
import shutil
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class BlocoNamelist:
    """Trecho de um namelist ``&NOME ... /`` dentro do arquivo FDS."""

    inicio: int
    fim: int
    corpo_inicio: int
    corpo_fim: int
    texto: str
    corpo: str


def _inicia_namelist(conteudo: str, posicao: int, marcador: str) -> bool:
    fim_nome = posicao + len(marcador)
    if conteudo[posicao:fim_nome].upper() != marcador:
        return False
    return fim_nome >= len(conteudo) or not (
        conteudo[fim_nome].isalnum() or conteudo[fim_nome] == "_"
    )


def encontrar_namelists(conteudo: str, nome: str) -> list[BlocoNamelist]:
    """
    Localiza namelists pelo nome sem confundir comentarios ou strings.

    No namelist Fortran, ``!`` inicia comentario ate o fim da linha e ``/``
    encerra o bloco somente fora de aspas. Aspas duplicadas (``''``/``""``)
    sao tratadas como um caractere literal. Um bloco iniciado e sem terminador
    e recusado para impedir que uma regravacao use apenas parte do arquivo.
    """
    nome_normalizado = str(nome or "").strip().lstrip("&").upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", nome_normalizado):
        raise ValueError(f"Nome de namelist invalido: {nome!r}")
    marcador = "&" + nome_normalizado

    blocos: list[BlocoNamelist] = []
    tamanho = len(conteudo)
    i = 0
    aspas: str | None = None
    comentario = False

    while i < tamanho:
        caractere = conteudo[i]

        if comentario:
            if caractere in "\r\n":
                comentario = False
            i += 1
            continue

        if aspas is not None:
            if caractere == aspas:
                if i + 1 < tamanho and conteudo[i + 1] == aspas:
                    i += 2
                    continue
                aspas = None
            i += 1
            continue

        if caractere == "!":
            comentario = True
            i += 1
            continue
        if caractere in ("'", '"'):
            aspas = caractere
            i += 1
            continue
        if caractere != "&" or not _inicia_namelist(conteudo, i, marcador):
            i += 1
            continue

        inicio = i
        corpo_inicio = i + len(marcador)
        j = corpo_inicio
        aspas_mesh: str | None = None
        comentario_mesh = False
        while j < tamanho:
            atual = conteudo[j]
            if comentario_mesh:
                if atual in "\r\n":
                    comentario_mesh = False
                j += 1
                continue
            if aspas_mesh is not None:
                if atual == aspas_mesh:
                    if j + 1 < tamanho and conteudo[j + 1] == aspas_mesh:
                        j += 2
                        continue
                    aspas_mesh = None
                j += 1
                continue
            if atual == "!":
                comentario_mesh = True
                j += 1
                continue
            if atual in ("'", '"'):
                aspas_mesh = atual
                j += 1
                continue
            if atual == "/":
                fim = j + 1
                blocos.append(
                    BlocoNamelist(
                        inicio=inicio,
                        fim=fim,
                        corpo_inicio=corpo_inicio,
                        corpo_fim=j,
                        texto=conteudo[inicio:fim],
                        corpo=conteudo[corpo_inicio:j],
                    )
                )
                i = fim
                break
            j += 1
        else:
            linha = conteudo.count("\n", 0, inicio) + 1
            raise ValueError(
                f"Namelist {marcador} iniciado na linha {linha} nao possui "
                "terminador '/' fora de comentarios/aspas."
            )

    return blocos


def encontrar_namelists_mesh(conteudo: str) -> list[BlocoNamelist]:
    """Atalho compativel para localizar blocos ``&MESH``."""
    return encontrar_namelists(conteudo, "MESH")


def remover_comentarios_fortran(texto: str) -> str:
    """Remove comentarios ``!`` preservando strings e posicoes do texto."""
    caracteres = list(texto)
    aspas: str | None = None
    comentario = False
    i = 0
    while i < len(caracteres):
        atual = caracteres[i]
        if comentario:
            if atual in "\r\n":
                comentario = False
            else:
                caracteres[i] = " "
            i += 1
            continue
        if aspas is not None:
            if atual == aspas:
                if i + 1 < len(caracteres) and caracteres[i + 1] == aspas:
                    i += 2
                    continue
                aspas = None
            i += 1
            continue
        if atual == "!":
            caracteres[i] = " "
            comentario = True
        elif atual in ("'", '"'):
            aspas = atual
        i += 1
    return "".join(caracteres)


def mascarar_strings_fortran(texto: str) -> str:
    """Substitui strings por espacos, preservando comprimento e posicoes."""
    caracteres = list(texto)
    aspas: str | None = None
    i = 0
    while i < len(caracteres):
        atual = caracteres[i]
        if aspas is None:
            if atual in ("'", '"'):
                aspas = atual
                caracteres[i] = " "
            i += 1
            continue

        caracteres[i] = " "
        if atual == aspas:
            if i + 1 < len(caracteres) and caracteres[i + 1] == aspas:
                caracteres[i + 1] = " "
                i += 2
                continue
            aspas = None
        i += 1
    return "".join(caracteres)


def namelist_tem_campo(corpo: str, nome_campo: str) -> bool:
    """Informa se ``NOME=`` aparece ativo, e nao em comentario/string."""
    nome = str(nome_campo or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_%]*", nome):
        raise ValueError(f"Nome de campo invalido: {nome_campo!r}")
    limpo = remover_comentarios_fortran(corpo)
    mascara = mascarar_strings_fortran(limpo)
    return bool(
        re.search(
            rf"(?<![A-Z0-9_%]){re.escape(nome)}(?:\s*\([^)]*\))?\s*=",
            mascara,
            re.IGNORECASE,
        )
    )


def motivos_contagem_mesh_indeterminada(caminho_arquivo: str) -> list[str]:
    """Aponta recursos que tornam a contagem literal de MESH incompleta.

    ``MULT_ID`` expande uma linha em varias meshes. ``CATF`` concatena outros
    arquivos antes de o FDS ler o caso. Sem executar esse pre-processamento,
    nao se pode limitar processos MPI com base apenas no arquivo principal.
    """
    if not os.path.exists(caminho_arquivo):
        return []
    with open(caminho_arquivo, "r", encoding="latin-1") as f:
        conteudo = f.read()

    motivos: list[str] = []
    if encontrar_namelists(conteudo, "CATF"):
        motivos.append("CATF")
    if any(
        namelist_tem_campo(bloco.corpo, "MULT_ID")
        for bloco in encontrar_namelists(conteudo, "MESH")
    ):
        motivos.append("MULT_ID")
    return motivos


def _binario_precisa_atualizar(origem: str, destino: str) -> bool:
    """Compara metadados baratos para evitar binarios locais obsoletos."""
    if not os.path.exists(destino):
        return True
    try:
        src = os.stat(origem)
        dst = os.stat(destino)
        return src.st_size != dst.st_size or src.st_mtime_ns > dst.st_mtime_ns
    except OSError:
        return True


def preparar_ambiente_fds(diretorio_base_fds: str, pasta_projeto: str) -> None:
    """
    Copia executáveis, DLLs e mpiexec para a pasta do projeto.
    Arquivos ausentes ou mais antigos na pasta de destino são atualizados.
    """
    pastas_para_buscar = [
        diretorio_base_fds,
        os.path.join(diretorio_base_fds, "mpi"),
    ]

    copiados = 0
    atualizados = 0
    for pasta in pastas_para_buscar:
        if not os.path.exists(pasta):
            logger.warning(f"Pasta não encontrada, pulando: {pasta}")
            continue
        for item in os.listdir(pasta):
            if item.lower().endswith(('.exe', '.dll')):
                origem  = os.path.join(pasta, item)
                destino = os.path.join(pasta_projeto, item)
                existia = os.path.exists(destino)
                if _binario_precisa_atualizar(origem, destino):
                    shutil.copy2(origem, destino)
                    logger.debug(f"{'Atualizado' if existia else 'Copiado'}: {item}")
                    if existia:
                        atualizados += 1
                    else:
                        copiados += 1

    if copiados or atualizados:
        logger.info(
            f"Binarios FDS preparados: {copiados} novo(s), "
            f"{atualizados} atualizado(s)."
        )
    else:
        logger.info("Binários FDS já presentes. Nenhuma cópia necessária.")


def contar_meshes(caminho_arquivo: str) -> int:
    """
    Le o arquivo .fds e conta namelists &MESH reais, inclusive multi-linha.
    Ocorrencias dentro de comentarios ou strings sao ignoradas.
    Retorna 0 se o arquivo não for encontrado.
    """
    if not os.path.exists(caminho_arquivo):
        logger.error(f"Arquivo não encontrado: {caminho_arquivo}")
        return 0

    with open(caminho_arquivo, 'r', encoding='latin-1') as f:
        conteudo = f.read()

    contador = len(encontrar_namelists(conteudo, "MESH"))

    logger.info(f"Meshes detectadas: {contador}")
    return contador


def contar_meshes_com_mpi_process(caminho_arquivo: str) -> int:
    """
    Conta quantas entradas &MESH possuem MPI_PROCESS definido.
    Funciona tambem para MESH multi-linha.
    """
    if not os.path.exists(caminho_arquivo):
        logger.error(f"Arquivo nao encontrado: {caminho_arquivo}")
        return 0

    with open(caminho_arquivo, 'r', encoding='latin-1') as f:
        conteudo = f.read()

    return sum(
        1
        for mesh in encontrar_namelists(conteudo, "MESH")
        if namelist_tem_campo(mesh.corpo, "MPI_PROCESS")
    )
