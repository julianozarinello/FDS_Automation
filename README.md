# FDS Automation

Gerenciador de simulações [Fire Dynamics Simulator (FDS)](https://pages.nist.gov/fds-smv/) em Python, com suporte a execução Serial, OpenMP, MPI e Híbrida (MPI+OpenMP).

## Requisitos

```bash
pip install pyyaml
```

> **Nota para distribuição com PyInstaller:** adicionar pedido de permissão ao usuário antes de copiar binários.

## Como usar

1. Edite o arquivo `config.yaml` com os caminhos e configurações da sua simulação.
2. Execute:

```bash
python main.py
# ou com config customizado:
python main.py --config meu_caso.yaml
```

Um arquivo `fds_run.log` será gerado automaticamente na pasta do projeto.

## Modos de execução

| Modo | Descrição |
|---|---|
| `SERIAL` | Um único processo, sem paralelismo |
| `OPENMP` | Múltiplas threads em memória compartilhada |
| `MPI` | Múltiplos processos (1 por mesh recomendado) |
| `HYBRID` | Combinação de processos MPI com threads OpenMP |

## Validações automáticas

- Verifica se o arquivo `.fds` existe antes de rodar
- Verifica se o diretório do FDS existe
- No modo MPI/HYBRID, alerta se o número de processos não bate com o número de meshes
- Registra tempo total de simulação no log
