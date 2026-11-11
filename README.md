# FDS Automation v1.18

Aplicativo Windows para organizar, testar e executar filas do
[Fire Dynamics Simulator (FDS)](https://pages.nist.gov/fds/) sem alterar o
código-fonte ou os executáveis do simulador.

O programa foi pensado para engenheiros: a interface escolhe a pasta dos casos,
mostra o que encontrou, recomenda a distribuição do computador e acompanha a
execução. O `config.yaml` continua disponível para ajustes avançados.

## Uso recomendado

1. Abra `FDS_Automation_GUI.exe` na pasta `dist`.
2. Clique em **Escolher…** e selecione a pasta que contém os casos `.fds`.
3. Mantenha marcada a opção **descobrir a configuração mais rápida**.
4. Clique em **RODAR SIMULAÇÕES**.

O primeiro teste de um caso compara configurações curtas em SERIAL, OpenMP,
MPI e modo híbrido. A melhor escolha fica em cache; execuções seguintes do
mesmo modelo, hardware, FDS e opções reaproveitam o resultado.

O botão **Diagnóstico do computador** explica, em linguagem direta, os núcleos,
a memória, o FDS encontrado e a configuração inicial recomendada.

Durante a fila, o botão **Parar caso atual** encerra somente a simulação em
andamento e inicia automaticamente a próxima. **Cancelar fila** continua
disponível quando for necessário interromper tudo.

## Log e identificação de erros

A aba **Log e erros** separa o diagnóstico amigável do registro técnico. Para
cada caso que falhar, ela mostra:

- a mensagem original, sem ocultar ou reescrever o que o FDS informou;
- o arquivo `.fds`, o código FDS/retorno e o trecho próximo ao erro;
- uma explicação em português da possível causa;
- ações sugeridas para verificar ou corrigir o modelo;
- botões para abrir o `.fds`, o `.out`, a pasta do caso e copiar o diagnóstico.

O arquivo técnico `fds_run.log` permanece completo. Os cartões da interface são
alimentados por `fds_diagnosticos.jsonl`, recriado a cada fila na pasta das
simulações. Casos parados pelo usuário aparecem como **INTERROMPIDA**, e não
como falha do FDS.

## O que o programa otimiza

- usa núcleos físicos e evita Hyper-Threading/SMT quando ele não traz benefício;
- testa SERIAL (inclusive MKL), 1, 2, 4 e o total de threads OpenMP, além das
  combinações MPI/híbridas compatíveis com o número de meshes;
- mede avanço de tempo simulado por tempo real, depois da inicialização do FDS;
- fixa prioridade e afinidade dos processos FDS no Windows;
- distribui `MPI_PROCESS` em blocos contíguos e balanceados, sem mudar a ordem
  das entradas `&MESH`;
- reduz automaticamente ranks MPI excedentes e valida o mapeamento antes de o
  FDS abortar; `MULT_ID`/`CATF` recebem fallback conservador porque expandem o
  caso somente dentro do simulador;
- invalida o cache quando o caso, o executável FDS, o computador ou uma opção de
  desempenho relevante muda.

## Segurança do modelo

Por padrão, o aplicativo **não cria novas interfaces de mesh**. Ele preserva as
meshes, sua ordem no arquivo e somente ajusta a atribuição a processos MPI. O
leitor respeita comentários e strings Fortran e mantém coordenadas com precisão
de round-trip.

O modo avançado `mesh_splitter.permitir_divisao: true` pode cortar meshes para
melhorar o balanceamento, mas fica desligado porque uma nova interface numérica
pode produzir pequenas diferenças. Ao usá-lo:

1. compare o caso original e o otimizado;
2. verifique `VELOCITY_ERROR` nas interfaces no arquivo de saída do FDS;
3. confirme que as grandezas de engenharia relevantes permanecem aceitáveis.

Mesmo com a opção ligada, o corte recua quando encontra mesh cilíndrica,
transformações/`MESH_NUMBER`, `CSVF`, restart, `MULT_ID` ou `CATF`. Isso evita
alterar associações que dependem da ordem e da quantidade original de meshes.

Antes da primeira reescrita, o arquivo original é guardado como `.fds.bak`. Esse
backup inicial não é sobrescrito em execuções posteriores. A gravação do `.fds`,
do cache e da planilha é atômica para reduzir risco de arquivo parcial.

## Acompanhamento da equipe

Na aba **Acompanhamento**, é possível escolher uma planilha `.xlsx` em pasta de
rede, OneDrive ou Google Drive. Cada linha identifica PC e caminho relativo do
caso, evitando colisões entre subpastas com arquivos de mesmo nome.

A escrita usa bloqueio cooperativo e nova tentativa. Sincronizadores de nuvem
podem, porém, criar conflitos entre computadores antes que o arquivo `.lock`
seja sincronizado; para operação crítica multi-PC, prefira uma pasta de rede com
acesso direto ao mesmo arquivo.

## Execução pelo código-fonte

Requer Python 3.12 ou mais recente:

```powershell
python -m pip install -r requirements.txt
python gui.py
```

Para o menu de terminal:

```powershell
python menu.py
```

Para rodar diretamente a fila descrita no `config.yaml`:

```powershell
python main.py --config config.yaml
```

## Testes rápidos

```powershell
python -m unittest discover -s tests -v
python -m py_compile case_control.py eula.py fds_diagnostics.py fds_reader.py firewall_config.py gui.py hw_detector.py main.py menu.py mesh_splitter.py monitor_xlsx.py optimizer.py path_detector.py process_guard.py
```

A metodologia, os resultados medidos e as fontes técnicas estão em
[`VALIDACAO.md`](VALIDACAO.md).

Estado desta revisão: **47 testes automatizados aprovados**; o ensaio curto com
FDS real confirmou ganho OpenMP no caso oficial e validou o limite seguro dos
cortes.

## Arquivos principais

| Arquivo | Função |
|---|---|
| `gui.py` | interface gráfica e processo worker |
| `main.py` | fila, execução do FDS e relatório |
| `optimizer.py` | candidatos, benchmark e cache |
| `mesh_splitter.py` | leitura e balanceamento seguro de meshes |
| `hw_detector.py` | detecção e recomendação de hardware |
| `process_guard.py` | afinidade, prioridade e cancelamento em árvore |
| `case_control.py` | canal seguro para parar somente o caso atual |
| `fds_diagnostics.py` | erros estruturados, explicações e caminhos `.out` |
| `monitor_xlsx.py` | acompanhamento em planilha compartilhada |
| `config.yaml` | opções avançadas com valores padrão seguros |
