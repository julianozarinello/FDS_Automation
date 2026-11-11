# Contexto do Projeto — FDS Automation

> **Este documento é um briefing técnico destinado a IAs assistentes, colaboradores ou qualquer pessoa que assuma o desenvolvimento deste projeto.**
> Leia este arquivo antes de qualquer outra coisa.

---

## 1. O que é este projeto?

O **FDS Automation** é um programa em Python que automatiza a execução de simulações do **Fire Dynamics Simulator (FDS)** — o motor de cálculo de dinâmica de incêndios desenvolvido pelo NIST (EUA), amplamente usado em engenharia de segurança contra incêndio.

O FDS é normalmente executado manualmente pelo usuário, um arquivo por vez, sem otimização de hardware. Este programa resolve isso: dada uma pasta com vários arquivos `.fds`, ele os executa em fila, com o máximo de performance que o PC permite.

**Repositório:** https://github.com/julianozarinello/FDS_Automation  
**Autor:** Juliano de Souza Zarinello Júnior  
**Licença:** Proprietário — © JZ Engenharia, licenciado para uso na RME Consultoria & Engenharia (ver `EULA.txt`)  
**Linguagem:** Python 3.12+  
**Sistema operacional alvo:** Windows (o FDS é distribuído via PyroSim/NIST para Windows)  
**Versão atual:** 1.18

---

## 2. Objetivo principal

> **Fazer as simulações FDS rodarem o mais rápido possível no PC do usuário, de forma automática e sem intervenção manual.**

Isso envolve três pilares:

1. **Automação** — descobrir, enfileirar e executar simulações sem o usuário precisar abrir o FDS manualmente.
2. **Paralelismo inteligente** — usar o modo de execução (OPENMP, MPI, HYBRID) mais adequado ao hardware e ao arquivo `.fds`.
3. **Otimização segura de malha** — balancear `MPI_PROCESS` sem mudar a ordem das meshes; cortes geométricos ficam opt-in porque criam novas interfaces numéricas.

---

## 3. Estrutura de arquivos

```
FDS_Automation/
├── main.py            # Ponto de entrada. Orquestra toda a execução.
├── fds_reader.py      # Lê arquivos .fds: copia binários e conta meshes.
├── hw_detector.py     # Detecta hardware (cores, RAM, NUMA, Intel/AMD).
├── mesh_splitter.py   # Balanceia MPI_PROCESS; divisão geométrica é opt-in.
├── optimizer.py       # Benchmark empírico, candidatos e cache por caso/hardware/FDS.
├── process_guard.py   # Prioridade, afinidade e encerramento da árvore FDS/MPI.
├── case_control.py    # IPC por arquivo exclusivo para pular somente o caso atual.
├── fds_diagnostics.py # Eventos de erro estruturados e explicados para a GUI.
├── monitor_xlsx.py    # Acompanhamento compartilhado com gravação atômica.
├── firewall_config.py # Libera FDS/MPI no Firewall do Windows (para as perguntas).
├── gui.py             # Interface gráfica (Tkinter). Também é o worker --rodar/--testar.
├── menu.py            # Menu de terminal (uso avançado; empacotado em FDS_Automation.exe).
├── config.yaml        # Painel de controle do usuário (caminhos, modo, flags).
├── README.md          # Documentação de uso para o usuário final.
├── VALIDACAO.md       # Fontes oficiais, metodologia e testes de desempenho.
├── tests/             # Testes rápidos de regressão e invariantes.
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
      ├─ aplicar_otimizador_desempenho() ← testa/reaproveita a configuração do caso
      ├─ aplicar_mesh_splitter()   ← balanceia MPI_PROCESS; divisão só se opt-in
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
Funções principais:
- `preparar_ambiente_fds(diretorio_fds, pasta_projeto)` — copia todos os `.exe` e `.dll` necessários para a pasta onde o `.fds` está. Isso é necessário porque o FDS precisa encontrar seus próprios binários na pasta de trabalho.
- `encontrar_namelists()` — scanner consciente de comentários, aspas Fortran e barras dentro de strings.
- `contar_meshes(caminho_fds)` — conta namelists `&MESH` ativos, usada para validar MPI e para o auto-config.
- `motivos_contagem_mesh_indeterminada()` — detecta `MULT_ID`/`CATF`, cuja expansão só ocorre dentro do FDS.

### `hw_detector.py`
Detecta o hardware do PC e recomenda a melhor configuração de paralelismo:
- Usa `psutil` para obter cores físicos vs. lógicos (hyperthreading), RAM e sockets NUMA.
- Usa `py-cpuinfo` (opcional) para identificar fabricante (Intel vs. AMD).
- **Regra chave:** o FDS performa pior com hyperthreading. Sempre usa apenas cores físicos.
- **Regra inicial:** recomenda MPI puro quando as meshes ocupam os núcleos físicos, HYBRID quando sobram núcleos por mesh e OPENMP para mesh única. A marca Intel/AMD não substitui o benchmark empírico.

### `mesh_splitter.py`
O módulo mais complexo. Responsável por:
1. **Ler** todas as meshes do `.fds` (XB, IJK, notação Fortran e campos extras preservados). Qualquer entrada não compreendida cancela a operação inteira.
2. **Particionar** a ordem existente em blocos contíguos com o menor pico de células possível. `MPI_PROCESS` fica contínuo e crescente.
3. **Dividir**, somente com `permitir_divisao: true`, meshes no melhor eixo. Um corte adicional só é aceito se reduzir o desequilíbrio previsto.
4. **Reescrever** cada mesh na posição original, com backup inicial imutável (`.fds.bak`) e troca atômica do arquivo.

**Restrições do FDS respeitadas pelo mesh_splitter:**
- IJK deve ser inteiro positivo em cada eixo.
- Só cortar eixos com pelo menos 8 células (cada metade precisa manter mínimo 4).
- O tamanho de célula (`dx`, `dy`, `dz`) é preservado matematicamente após o corte.
- Meshes de um mesmo núcleo MPI devem ter IDs consecutivos e crescentes na ordem do arquivo.
- Novas interfaces exigem comparação original × otimizado e inspeção de `VELOCITY_ERROR`.

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
| `mesh_splitter.ativo` | Ativa o balanceamento seguro de `MPI_PROCESS` |
| `mesh_splitter.margem_pct` | Tolerância de desequilíbrio de células entre núcleos (%) |
| `mesh_splitter.dry_run` | Se `true`, apenas loga a divisão sem modificar o arquivo |
| `mesh_splitter.permitir_divisao` | Opção avançada; cria interfaces somente quando `true` |
| `mesh_splitter.max_meshes_por_core` | Limite conservador de cortes por processo |
| `performance.*` | Flags de OpenMP/Intel (afinidade, spin-wait, NUMA, MKL) |

---

## 6. Dependências Python

| Pacote | Obrigatório | Uso |
|---|---|---|
| `pyyaml` | Sim | Leitura do `config.yaml` |
| `psutil` | Sim | Detecção de hardware e prioridade de processo |
| `py-cpuinfo` | Não | Identifica fabricante da CPU (Intel/AMD) com mais precisão |
| `openpyxl` | Para planilha | Acompanhamento compartilhado em `.xlsx` |

Instalação:
```bash
pip install -r requirements.txt
```

---

## 7. Princípios de desenvolvimento (respeite sempre)

1. **Não apague funcionalidades existentes.** O histórico mostra que código já foi perdido por IAs que reescreveram arquivos inteiros. Sempre adicione, nunca substitua sem necessidade.
2. **Novas funcionalidades = novos módulos.** Siga o padrão já estabelecido: `fds_reader.py`, `hw_detector.py`, `mesh_splitter.py`. Se precisar adicionar algo grande, crie um novo arquivo.
3. **GUI para o fluxo comum; `config.yaml` para opções avançadas.** Novas opções devem ter linguagem de engenharia, mensagens acionáveis e valores padrão seguros.
4. **Backup antes de modificar `.fds`.** Qualquer operação que altere o arquivo de simulação deve criar `.fds.bak` antes.
5. **Logging em vez de print.** Todo output usa `logging.getLogger(__name__)`. Nunca usar `print()` no código novo.
6. **Windows first.** O FDS roda em Windows. Caminhos, prioridade de processo (`HIGH_PRIORITY_CLASS`), variáveis de ambiente Intel MPI — tudo deve ser testado com mentalidade Windows.
7. **Codificação `latin-1` para `.fds`.** Arquivos gerados pelo PyroSim/FDS usam encoding `latin-1`, não UTF-8.

---

## 8. O que ainda não foi implementado (ideias futuras)

- [ ] Correção automática do caminho do log (atualmente pode falhar se `pasta_simulacoes` estiver dentro de `Program Files`).
- [ ] Suporte a simulações em rede/NAS com detecção de OneDrive ativo.
- [x] Interface gráfica simples (v1.17: `gui.py` em Tkinter, empacotada como `FDS_Automation_GUI.exe`).
- [x] Estimativa de tempo restante durante a simulação (v1.17: lê T_END do `.fds` e projeta a partir do progresso real).
- [x] Aba de erros explicados, com mensagem literal do FDS e ações sugeridas (v1.18).
- [x] Interrupção individual do caso atual sem encerrar a fila (v1.18).
- [ ] Validação da relação de aspecto das meshes após o corte (FDS recomenda células com proporção próxima de 1:1:1).
- [ ] Suporte a Linux/WSL (FDS também tem versão Linux).

---

## 9. Novidades da v1.18

**Diagnóstico de falhas:**
- Nova aba **Log e erros**, dividida em “Erros explicados” e “Log técnico completo”.
- O módulo `fds_diagnostics.py` grava `fds_diagnosticos.jsonl` ao lado de `fds_run.log`; cada evento guarda caso, mensagem original, contexto, código, possível causa e passos de verificação.
- O `.out` é localizado pelo `CHID` de `&HEAD`, que pode ser diferente do nome do `.fds`.
- Erros de preparação e erros fatais do FDS são estados separados; `Maximum Velocity Error` continua sendo tratado como métrica, não como falha.
- O código final genérico deixou de dominar a interface: ela mostra contagens amigáveis e abre a aba de erros quando uma falha real ocorre.

**Controle da fila:**
- Novo botão **Parar caso atual**. Ele encerra somente o Job Object interno do FDS/benchmark e mantém o worker da fila vivo para iniciar o próximo caso.
- O pedido contém índice e nome do caso. Pedidos atrasados são descartados para nunca interromper acidentalmente o caso seguinte.
- Cancelamento durante o otimizador é propagado como interrupção voluntária, sem testar outros candidatos nem registrar uma falsa falha.
- O relatório e `tempos_simulacao.dat` usam `PULOU`; a planilha usa `CANCELADA`. Uma fila com apenas sucessos e interrupções voluntárias termina normalmente.
- **47 testes rápidos** cobrem as regressões anteriores, diagnósticos, protocolo contra pedidos obsoletos, continuação da fila e encerramento real de um subprocesso lento.

---

## 10. Novidades da v1.17

**Correções:**
- `configurar_logging` agora usa `force=True`: antes, se o usuário abrisse "Detectar hardware" no menu primeiro, o `fds_run.log` nunca era criado; trocar de pasta também mantinha o log preso na pasta antiga.
- `fixar_afinidade_os` em MPI/HYBRID **particiona** os núcleos em fatias contíguas (uma por rank `fds.exe`). Antes, todos os ranks recebiam a máscara completa de P-cores, o que desfazia o pinning compacto do Intel MPI e deixava os ranks migrando entre núcleos.
- A afinidade agora aguarda todos os ranks MPI aparecerem (`n_esperado`) antes de considerar o ajuste completo — evita particionar entre só parte dos ranks.
- `hybrid_cpu.usar_e_cores: true` agora libera de fato os E-cores na afinidade (novo campo `logical_indices_todos` no hw_detector). Antes só mudava a contagem de cores, mas a afinidade continuava prendendo o FDS nos P-cores.
- Coordenadas de mesh reescritas com 17 dígitos significativos, garantindo round-trip IEEE-754 (antes 6/9 podiam arredondar domínios grandes ou limites próximos).
- Detecção de sockets com fallback PowerShell/CIM (o `wmic` foi removido no Windows 11 24H2+).

**Performance:**
- `detectar_hardware()` com cache (era chamado várias vezes por simulação; a detecção de sockets gastava 1–2 s por chamada).
- `OMP_STACKSIZE=16M` como padrão defensivo — configurável em `performance.omp_stacksize`.
- `MKL_NUM_THREADS` é 1 em MPI puro e acompanha o orçamento de threads nos demais modos; o otimizador também compara SERIAL/MKL.
- `I_MPI_FABRICS=shm` para execução em máquina única (elimina sondagem/overhead de rede do Intel MPI) — configurável em `performance.mpi_shm` (use `false` em cluster).
- O ambiente dos testes do otimizador espelha exatamente o da simulação real (mesmas variáveis novas).

**Interface:**
- Status periódico do console agora mostra o tempo restante estimado (`restante ~HHh MMm SSs`), calculado por regra de três entre o tempo simulado e o T_END do arquivo.
- Menu totalmente reformulado para leigos: cabeçalho com status (pasta, nº de casos, FDS encontrado, firewall), opções em linguagem simples com emojis, "RODAR" como opção 1, dica de primeiro uso.

**Novo módulo `gui.py` (interface gráfica Tkinter):**
- Janela única: status (pasta/casos/FDS/firewall), escolha de pasta, lista de casos, botão RODAR, acompanhamento ao vivo com tempo restante na barra de status, cancelamento seguro, botão de firewall.
- A fila roda em um **processo separado** (o próprio exe/script chamado com `--rodar`, protegido por Job Object) — a janela nunca trava e Cancelar mata a árvore FDS/MPI inteira.
- O acompanhamento lê (`tail`) o `fds_run.log` que o main.py já escreve; linhas "FDS |" ficam ocultas a menos que o usuário marque "Mostrar detalhes técnicos".
- Empacotada por `FDS_Automation_GUI.spec` (console=False) como `FDS_Automation_GUI.exe`. O menu de terminal (`menu.py` → `FDS_Automation.exe`) continua existindo.

**Licença e aceite (EULA):**
- `EULA.txt`: contrato de licença — software © JZ Engenharia, licenciado para uso na RME Consultoria & Engenharia (embutido nos .exe via `datas` do PyInstaller).
- Novo módulo `eula.py`: aceite obrigatório na primeira execução (janela modal na GUI com checkbox "Li e aceito"; prompt "digite ACEITO" no menu). O aceite fica registrado em `eula_aceite.txt` (versão da EULA, data, usuário do Windows, PC) ao lado do programa ou em `%LOCALAPPDATA%\FDS_Automation`.
- O aceite é **amarrado ao nome do PC**: copiar a pasta para outra máquina exibe a EULA de novo. Mudar `EULA_VERSAO` em `eula.py` força novo aceite geral.
- O modo worker (`--rodar`/`--testar`) nunca bloqueia pedindo aceite (é lançado pela GUI, que já o exigiu).

**Fila dinâmica (seção `fila_dinamica` do config):**
- Com a fila já rodando, a pasta principal é re-escaneada ao fim de cada simulação; subpastas/casos `.fds` novos entram automaticamente no **final da fila** (e aparecem como NA FILA na planilha de acompanhamento).
- `otimizar_novas: true` (padrão): simulações detectadas depois do início rodam com o otimizador forçado — descobrem a melhor configuração automaticamente mesmo que a fila tenha começado sem otimizador (o cache evita re-testar casos já conhecidos).
- `idade_min_seg` ignora `.fds` modificados há poucos segundos (cópia/sincronização ainda em andamento; ficam para a próxima verificação).
- Só no modo `pasta_simulacoes` (lista manual e arquivo único são fixos por definição).

**Novo módulo `monitor_xlsx.py` (acompanhamento em planilha compartilhada):**
- Grava o andamento da fila em um `.xlsx` (colunas: Caso, Status, Progresso, Tempo restante, Usuário, Empresa, PC, Início, Fim, Duração, Erro, Atualizado em) com cores por status e cabeçalho congelado.
- Multi-PC: o arquivo fica numa pasta sincronizada (Google Drive para Desktop/OneDrive/rede); cada PC atualiza **apenas as próprias linhas** (chave = PC + caso), então vários computadores compartilham a mesma planilha sem apagar os dados uns dos outros. Sem API do Google — é o cliente de sincronização que replica o arquivo.
- Best-effort sempre: planilha aberta no Excel/bloqueada → 3 tentativas e segue a fila (avisa 1x no log). Progresso limitado por `monitoramento.intervalo_seg` para não sobrecarregar a sincronização.
- Erros do FDS: a primeira linha de erro detectada no stdout (`ERROR(...)` etc.) vai para a coluna Erro.
- Config: seção `monitoramento` (ativo, arquivo_xlsx, usuario, empresa, pc, intervalo_seg). A GUI tem a aba "📊 Acompanhamento" que preenche a seção, mostra a planilha inteira (todos os PCs) numa tabela colorida com auto-refresh de 30 s, e botões para escolher/abrir a planilha.
- Dependência nova: `openpyxl` (obrigatória apenas se o monitoramento for usado; sem ela o recurso se desativa com aviso).

**Novo módulo `firewall_config.py`:**
- O Windows perguntava "Deseja permitir que este aplicativo se comunique?" para cada executável FDS/MPI em cada pasta nova (os binários são copiados por pasta de simulação → pergunta a cada projeto).
- O módulo cadastra regras permanentes no Firewall do Windows (`netsh advfirewall`) para fds.exe, fds_openmp.exe, mpiexec.exe, hydra_service.exe, hydra_pmi_proxy.exe e smpd.exe — na instalação E nas pastas de simulação (regras podem ser criadas antes da cópia, o firewall guarda só o caminho).
- Sem admin: menu opção 4 gera script PowerShell e eleva via UAC uma única vez. Com admin: cadastra em silêncio antes de cada simulação (`firewall.auto_liberar: true` no config).
- Também remove o Zone.Identifier ("arquivo veio da internet") dos executáveis copiados, evitando avisos do SmartScreen.

### Revisão de robustez e desempenho — 20/07/2026

- O balanceamento passou de bin-packing com reordenação para partição ótima em blocos contíguos; a ordem de `&MESH` e a posição de registros intercalados são preservadas.
- `permitir_divisao: false` é o padrão seguro. Cortes só são aceitos quando melhoram a carga prevista e respeitam `max_meshes_por_core`.
- Parser de meshes aceita expoentes `D`, repetição Fortran, comentários e strings com `/`, `!`, vírgulas ou nomes de campos; cancela a reescrita se qualquer mesh não for compreendida.
- `MULT_ID`/`CATF` preservam o modelo sem otimização baseada em contagem incompleta; ranks MPI excedentes são limitados quando a contagem é conhecida.
- A divisão avançada recua para transformações, `SLCF MESH_NUMBER`, `CSVF`, restart e meshes cilíndricas, e nunca cria uma dimensão filha menor que 4 células.
- Backup original não é sobrescrito; `.fds`, cache e `.xlsx` usam gravação temporária + troca atômica.
- Benchmark mede o avanço entre amostras, testa SERIAL e OpenMP 1/2/4/total, usa o binário OpenMP no híbrido e invalida cache por executável, hardware e opções relevantes.
- A recomendação AMD deixou de forçar OpenMP: multi-mesh segue a orientação MPI/HYBRID e o benchmark decide empiricamente.
- Fila e worker agora devolvem falha real; cancelamento tem fallbacks para toda a árvore MPI/FDS.
- GUI ganhou diagnóstico do computador e aviso explícito quando a divisão avançada está ativa.
- Suíte em `tests/`: 47 regressões rápidas, além de comparação exata de 4.500 partições aleatórias. Consulte `VALIDACAO.md`.
