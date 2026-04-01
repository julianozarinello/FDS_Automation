from fds_reader import preparar_ambiente_fds
import os
import subprocess
import shutil

def main():
    

    # 1. Caminhos Originais
    
    diretorio_fds_origem = r"C:\Program Files\PyroSim 2023\fds"
    C_ARQUIVO_FDS = r"C:\Users\Aluga.com\Documents\Programas\Pyrosim teste\FDS\Pyrosim.fds"
    
    pasta_projeto = os.path.dirname(C_ARQUIVO_FDS)
    nome_fds = os.path.basename(C_ARQUIVO_FDS)

    # 2. Executa a cópia "inteligente" (incluindo a pasta mpi)
    preparar_ambiente_fds(diretorio_fds_origem, pasta_projeto)

    # 3. Agora o fds.exe está LOCAL, cercado de todas as suas DLLs
    C_FDS_LOCAL = os.path.join(pasta_projeto, "fds.exe")

    print("\n=== INICIANDO SIMULAÇÃO FDS VIA PYTHON ===")
    
    try:
        # Rodamos o FDS localmente
        subprocess.run([C_FDS_LOCAL, nome_fds], cwd=pasta_projeto, check=True)
        print("\n[FINALIZADO] Simulação concluída com sucesso!")
    except subprocess.CalledProcessError as e:
        print(f"\n[ERRO] O FDS fechou com problemas. Verifique o arquivo .out na pasta.")

main ()