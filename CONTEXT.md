# Contexto do Projeto — FDS Automation

> **Este documento é um briefing técnico destinado a IAs assistentes, colaboradores ou qualquer pessoa que assuma o desenvolvimento deste projeto.**
> Leia este arquivo antes de qualquer outra coisa.

---

## 1. O que é este projeto?

O **FDS Automation** é um programa em Python que automatiza a execução de simulações do **Fire Dynamics Simulator (FDS)** — o motor de cálculo de dinâmica de incêndios desenvolvido pelo NIST (EUA), amplamente usado em engenharia de segurança contra incêndio.

O FDS é normalmente executado manualmente pelo usuário, um arquivo por vez, sem otimização de hardware. Este programa resolve isso: dada uma pasta com vários arquivos `.fds`, ele os executa em fila, com o máximo de performance que o PC permite.

**Repositório:** https://github.com/julianozarinello/FDS_Automation  
**Autor:** Juliano de Souza Zarinello Júnior  
**Linguagem:** Python 3.12+  
**Sistema operacional alvo:** Windows (o FDS é distribuído via PyroSim/NIST para Windows)  
**Versão atual:** 1.15

---

## 2. Objetivo principal

> **Fazer as simulações FDS rodarem o mais rápido possível no PC do usuário, de forma automática e sem intervenção manual.**

Isso envolve três pilares:

1. **Automação** — descobrir, enfileirar e executar simulações sem o usuário precisar abrir o FDS manualmente.
2. **Paralelismo inteligente** — usar o modo de execução (OPENMP, MPI, HYBRID) mais adequado ao hardware e ao arquivo `.fds`.
3. **Otimização de malha** — dividir e reordenar as meshes do arquivo `.fds` para que cada núcleo do processador receba uma carga equilibrada de células.

---

## 3. Estrutura de arquivos

```
FDS_Automation/
├── main.py            # Ponto de entrada. Orquestra toda a execução.
├── fds_reader.py      # Lê arquivos .fds: copia binários e conta meshes.
├── hw_detector.py     # Detecta hardware (cores, RAM, NUMA, Intel/AMD).
├── mesh_splitter.py   # Divide e reordena meshes para balancear carga por núcleo.
├── config.yaml        # Painel de controle do usuário (caminhos, modo, flags).
├── README.md          # Documentação de uso para o usuário final.
└── CONTEXT.md         # Este arquivo. Briefing técnico para IAs e colaboradores.
```

---

## 4. Fluxo de execução (o que acontece ao rodar `main.py`)

```
main()
 ├─ Carrega config.yaml
 ├─ Configura logging (arquivo fds_run.log + console)
 ├─ Descobre arquivos .fds (pasta / lista manual / arquivo único)
 ├─ Eleva prioridade do processo (HIGH_PRIORITY_CLASS no Windows)
 └─ Para cada .fds na fila:
      ├─ aplicar_autoconfig()      ← detecta hardware e recomenda modo/núcleos
      ├─ aplicar_mesh_splitter()   ← divide e reordena meshes (se ativo no config)
      ├─ validar_simulacao()       ← checa arquivos e consistência MPI × meshes
      ├─ preparar_ambiente_fds()   ← copia .exe e .dll para a pasta do projeto
      ├─ aplicar_variaveis_perf()  ← define OMP_NUM_THREADS, KMP_AFFINITY, MKL, etc.
      ├─ construir_comando()       ← monta o comando (fds.exe / mpiexec / fds_openmp.exe)
      └─ subprocess.run()          ← executa o FDS e mede o tempo
 └─ Imprime relatório final com tempo de cada simulação
```

---

## 5. Módulos em detalhe

### `main.py`
Ponto de entrada e orquestrador. Contém toda a lógica de fluxo, variáveis de ambiente de performance, construção de comandos e relatório final. **Não deve conter lógica de leitura de `.fds` nem de hardware** — isso fica nos módulos especializados.

### `fds_reader.py`
Duas funções:
- `preparar_ambiente_fds(diretorio_fds, pasta_projeto)` — copia todos os `.exe` e `.dll` necessários para a pasta onde o `.fds` está. Isso é necessário porque o FDS precisa encontrar seus próprios binários na pasta de trabalho.
- `contar_meshes(caminho_fds)` — conta quantas linhas `&MESH` existem no arquivo, usada para validar MPI e para o auto-config.

### `hw_detector.py`
Detecta o hardware do PC e recomenda a melhor configuração de paralelismo:
- Usa `psutil` para obter cores físicos vs. lógicos (hyperthreading), RAM e sockets NUMA.
- Usa `py-cpuinfo` (opcional) para identificar fabricante (Intel vs. AMD).
- **Regra chave:** o FDS performa pior com hyperthreading. Sempre usa apenas cores físicos.
- **Regra AMD:** em CPUs AMD, prefere OPENMP puro (o driver OpenMP GNU é mais eficiente que Intel MPI em AMD).
- **Regra MPI:** recomenda MPI puro quando `n_meshes == n_cores_físicos`, HYBRID quando sobram cores por mesh, OPENMP nos demais casos.

### `mesh_splitter.py`
O módulo mais complexo. Responsável por:
1. **Ler** todas as meshes do `.fds` (XB, IJK e campos extras preservados).
2. **Dividir** meshes iterativamente ao meio no melhor eixo (maior número de células, preferencialmente par) até ter meshes suficientes para todos os núcleos.
3. **Reordenar** as meshes usando algoritmo greedy de balanceamento de carga, garantindo que cada núcleo MPI receba um bloco contíguo de IDs crescentes (exigência do FDS).
4. **Reescrever** o arquivo `.fds` com as novas meshes, criando backup automático (`.fds.bak`) antes.

**Restrições do FDS respeitadas pelo mesh_splitter:**
- IJK deve ser inteiro positivo em cada eixo.
- Não cortar eixos com menos de 4 células (cada metade precisa ter mínimo 2).
- O tamanho de célula (`dx`, `dy`, `dz`) é preservado matematicamente após o corte.
- Meshes de um mesmo núcleo MPI devem ter IDs consecutivos e crescentes na ordem do arquivo.

### `config.yaml`
Todo o comportamento do programa é controlado aqui. O usuário **nunca** precisa editar o código Python. Campos principais:

| Campo | Descrição |
|---|---|
| `pasta_simulacoes` | Pasta com os arquivos `.fds` a executar |
| `diretorio_fds` | Pasta de instalação do FDS/PyroSim |
| `auto_config` | Se `true`, detecta hardware e ignora modo/num_cores manuais |
| `modo` | `SERIAL` / `OPENMP` / `MPI` / `HYBRID` (manual) |
| `num_cores` | Número de threads ou processos MPI (manual) |
| `hybrid.mpi_procs` | Processos MPI no modo HYBRID |
| `hybrid.omp_threads` | Threads OpenMP por processo no modo HYBRID |
| `mesh_splitter.ativo` | Ativa divisão automática de meshes |
| `mesh_splitter.margem_pct` | Tolerância de desequilíbrio de células entre núcleos (%) |
| `mesh_splitter.dry_run` | Se `true`, apenas loga a divisão sem modificar o arquivo |
| `performance.*` | Flags de otimização OpenMP/Intel (affinity, spin-wait, NUMA) |

---

## 6. Dependências Python

| Pacote | Obrigatório | Uso |
|---|---|---|
| `pyyaml` | Sim | Leitura do `config.yaml` |
| `psutil` | Sim | Detecção de hardware e prioridade de processo |
| `py-cpuinfo` | Não | Identifica fabricante da CPU (Intel/AMD) com mais precisão |

Instalação:
```bash
pip install pyyaml psutil py-cpuinfo
```

---

## 7. Princípios de desenvolvimento (respeite sempre)

1. **Não apague funcionalidades existentes.** O histórico mostra que código já foi perdido por IAs que reescreveram arquivos inteiros. Sempre adicione, nunca substitua sem necessidade.
2. **Novas funcionalidades = novos módulos.** Siga o padrão já estabelecido: `fds_reader.py`, `hw_detector.py`, `mesh_splitter.py`. Se precisar adicionar algo grande, crie um novo arquivo.
3. **O `config.yaml` é a única interface do usuário.** Toda nova funcionalidade deve ser controlável por ele, com valores padrão seguros (geralmente `false` para features opcionais).
4. **Backup antes de modificar `.fds`.** Qualquer operação que altere o arquivo de simulação deve criar `.fds.bak` antes.
5. **Logging em vez de print.** Todo output usa `logging.getLogger(__name__)`. Nunca usar `print()` no código novo.
6. **Windows first.** O FDS roda em Windows. Caminhos, prioridade de processo (`HIGH_PRIORITY_CLASS`), variáveis de ambiente Intel MPI — tudo deve ser testado com mentalidade Windows.
7. **Codificação `latin-1` para `.fds`.** Arquivos gerados pelo PyroSim/FDS usam encoding `latin-1`, não UTF-8.

---

## 8. O que ainda não foi implementado (ideias futuras)

- [ ] Correção automática do caminho do log (atualmente pode falhar se `pasta_simulacoes` estiver dentro de `Program Files`).
- [ ] Suporte a simulações em rede/NAS com detecção de OneDrive ativo.
- [ ] Interface gráfica simples (Tkinter ou web) para configurar o `config.yaml` visualmente.
- [ ] Estimativa de tempo restante durante a simulação (lendo o arquivo `.out` do FDS em tempo real).
- [ ] Validação da relação de aspecto das meshes após o corte (FDS recomenda células com proporção próxima de 1:1:1).
- [ ] Suporte a Linux/WSL (FDS também tem versão Linux).
