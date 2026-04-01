#Subrotina responsável por abrir e rodar as simulações FDS
import subprocess
import os
import time
import shutil

def preparar_ambiente_fds(diretorio_base_fds, pasta_projeto):
    """Copia o executável, DLLs e o mpiexec para a pasta do projeto."""
    pastas_para_buscar = [diretorio_base_fds, os.path.join(diretorio_base_fds, "mpi")]
    for pasta in pastas_para_buscar:
        if os.path.exists(pasta):
            for item in os.listdir(pasta):
                if item.lower().endswith(('.exe', '.dll')):
                    shutil.copy2(os.path.join(pasta, item), os.path.join(pasta_projeto, item))

def contar_meshes(caminho_arquivo):
    """
    Lê o arquivo .fds e conta quantas linhas começam com &MESH.
    """
    if not os.path.exists(caminho_arquivo):
        print(f"[ERRO] Arquivo não encontrado: {caminho_arquivo}")
        return 0

    contador = 0
    
    # Abrimos o arquivo para leitura ('r') com codificação latin-1 
    # (comum em arquivos gerados por softwares de engenharia)
    with open(caminho_arquivo, 'r', encoding='latin-1') as f:
        for linha in f:
            # Remove espaços em branco e transforma em maiúsculas para não errar
            linha_limpa = linha.strip().upper()
            
            # Se a linha começa com &MESH, incrementamos o contador
            if linha_limpa.startswith('&MESH'):
                contador += 1
                
    return contador