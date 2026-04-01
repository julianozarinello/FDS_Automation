#Subrotina responsável por abrir e rodar as simulações FDS
import subprocess
import os
import time
import shutil

def run_fds(caminho_fds_exe, arquivo_fds):
    """
    Sub-rotina para disparar o motor de cálculo do FDS.
    """
    # Verifica se o arquivo .fds realmente existe antes de começar
    if not os.path.exists(arquivo_fds):
        print(f"Erro: O arquivo {arquivo_fds} não foi encontrado!")
        return False

    # Define a pasta onde os resultados serão salvos (mesma pasta do arquivo .fds)
    diretorio_trabalho = os.path.dirname(os.path.abspath(arquivo_fds))
    
    print(f"\n[INFO] Iniciando simulação: {os.path.basename(arquivo_fds)}")
    inicio = time.time()

    try:
        # O comando enviado ao Windows: fds.exe arquivo.fds
        # O 'cwd' garante que o FDS crie os arquivos de saída na pasta correta
        subprocess.run([caminho_fds_exe, arquivo_fds], cwd=diretorio_trabalho, check=True)
        
        fim = time.time()
        tempo_total = (fim - inicio) / 60
        print(f"[SUCESSO] Simulação concluída em {tempo_total:.2f} minutos.")
        return True

    except subprocess.CalledProcessError as e:
        print(f"[ERRO] O FDS parou com erro: {e}")
        return False

def preparar_pasta_simulacao(diretorio_fds, pasta_destino):
    """
    Copia todas as DLLs da pasta do PyroSim para a pasta onde o .fds está.
    """
    print("[LIMPEZA] Organizando as DLLs na pasta de destino...")
    
    # Lista de arquivos comuns que o FDS precisa (as peças do motor)
    for arquivo in os.listdir(diretorio_fds):
        if arquivo.endswith(".dll"):
            origem = os.path.join(diretorio_fds, arquivo)
            destino = os.path.join(pasta_destino, arquivo)
            
            # Só copia se o arquivo ainda não estiver lá (para ser mais rápido)
            if not os.path.exists(destino):
                shutil.copy2(origem, destino)
                print(f"  > Copiado: {arquivo}")

def preparar_ambiente_fds(diretorio_base_fds, pasta_projeto):
    """
    Copia o motor (FDS) e TODAS as DLLs (incluindo as da pasta MPI)
    para a pasta onde a simulação vai rodar.
    """
    print(f"[PREPARAÇÃO] Coletando componentes em: {diretorio_base_fds}")
    
    # Lista de pastas onde o PyroSim esconde as peças do motor
    pastas_para_buscar = [
        diretorio_base_fds,              # Pasta principal (\fds)
        os.path.join(diretorio_base_fds, "mpi") # Pasta das DLLs MPI (\fds\mpi)
    ]
    
    for pasta in pastas_para_buscar:
        if not os.path.exists(pasta):
            continue
            
        for item in os.listdir(pasta):
            # Queremos o fds.exe e todas as .dll
            if item.lower().endswith(('.exe', '.dll')):
                origem = os.path.join(pasta, item)
                destino = os.path.join(pasta_projeto, item)
                
                if not os.path.exists(destino):
                    shutil.copy2(origem, destino)
                    print(f"  > Componente preparado: {item}")