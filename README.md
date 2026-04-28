# FDS Automation v1.13

Gerenciador de simulações [Fire Dynamics Simulator (FDS)](https://pages.nist.gov/fds-smv/) em Python, com auto-descoberta de arquivos, fila sequencial e otimizações de performance.

## Requisitos

```bash
pip install pyyaml psutil
```

> **Nota para distribuição com PyInstaller:** adicionar pedido de permissão ao usuário antes de copiar binários.

## Como usar

1. Edite o `config.yaml` com o caminho da pasta e as configurações.
2. Execute:

```bash
python main.py
# ou com config customizado:
python main.py --config meu_caso.yaml
```

Um arquivo `fds_run.log` é gerado automaticamente na pasta das simulações.

## Seleção de arquivos (3 opções)

Escolha **uma** das opções no `config.yaml`:

### Opção A — Pasta (recomendada)
Aponte para a pasta com os arquivos `.fds`. O programa detecta e ordena todos automaticamente:
```yaml
pasta_simulacoes: "C:/Projetos/FDS"
```

### Opção B — Lista manual
```yaml
simulacoes:
  - "C:/Projetos/FDS/Caso_01.fds"
  - "C:/Projetos/FDS/Caso_02.fds"
```

### Opção C — Arquivo único
```yaml
arquivo_fds: "C:/Projetos/FDS/Pyrosim.fds"
```

> **Prioridade:** `pasta_simulacoes` > `simulacoes` > `arquivo_fds`

## Modos de execução

| Modo | Descrição |
|---|---|
| `SERIAL` | Um único processo, sem paralelismo |
| `OPENMP` | Múltiplas threads em memória compartilhada |
| `MPI` | Múltiplos processos (1 por mesh recomendado) |
| `HYBRID` | Combinação de processos MPI com threads OpenMP |

## Otimizações de performance

| Otimização | Descrição |
|---|---|
| **Prioridade HIGH** | Processo elevado a alta prioridade no SO |
| `KMP_AFFINITY` | Fixa threads OpenMP a núcleos físicos |
| `OMP_WAIT_POLICY=active` | Threads em spin-wait (menos latência) |
| `KMP_BLOCKTIME` | Tempo de bloqueio de thread controlado |
| `OMP_PLACES=cores` | Otimização NUMA para CPUs multi-socket |
| `-bind-to core` (MPI) | Cada processo MPI fixado a um núcleo |

## Relatório final

Exemplo de saída ao final de uma fila:

```
=======================================================
  RELATÓRIO FINAL DA FILA
=======================================================
  [  OK  ] Caso_01.fds                         00h 18m 42s
  [  OK  ] Caso_02.fds                         00h 32m 11s
  [FALHOU] Caso_03.fds                         --
-------------------------------------------------------
  TEMPO TOTAL (simulações OK):                 00h 50m 53s
=======================================================
```
