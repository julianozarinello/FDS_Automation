# menu.py — Menu numerado amigável para o FDS Automation
# Camada simples sobre o config.yaml/CLI existente (main.py), pensada para
# usuários de engenharia que não programam. Não substitui main.py: uso
# avançado/scriptado continua via `python main.py --config config.yaml`.
import os
import re
import sys
import traceback
from copy import deepcopy

# Forca console/saida em UTF-8 antes de qualquer print com emoji (✅❌⚠️).
# Sem isso, o .exe empacotado quebra com UnicodeEncodeError em PCs com
# codepage padrao do Windows (cp1252/ANSI), que nao tem esses caracteres.
if sys.platform == "win32":
    try:
        os.system("chcp 65001 > nul")
    except Exception:
        pass
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import main as fds_main
import path_detector
import firewall_config
from hw_detector import detectar_hardware, recomendar_config, logar_hardware

# True depois que o usuario rodar "Testar melhor configuracao" nesta sessao.
# Usado para decidir se "Executar fila" deve perguntar o modo de
# processamento antes de rodar (quem ja testou nao precisa escolher de novo).
_otimizador_testado = False

# Cache do status do firewall para o cabecalho do menu (a consulta ao
# netsh leva ~1s na primeira vez). None = ainda nao consultado.
_status_firewall_cache: str | None = None


# ---------------------------------------------------------------------------
# Resumo de status para o cabecalho do menu
# ---------------------------------------------------------------------------
def _carregar_cfg_silencioso() -> dict:
    try:
        return fds_main.carregar_config("config.yaml") or {}
    except Exception:
        return {}


def _contar_casos(pasta: str) -> int:
    """Conta arquivos .fds na pasta (recursivo), sem logar nada."""
    try:
        total = 0
        for _raiz, _dirs, nomes in os.walk(pasta):
            total += sum(1 for n in nomes if n.lower().endswith(".fds"))
        return total
    except Exception:
        return 0


def _diretorio_fds_atual(cfg: dict) -> str:
    diretorio = str(cfg.get("diretorio_fds", "") or "")
    if os.path.isdir(diretorio):
        return diretorio
    try:
        for pasta in path_detector.detect_fds_paths() + path_detector.detect_pyrosim_paths():
            if os.path.exists(os.path.join(pasta, "fds.exe")):
                return pasta
    except Exception:
        pass
    return ""


def _encurtar_caminho(caminho: str, largura: int = 45) -> str:
    if len(caminho) <= largura:
        return caminho
    return "..." + caminho[-(largura - 3):]


def _resumo_status() -> list[str]:
    """Monta as linhas de status exibidas no topo do menu."""
    global _status_firewall_cache
    cfg = _carregar_cfg_silencioso()

    pasta = str(cfg.get("pasta_simulacoes", "") or "")
    if pasta and os.path.isdir(pasta):
        n_casos = _contar_casos(pasta)
        linha_pasta = f"  📁 Pasta das simulações : {_encurtar_caminho(pasta)}"
        linha_casos = f"     Casos prontos p/ rodar: {n_casos}" + (
            "  ⚠️ (nenhum .fds encontrado)" if n_casos == 0 else ""
        )
    else:
        linha_pasta = "  📁 Pasta das simulações : (ainda não escolhida — use a opção 2)"
        linha_casos = ""

    diretorio_fds = _diretorio_fds_atual(cfg)
    if diretorio_fds:
        linha_fds = "  🔥 Programa FDS         : ✅ encontrado"
    else:
        linha_fds = "  🔥 Programa FDS         : ❌ não encontrado (use a opção 6)"

    if _status_firewall_cache is None and diretorio_fds:
        _status_firewall_cache = firewall_config.status_firewall(diretorio_fds)
    if _status_firewall_cache == "liberado":
        linha_fw = "  🛡️ Perguntas do Windows : ✅ liberado (não vai perguntar)"
    elif _status_firewall_cache == "pendente":
        linha_fw = "  🛡️ Perguntas do Windows : ⚠️ pendente (use a opção 4)"
    else:
        linha_fw = ""

    return [l for l in (linha_pasta, linha_casos, linha_fds, linha_fw) if l]


def limpar_tela():
    """Limpa o terminal independentemente do sistema operacional."""
    os.system('cls' if os.name == 'nt' else 'clear')


def _atualizar_pasta_simulacoes(caminho_config: str, nova_pasta: str) -> None:
    """
    Reescreve apenas a linha 'pasta_simulacoes:' do config.yaml, preservando
    o restante do arquivo (comentários, demais seções) intacto.
    """
    with open(caminho_config, "r", encoding="utf-8") as f:
        texto = f.read()

    linha_nova = f'pasta_simulacoes: "{nova_pasta}"'
    novo_texto, n = re.subn(
        r'^pasta_simulacoes:.*$', linha_nova, texto, count=1, flags=re.MULTILINE
    )
    if n == 0:
        novo_texto = texto.rstrip() + f"\n\n{linha_nova}\n"

    with open(caminho_config, "w", encoding="utf-8") as f:
        f.write(novo_texto)


def opcao_selecionar_pasta():
    limpar_tela()
    print("=" * 55)
    print("  SELEÇÃO DA PASTA DE SIMULAÇÕES")
    print("=" * 55 + "\n")
    print("Selecione a pasta raiz que contém as subpastas de cada")
    print("simulação (cada subpasta com seu arquivo .fds dentro).\n")

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    pasta = filedialog.askdirectory(title="Selecione a pasta raiz com os casos FDS")
    root.destroy()

    if not pasta:
        print("⚠️  Seleção cancelada.")
        input("\nPressione ENTER para voltar ao menu...")
        return

    pasta = pasta.replace("\\", "/")
    _atualizar_pasta_simulacoes("config.yaml", pasta)

    cfg = fds_main.carregar_config("config.yaml")
    try:
        simulacoes = fds_main.descobrir_simulacoes(cfg)
    except Exception as e:
        print(f"\n❌ Erro ao detectar casos .fds: {e}")
        input("\nPressione ENTER para voltar ao menu...")
        return

    print(f"\n📁 Pasta definida no config.yaml: {pasta}")
    print(f"✅ {len(simulacoes)} arquivo(s) .fds encontrado(s):\n")
    for s in simulacoes:
        print(f"   - {os.path.relpath(s, pasta)}")

    input("\nPressione ENTER para voltar ao menu...")


def opcao_testar_configuracao():
    global _otimizador_testado
    limpar_tela()
    cfg = fds_main.carregar_config("config.yaml")
    fds_main.testar_melhor_configuracao(cfg)
    _otimizador_testado = True
    print('\n')
    input("Pressione ENTER para voltar ao menu...")


def _escolher_processamento_manual(cfg: dict) -> dict:
    cfg = deepcopy(cfg)
    print("\nEscolha como o computador vai processar (em caso de dúvida, use 1):")
    print("  1 - Automático — o programa decide sozinho (recomendado)")
    print("  2 - OPENMP  (avançado: threads em 1 processo)")
    print("  3 - MPI     (avançado: 1 processo por núcleo)")
    print("  4 - HYBRID  (avançado: MPI + OPENMP combinados)")

    try:
        escolha = int(input("Opcao: "))
    except ValueError:
        escolha = 1

    cfg.setdefault("otimizador", {})["ativo"] = False

    if escolha == 2:
        n = input("Quantos nucleos/threads usar? [ex: 4]: ").strip()
        cfg["auto_config"] = False
        cfg["modo"] = "OPENMP"
        cfg["num_cores"] = int(n) if n.isdigit() else 1
    elif escolha == 3:
        n = input("Quantos processos MPI usar? [ex: 4]: ").strip()
        cfg["auto_config"] = False
        cfg["modo"] = "MPI"
        cfg["num_cores"] = int(n) if n.isdigit() else 1
    elif escolha == 4:
        mpi = input("Processos MPI [ex: 4]: ").strip()
        omp = input("Threads OMP por processo [ex: 2]: ").strip()
        cfg["auto_config"] = False
        cfg["modo"] = "HYBRID"
        cfg["hybrid"] = {
            "mpi_procs": int(mpi) if mpi.isdigit() else 1,
            "omp_threads": int(omp) if omp.isdigit() else 1,
        }
    else:
        cfg["auto_config"] = True

    return cfg


def opcao_executar_fila():
    global _otimizador_testado
    limpar_tela()
    cfg = fds_main.carregar_config("config.yaml")

    if not _otimizador_testado:
        print("=" * 60)
        print("  ANTES DE COMEÇAR")
        print("=" * 60 + "\n")
        print("O programa pode fazer testes rápidos (alguns minutos) para")
        print("descobrir a forma MAIS RÁPIDA de rodar no seu computador.")
        print("Vale a pena: as simulações depois podem levar horas.\n")
        print("1 - Descobrir a configuração mais rápida e rodar (recomendado)")
        print("2 - Escolher manualmente (usuários avançados)")
        print("3 - Rodar direto, sem testes")
        print('')
        try:
            escolha = int(input("Digite o número e pressione ENTER: "))
        except ValueError:
            escolha = 3

        if escolha == 1:
            fds_main.testar_melhor_configuracao(cfg)
            _otimizador_testado = True
            # Garante que a fila aplique o cache que acabou de ser criado,
            # mesmo quando o YAML veio com o otimizador desativado.
            cfg.setdefault("otimizador", {})["ativo"] = True
        elif escolha == 2:
            cfg = _escolher_processamento_manual(cfg)
        else:
            # "Rodar direto" deve respeitar literalmente a escolha e nao
            # iniciar testes apenas porque o config.yaml os ativava.
            cfg.setdefault("otimizador", {})["ativo"] = False
            cfg.setdefault("auto_benchmark", {})["ativo"] = False
        limpar_tela()
    elif _otimizador_testado:
        cfg.setdefault("otimizador", {})["ativo"] = True

    fds_main.executar_fila(cfg)
    print('\n')
    input("Pressione ENTER para voltar ao menu...")


def opcao_detectar_hardware():
    limpar_tela()
    print("=" * 55)
    print("  DETECÇÃO DE HARDWARE")
    print("=" * 55 + "\n")

    hw = detectar_hardware()
    rec = recomendar_config(hw, n_meshes=1)

    import logging
    # force=True: garante saida no console mesmo que uma fila ja tenha
    # configurado o logging antes (com filtro de console amigavel que
    # esconderia estas linhas). A proxima fila reconfigura o logging
    # completo de novo (configurar_logging tambem usa force=True).
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)
    logar_hardware(hw, rec)

    print('\n')
    input("Pressione ENTER para voltar ao menu...")


def opcao_verificar_path():
    limpar_tela()
    path_detector.executar_verificacao_path()
    print('\n')
    input("Pressione ENTER para voltar ao menu...")


def opcao_firewall():
    """
    Cadastra liberacoes permanentes no Firewall do Windows para os
    executaveis FDS/MPI (instalacao + pastas de simulacao). Depois disso
    o Windows para de perguntar "Deseja permitir que este aplicativo se
    comunique?" a cada execucao/pasta nova.
    """
    global _status_firewall_cache
    limpar_tela()
    print("=" * 60)
    print("  PARAR AS PERGUNTAS DO WINDOWS (FIREWALL)")
    print("=" * 60 + "\n")
    print("Toda vez que o FDS ou o MPI rodam em uma pasta nova, o Windows")
    print("pergunta se eles podem se comunicar. Esta opção cadastra as")
    print("liberações de uma vez por todas — a pergunta não aparece mais.\n")

    cfg = _carregar_cfg_silencioso()
    diretorio_fds = _diretorio_fds_atual(cfg)
    if not diretorio_fds:
        print("❌ O FDS não foi encontrado neste computador.")
        print("   Use a opção 6 do menu para verificar a instalação.")
        input("\nPressione ENTER para voltar ao menu...")
        return

    # Pastas onde os binarios sao copiados (cada caso da fila)
    pastas_projetos: list[str] = []
    pasta_sims = str(cfg.get("pasta_simulacoes", "") or "")
    if pasta_sims and os.path.isdir(pasta_sims):
        for raiz, _dirs, nomes in os.walk(pasta_sims):
            if any(n.lower().endswith(".fds") for n in nomes):
                pastas_projetos.append(raiz)

    print(f"Instalação do FDS : {diretorio_fds}")
    if pastas_projetos:
        print(f"Pastas de simulação: {len(pastas_projetos)} (liberadas também)")
    if not firewall_config.eh_admin():
        print("\n⚠️  O Windows vai abrir UMA janela pedindo permissão de")
        print("   administrador. Clique em 'Sim' para concluir.")

    resposta = input("\nContinuar? (S/n): ").strip().lower()
    if resposta in ("n", "nao", "não"):
        print("Operação cancelada.")
        input("\nPressione ENTER para voltar ao menu...")
        return

    print("\nCadastrando liberações no Firewall do Windows...")
    ok, msg = firewall_config.liberar_firewall_interativo(diretorio_fds, pastas_projetos)
    print(("✅ " if ok else "⚠️  ") + msg)

    _status_firewall_cache = None  # forca re-checagem no cabecalho
    input("\nPressione ENTER para voltar ao menu...")


def run_menu():
    # Primeira execucao: exige o aceite da EULA antes do menu.
    import eula
    limpar_tela()
    if not eula.garantir_aceite_console():
        return

    while True:
        limpar_tela()
        largura = 60
        print("=" * largura)
        print(f"   🔥 FDS AUTOMATION v{fds_main.VERSAO}")
        print("   Suas simulações de incêndio no piloto automático")
        print("   © JZ Engenharia — licenciado à RME Consultoria & Engenharia")
        print("=" * largura)
        for linha in _resumo_status():
            print(linha)
        print("-" * largura)
        print('')
        print("  O QUE VOCÊ QUER FAZER?")
        print('')
        print("   1  🚀 RODAR as simulações")
        print("   2  📁 Escolher a pasta das simulações")
        print("   3  ⚡ Descobrir a configuração mais rápida (testes curtos)")
        print("   4  🛡️  Parar as perguntas do Windows (firewall)")
        print("   5  🖥️  Ver meu computador e a configuração sugerida")
        print("   6  🔧 Verificar a instalação do FDS/MPI")
        print("   0  🚪 Sair")
        print('')
        print("  💡 Primeira vez? Faça 2 → 4 → 1. O resto é automático.")
        print("-" * largura)
        print('')

        try:
            selecao = int(input("Digite o número e pressione ENTER: "))
        except ValueError:
            print("\n⚠️  Digite apenas o número da opção (ex.: 1) e ENTER.")
            input("Pressione ENTER para tentar novamente...")
            continue

        try:
            if selecao == 1:
                opcao_executar_fila()
            elif selecao == 2:
                opcao_selecionar_pasta()
            elif selecao == 3:
                opcao_testar_configuracao()
            elif selecao == 4:
                opcao_firewall()
            elif selecao == 5:
                opcao_detectar_hardware()
            elif selecao == 6:
                opcao_verificar_path()
            elif selecao == 0:
                limpar_tela()
                print("-" * largura)
                print("Fechando o programa. Até a próxima!")
                print("-" * largura)
                break
            else:
                print("\n⚠️  Essa opção não existe. Escolha um número de 0 a 6.")
                input("Pressione ENTER para continuar...")
        except KeyboardInterrupt:
            print("\n\nOperação cancelada. Nada foi perdido.")
            input("Pressione ENTER para voltar ao menu...")
        except Exception:
            print("\n[ERRO INESPERADO — copie o texto abaixo e envie para o suporte]\n")
            print(traceback.format_exc())
            input("Pressione ENTER para voltar ao menu...")


if __name__ == "__main__":
    run_menu()
