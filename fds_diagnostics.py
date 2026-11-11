"""Diagnosticos estruturados e explicados para falhas do FDS.

O ``fds_run.log`` continua sendo o registro tecnico completo. Este modulo
grava, ao lado dele, eventos JSONL pequenos e associados ao caso correto para
que a interface possa mostrar a mensagem literal do FDS, uma explicacao em
portugues e passos de verificacao sem tentar interpretar o log inteiro.

Todas as gravacoes sao *best effort*: nunca se permite que uma falha no
diagnostico interrompa ou altere o resultado da simulacao.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import threading
import uuid
from typing import Iterable

from fds_reader import encontrar_namelists, remover_comentarios_fortran


NOME_ARQUIVO = "fds_diagnosticos.jsonl"
CHAVE_CAMINHO = "_diagnosticos_path"
CHAVE_SESSAO = "_diagnosticos_session_id"

_LOCK = threading.Lock()
_RE_ERRO_FDS = re.compile(r"^\s*ERROR(?:\((\d+)\))?:\s*(.*)$", re.IGNORECASE)
_RE_CHID = re.compile(
    r"(?<![A-Z0-9_%])CHID\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL
)


def _agora() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _limitar_linhas(linhas: Iterable[str], limite: int = 60) -> list[str]:
    resultado = []
    for linha in linhas:
        texto = str(linha).rstrip("\r\n")
        if texto:
            resultado.append(texto[:4000])
    return resultado[-limite:]


def caminho_relatorio(pasta_log: str) -> str:
    return os.path.join(os.path.abspath(pasta_log), NOME_ARQUIVO)


def iniciar_sessao(cfg: dict, pasta_log: str) -> str:
    """Inicia o relatorio da fila atual e devolve seu caminho."""
    caminho = caminho_relatorio(pasta_log)
    sessao = uuid.uuid4().hex
    cfg[CHAVE_CAMINHO] = caminho
    cfg[CHAVE_SESSAO] = sessao
    evento = {
        "schema": 1,
        "id": uuid.uuid4().hex,
        "sessao": sessao,
        "data": _agora(),
        "tipo": "sessao",
        "titulo": "Nova execução iniciada",
    }
    try:
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        with _LOCK, open(caminho, "w", encoding="utf-8", newline="\n") as arquivo:
            arquivo.write(json.dumps(evento, ensure_ascii=False) + "\n")
            arquivo.flush()
            os.fsync(arquivo.fileno())
    except OSError as exc:
        cfg[CHAVE_CAMINHO] = ""
        logging.getLogger(__name__).warning(
            "Nao foi possivel iniciar o relatorio amigavel de erros: %s", exc
        )
    return caminho


def ler_eventos(caminho: str) -> list[dict]:
    """Le eventos validos, ignorando com seguranca linhas parciais."""
    eventos: list[dict] = []
    try:
        with open(caminho, "r", encoding="utf-8", errors="replace") as arquivo:
            for linha in arquivo:
                try:
                    evento = json.loads(linha)
                except (ValueError, TypeError):
                    continue
                if isinstance(evento, dict):
                    eventos.append(evento)
    except OSError:
        pass
    return eventos


def _registrar(cfg: dict | None, evento: dict) -> None:
    if not isinstance(cfg, dict):
        return
    caminho = str(cfg.get(CHAVE_CAMINHO, "") or "")
    if not caminho:
        return
    evento = {
        "schema": 1,
        "id": uuid.uuid4().hex,
        "sessao": str(cfg.get(CHAVE_SESSAO, "") or ""),
        "data": _agora(),
        **evento,
    }
    try:
        with _LOCK, open(caminho, "a", encoding="utf-8", newline="\n") as arquivo:
            arquivo.write(json.dumps(evento, ensure_ascii=False) + "\n")
            arquivo.flush()
    except OSError:
        logging.getLogger(__name__).debug(
            "Nao foi possivel gravar um diagnostico.", exc_info=True
        )


def _extrair_chid(arquivo_fds: str) -> str:
    try:
        with open(arquivo_fds, "r", encoding="latin-1", errors="replace") as arquivo:
            conteudo = arquivo.read()
        blocos = encontrar_namelists(conteudo, "HEAD")
        if not blocos:
            return os.path.splitext(os.path.basename(arquivo_fds))[0]
        corpo = remover_comentarios_fortran(blocos[0].corpo)
        match = _RE_CHID.search(corpo)
        if match and match.group(2).strip():
            return os.path.basename(match.group(2).strip())
    except (OSError, ValueError):
        pass
    return os.path.splitext(os.path.basename(arquivo_fds))[0]


def caminho_saida_out(arquivo_fds: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(arquivo_fds)), _extrair_chid(arquivo_fds) + ".out")


def _mensagem_principal(mensagem: str | None, contexto: list[str], codigo: int | None) -> str:
    if mensagem and str(mensagem).strip():
        return str(mensagem).strip()
    for linha in contexto:
        if _RE_ERRO_FDS.match(linha):
            return linha.strip()
    for linha in reversed(contexto):
        texto = linha.strip()
        if texto:
            return texto
    return f"O processo do FDS encerrou com o código {codigo}." if codigo is not None else "O FDS encerrou sem informar uma mensagem específica."


def classificar_erro(mensagem: str, contexto: Iterable[str] = ()) -> dict:
    """Traduz categorias comuns sem esconder nem reescrever o texto original."""
    texto = "\n".join([str(mensagem), *(str(l) for l in contexto)]).lower()
    match_codigo = _RE_ERRO_FDS.match(str(mensagem))
    codigo_fds = match_codigo.group(1) if match_codigo else None

    titulo = "O FDS interrompeu a simulação"
    explicacao = (
        "O FDS encontrou uma condição que impediu a continuidade. A mensagem "
        "original abaixo é a informação mais importante para localizar a causa."
    )
    acoes = [
        "Leia a mensagem original e procure nela o nome de um ID, arquivo, mesh ou parâmetro.",
        "Abra o arquivo .out e examine as linhas imediatamente anteriores ao encerramento.",
        "Corrija o arquivo .fds e execute somente este caso para confirmar a solução.",
    ]

    if any(chave in texto for chave in ("out of memory", "insufficient memory", "cannot allocate", "allocation failed", "forrtl: severe (41)")):
        titulo = "Memória insuficiente para o caso"
        explicacao = "O FDS ou uma biblioteca não conseguiu reservar a memória necessária para continuar."
        acoes = [
            "Feche outros programas e confirme a memória disponível no Gerenciador de Tarefas.",
            "Reduza temporariamente o número de processos MPI; cada processo pode aumentar o consumo de memória.",
            "Se necessário, reduza o número de células ou execute o caso em uma máquina com mais RAM.",
        ]
    elif any(chave in texto for chave in ("numerical instability", "numerical instability detected", "floating invalid", "floating-point invalid")):
        titulo = "Instabilidade numérica detectada"
        explicacao = "A solução numérica deixou de permanecer estável; isso normalmente está relacionado à malha, condições de contorno ou parâmetros físicos do modelo."
        acoes = [
            "No .out, identifique o instante e a mesh indicados imediatamente antes da instabilidade.",
            "Revise resolução da malha, obstruções/aberturas muito pequenas, velocidades e condições iniciais nessa região.",
            "Compare com uma versão conhecida do caso e faça uma execução curta antes de retomar a fila.",
        ]
    elif any(chave in texto for chave in ("mpi_process", "mpi process", "mpi_abort", "mpiexec", "hydra", "pmi_", "rank ")):
        titulo = "Problema na configuração ou execução MPI"
        explicacao = "A falha envolve a distribuição das meshes entre processos ou a comunicação/inicialização do MPI."
        acoes = [
            "Confira se MPI_PROCESS começa em 0, é contínuo e não usa mais processos do que meshes efetivas.",
            "Execute o diagnóstico do computador para confirmar a instalação do FDS e do MPI.",
            "Teste o mesmo caso em OpenMP; se funcionar, revise especificamente a configuração MPI e o balanceamento das meshes.",
        ]
    elif any(chave in texto for chave in ("cannot open", "could not open", "file not found", "no such file", "cannot read", "failed to open")):
        titulo = "Arquivo necessário não foi encontrado ou não pôde ser aberto"
        explicacao = "O caso referencia um arquivo auxiliar ausente, movido, bloqueado ou com caminho diferente do esperado."
        acoes = [
            "Localize na mensagem original o nome exato do arquivo solicitado.",
            "Confirme que ele está na pasta do caso e que não está sendo bloqueado por outro programa.",
            "Revise no .fds o caminho e a grafia, incluindo extensão e subpastas.",
        ]
    elif any(chave in texto for chave in ("not found", "not defined", "undefined", "unknown id", "no matching")):
        titulo = "Referência ou identificador não encontrado"
        explicacao = "Uma linha do caso usa um ID ou objeto que o FDS não encontrou entre as definições lidas."
        acoes = [
            "Copie da mensagem original o ID informado e pesquise por ele no arquivo .fds.",
            "Confira grafia, espaços, maiúsculas/minúsculas e se a definição aparece antes do uso quando exigido.",
            "Verifique também arquivos incluídos por CATF ou outros arquivos auxiliares.",
        ]
    elif "mesh" in texto and any(chave in texto for chave in ("overlap", "align", "boundary", "bounds", "dimension", "ijk", "xb", "cell")):
        titulo = "Inconsistência na definição das meshes"
        explicacao = "O FDS identificou uma incompatibilidade geométrica, dimensional ou de resolução em uma ou mais meshes."
        acoes = [
            "Anote o número/ID da mesh citado na mensagem e localize o bloco &MESH correspondente.",
            "Revise IJK, XB, sobreposições e o alinhamento das células nas interfaces.",
            "Compare o .fds atual com o backup .fds.bak antes de alterar a geometria.",
        ]
    elif any(chave in texto for chave in ("namelist", "problem with", "read error", "syntax error", "bad value")):
        titulo = "Parâmetro ou bloco do arquivo FDS inválido"
        explicacao = "O leitor de entrada do FDS não conseguiu interpretar um namelist, parâmetro ou valor."
        acoes = [
            "Identifique na mensagem o bloco citado, por exemplo &MESH, &OBST, &SURF ou &DEVC.",
            "Revise vírgulas, aspas, nomes dos campos, tipos dos valores e o terminador '/' desse bloco.",
            "Execute somente o caso corrigido e confirme que a leitura inicial é concluída.",
        ]

    return {
        "titulo": titulo,
        "explicacao": explicacao,
        "acoes": acoes,
        "codigo_fds": codigo_fds,
    }


def registrar_erro_fds(
    cfg: dict,
    arquivo_fds: str,
    codigo_retorno: int | None,
    elapsed: float,
    mensagem: str | None,
    contexto: Iterable[str],
) -> None:
    linhas = _limitar_linhas(contexto)
    original = _mensagem_principal(mensagem, linhas, codigo_retorno)
    classificacao = classificar_erro(original, linhas)
    _registrar(cfg, {
        "tipo": "erro_fds",
        "severidade": "erro",
        "fonte": "FDS",
        "caso": os.path.basename(arquivo_fds),
        "arquivo_fds": os.path.abspath(arquivo_fds),
        "arquivo_out": caminho_saida_out(arquivo_fds),
        "codigo_retorno": codigo_retorno,
        "codigo_fds": classificacao["codigo_fds"],
        "duracao_s": max(0.0, float(elapsed)),
        "mensagem_original": original,
        "contexto": linhas,
        "titulo": classificacao["titulo"],
        "explicacao": classificacao["explicacao"],
        "acoes": classificacao["acoes"],
    })


def registrar_erro_automacao(
    cfg: dict,
    arquivo_fds: str,
    mensagem: str,
    contexto: Iterable[str] = (),
) -> None:
    _registrar(cfg, {
        "tipo": "erro_automacao",
        "severidade": "erro",
        "fonte": "FDS Automation",
        "caso": os.path.basename(arquivo_fds),
        "arquivo_fds": os.path.abspath(arquivo_fds),
        "arquivo_out": caminho_saida_out(arquivo_fds),
        "mensagem_original": str(mensagem),
        "contexto": _limitar_linhas(contexto),
        "titulo": "O caso não pôde ser preparado ou iniciado",
        "explicacao": "A falha ocorreu antes ou fora do cálculo principal do FDS.",
        "acoes": [
            "Confira a mensagem original abaixo e os caminhos de arquivos mencionados.",
            "Verifique a instalação do FDS, permissões da pasta e arquivos auxiliares do caso.",
            "Depois da correção, execute somente este caso para validar.",
        ],
    })


def registrar_interrupcao(cfg: dict, arquivo_fds: str) -> None:
    _registrar(cfg, {
        "tipo": "interrompida",
        "severidade": "aviso",
        "fonte": "Usuário",
        "caso": os.path.basename(arquivo_fds),
        "arquivo_fds": os.path.abspath(arquivo_fds),
        "arquivo_out": caminho_saida_out(arquivo_fds),
        "mensagem_original": "A simulação foi interrompida pelo botão 'Parar caso atual'.",
        "contexto": [],
        "titulo": "Simulação interrompida; a fila continuou",
        "explicacao": "Este caso não falhou por um erro do FDS. A interrupção foi solicitada manualmente e os casos seguintes continuaram normalmente.",
        "acoes": [
            "Se quiser retomar, execute novamente este arquivo .fds.",
            "Considere remover arquivos de saída incompletos antes de reiniciar, salvo quando estiver usando RESTART de forma planejada.",
        ],
    })


def registrar_resumo(cfg: dict, concluidas: int, falhas: int, interrompidas: int) -> None:
    _registrar(cfg, {
        "tipo": "resumo",
        "concluidas": int(concluidas),
        "falhas": int(falhas),
        "interrompidas": int(interrompidas),
        "titulo": "Fila finalizada",
    })
