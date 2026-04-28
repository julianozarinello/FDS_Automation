# FDS Automation v1.12

Gerenciador de simulações [Fire Dynamics Simulator (FDS)](https://pages.nist.gov/fds-smv/) em Python, com suporte a fila sequencial de simulações, múltiplos modos de paralelismo e otimizações de performance.

## Requisitos

```bash
pip install pyyaml psutil
```

> **Nota para distribuição com PyInstaller:** adicionar pedido de permissão ao usuário antes de copiar binários.

## Como usar

1. Edite o `config.yaml` com os caminhos e configurações.
2. Execute:

```bash
python main.py
# ou com config customizado:
python main.py --config meu_caso.yaml
```

Um arquivo `fds_run.log` é gerado automaticamente na pasta do projeto.

## Fila de simulações

Para rodar múltiplos casos em sequência, use a chave `simulacoes` no `config.yaml`:

```yaml
simulacoes:
  - "C:/Projetos/FDS/Caso_01.fds"
  - "C:/Projetos/FDS/Caso_02.fds"
  - "C:/Projetos/FDS/Caso_03.fds"
```

Ao final da fila, um relatório consolidado é exibido com o tempo de cada simulação e o tempo total.

## Modos de execução

| Modo | Descrição |
|---|---|
| `SERIAL` | Um único processo, sem paralelismo |
| `OPENMP` | Múltiplas threads em memória compartilhada |
| `MPI` | Múltiplos processos (1 por mesh recomendado) |
| `HYBRID` | Combinação de processos MPI com threads OpenMP |

## Otimizações de performance (v1.12)

| Otimização | Descrição |
|---|---|
| **Prioridade HIGH** | Processo elevado a alta prioridade no SO |
| `KMP_AFFINITY` | Fixa threads OpenMP a núcleos físicos |
| `OMP_WAIT_POLICY=active` | Threads em spin-wait (menos latência) |
| `KMP_BLOCKTIME` | Tempo de bloqueio de thread controlado |
| `OMP_PLACES=cores` | Otimização NUMA para CPUs multi-socket |
| `-bind-to core` (MPI) | Cada processo MPI fixado a um núcleo |

## Relatório final

Exemplo de saída ao final de uma fila com 3 simulações:

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
