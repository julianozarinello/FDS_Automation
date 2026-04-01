from fds_reader import preparar_ambiente_fds, contar_meshes
import os
import subprocess
import shutil

def main():
    # --- PAINEL DE CONTROLE DO USUÁRIO ---
    MODO = "OPENMP"        # Opções: "SERIAL", "OPENMP", "MPI"
    NUM_CORES = 20       # Usado no OPENMP (threads) ou no MPI (processos)
    # -------------------------------------

    diretorio_fds_origem = r"C:\Program Files\PyroSim 2023\fds"
    C_ARQUIVO_FDS = r"C:\Users\Aluga.com\Documents\Programas\Pyrosim teste\FDS\Pyrosim.fds"
    
    pasta_projeto = os.path.dirname(C_ARQUIVO_FDS)
    nome_fds = os.path.basename(C_ARQUIVO_FDS)

    preparar_ambiente_fds(diretorio_fds_origem, pasta_projeto)

    # LÓGICA DE SELEÇÃO DE MOTOR
    if MODO == "OPENMP":
        comando = [os.path.join(pasta_projeto, "fds_openmp.exe"), nome_fds]
        os.environ["OMP_NUM_THREADS"] = str(NUM_CORES)
        status_msg = f"Iniciando OPENMP com {NUM_CORES} threads..."

    elif MODO == "MPI":
        # No MPI, chamamos o mpiexec que gerencia os processos do fds.exe
        C_MPIEXEC = os.path.join(pasta_projeto, "mpiexec.exe")
        C_FDS = os.path.join(pasta_projeto, "fds.exe")
        
        # Comando: mpiexec -n [numero] fds.exe arquivo.fds
        comando = [C_MPIEXEC, "-n", str(NUM_CORES), C_FDS, nome_fds]
        
        # No MPI puro, cada processo usa 1 thread
        os.environ["OMP_NUM_THREADS"] = "1"
        status_msg = f"Iniciando MPI com {NUM_CORES} processos..."

    else: # SERIAL
        comando = [os.path.join(pasta_projeto, "fds.exe"), nome_fds]
        os.environ["OMP_NUM_THREADS"] = "1"
        status_msg = "Iniciando modo SERIAL..."

    print(f"\n=== GERENCIADOR DE SIMULAÇÕES ===\n[INFO] {status_msg}")
    
    try:
        subprocess.run(comando, cwd=pasta_projeto, check=True)
        print("\n[SUCESSO] Simulação finalizada!")
    except subprocess.CalledProcessError:
        print("\n[ERRO] A simulação falhou. Verifique se o MPI está instalado corretamente.")

if __name__ == "__main__":
    main()