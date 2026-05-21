# Subrotina responsável por preparar o ambiente e ler o arquivo FDS
import os
import re
import shutil
import logging

logger = logging.getLogger(__name__)

_RE_MESH = re.compile(r'&MESH\b(.*?)/', re.IGNORECASE | re.DOTALL)
_RE_MPI_PROCESS = re.compile(r'\bMPI_PROCESS\s*=', re.IGNORECASE)


def preparar_ambiente_fds(diretorio_base_fds: str, pasta_projeto: str) -> None:
    """
    Copia executáveis, DLLs e mpiexec para a pasta do projeto.
    Arquivos já existentes na pasta de destino são ignorados.
    """
    pastas_para_buscar = [
        diretorio_base_fds,
        os.path.join(diretorio_base_fds, "mpi"),
    ]

    copiados = 0
    for pasta in pastas_para_buscar:
        if not os.path.exists(pasta):
            logger.warning(f"Pasta não encontrada, pulando: {pasta}")
            continue
        for item in os.listdir(pasta):
            if item.lower().endswith(('.exe', '.dll')):
                origem  = os.path.join(pasta, item)
                destino = os.path.join(pasta_projeto, item)
                if not os.path.exists(destino):
                    shutil.copy2(origem, destino)
                    logger.debug(f"Copiado: {item}")
                    copiados += 1

    if copiados > 0:
        logger.info(f"{copiados} arquivo(s) copiado(s) para a pasta do projeto.")
    else:
        logger.info("Binários FDS já presentes. Nenhuma cópia necessária.")


def contar_meshes(caminho_arquivo: str) -> int:
    """
    Lê o arquivo .fds e conta quantas linhas começam com &MESH.
    Retorna 0 se o arquivo não for encontrado.
    """
    if not os.path.exists(caminho_arquivo):
        logger.error(f"Arquivo não encontrado: {caminho_arquivo}")
        return 0

    contador = 0
    with open(caminho_arquivo, 'r', encoding='latin-1') as f:
        for linha in f:
            if linha.strip().upper().startswith('&MESH'):
                contador += 1

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
        for mesh in _RE_MESH.finditer(conteudo)
        if _RE_MPI_PROCESS.search(mesh.group(1))
    )
