"""Canal simples e seguro entre a interface e o worker da fila.

A GUI e a fila rodam em processos diferentes. Para interromper somente o
caso atual sem encerrar a fila inteira, a GUI grava uma solicitacao JSON em
um arquivo exclusivo daquela execucao. O worker so aceita a solicitacao se
o indice e o nome ainda corresponderem ao caso em andamento; assim um clique
feito durante a troca de casos nunca interrompe acidentalmente o proximo.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from typing import Any


CHAVE_CAMINHO = "_controle_caso_path"
CHAVE_CASO_ATUAL = "_controle_caso_atual"


class PularCasoSolicitado(Exception):
    """Interrupcao esperada: o usuario pediu para seguir ao proximo caso."""


def novo_caminho_controle() -> str:
    """Gera um caminho exclusivo sem criar o arquivo de solicitacao."""
    nome = f"fds_automation_{os.getpid()}_{uuid.uuid4().hex}.control.json"
    return os.path.join(tempfile.gettempdir(), nome)


def configurar_caso_atual(
    cfg: dict, arquivo_fds: str, indice: int, total: int
) -> dict:
    """Anexa ao cfg os dados usados para validar a solicitacao da GUI."""
    cfg[CHAVE_CASO_ATUAL] = {
        "indice": int(indice),
        "total": int(total),
        "nome": os.path.basename(arquivo_fds),
        "caminho": os.path.normcase(os.path.abspath(arquivo_fds)),
    }
    return cfg


def limpar_solicitacao(caminho: str | None) -> None:
    if not caminho:
        return
    try:
        os.remove(caminho)
    except FileNotFoundError:
        pass
    except OSError:
        # O proximo ciclo tentara novamente; falhar ao limpar nunca deve
        # derrubar uma simulacao.
        pass


def solicitar_pulo(caminho: str, indice: int, nome: str) -> None:
    """Publica atomicamente um pedido para interromper o caso indicado."""
    if not caminho:
        raise ValueError("Canal de controle da fila nao foi inicializado.")

    pasta = os.path.dirname(os.path.abspath(caminho))
    os.makedirs(pasta, exist_ok=True)
    payload = {
        "acao": "pular_caso",
        "indice": int(indice),
        "nome": os.path.basename(str(nome)),
        "solicitado_em": time.time(),
    }

    temporario = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=pasta,
            prefix=os.path.basename(caminho) + ".",
            suffix=".tmp",
        ) as arquivo:
            temporario = arquivo.name
            json.dump(payload, arquivo, ensure_ascii=False)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, caminho)
        temporario = None
    finally:
        if temporario:
            try:
                os.remove(temporario)
            except OSError:
                pass


def _ler_payload(caminho: str) -> dict[str, Any] | None:
    try:
        with open(caminho, "r", encoding="utf-8") as arquivo:
            dados = json.load(arquivo)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        # Um arquivo truncado/malformado nao pode ficar cancelando casos.
        limpar_solicitacao(caminho)
        return None
    return dados if isinstance(dados, dict) else None


def consumir_pulo_se_for_caso_atual(cfg: dict | None) -> bool:
    """Consome o pedido somente quando ele pertence ao caso em andamento."""
    if not isinstance(cfg, dict):
        return False
    caminho = str(cfg.get(CHAVE_CAMINHO, "") or "")
    atual = cfg.get(CHAVE_CASO_ATUAL) or {}
    if not caminho or not isinstance(atual, dict):
        return False

    pedido = _ler_payload(caminho)
    if not pedido:
        return False

    try:
        indice_pedido = int(pedido.get("indice"))
        indice_atual = int(atual.get("indice"))
    except (TypeError, ValueError):
        limpar_solicitacao(caminho)
        return False

    nome_pedido = os.path.normcase(os.path.basename(str(pedido.get("nome", ""))))
    nome_atual = os.path.normcase(os.path.basename(str(atual.get("nome", ""))))
    corresponde = (
        pedido.get("acao") == "pular_caso"
        and indice_pedido == indice_atual
        and bool(nome_pedido)
        and nome_pedido == nome_atual
    )

    if corresponde:
        limpar_solicitacao(caminho)
        return True

    # Um pedido para um indice ja ultrapassado ficou obsoleto na pequena
    # janela entre o fim de um caso e o inicio do seguinte. Remove-o para
    # garantir que nunca afete outra simulacao.
    if indice_pedido <= indice_atual:
        limpar_solicitacao(caminho)
    return False


def verificar_pulo(cfg: dict | None) -> None:
    """Levanta a excecao de fluxo esperada quando a GUI pediu o pulo."""
    if consumir_pulo_se_for_caso_atual(cfg):
        raise PularCasoSolicitado()
