# monitor_xlsx.py — Acompanhamento das simulações em planilha compartilhada
#
# Grava o andamento da fila em um arquivo .xlsx que pode ficar em uma
# pasta sincronizada (Google Drive para Desktop, OneDrive, rede). Varios
# computadores apontam para a MESMA planilha: cada um atualiza apenas as
# suas proprias linhas (chave = PC + nome do caso), entao a equipe ve em
# um so lugar o que esta rodando, o que terminou e o que deu erro em
# cada maquina.
#
# Colunas: Caso | Status | Progresso | Tempo restante | Usuário | Empresa
#          | PC | Início | Fim | Duração | Erro | Atualizado em
#
# Regras de convivencia:
#   - Best-effort SEMPRE: se a planilha estiver aberta no Excel ou o
#     Drive estiver sincronizando, tenta de novo e, se nao der, segue a
#     fila normalmente (avisa uma unica vez no log).
#   - Atualizacoes de progresso sao limitadas por intervalo_seg para nao
#     sobrecarregar a sincronizacao do Drive.
import datetime
import logging
import os
import tempfile
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    _OPENPYXL_OK = True
except ImportError:
    _OPENPYXL_OK = False

NOME_ABA = "Simulacoes"

CABECALHO = [
    "Caso", "Status", "Progresso", "Tempo restante", "Usuário", "Empresa",
    "PC", "Início", "Fim", "Duração", "Erro", "Atualizado em",
]
COL = {nome: i + 1 for i, nome in enumerate(CABECALHO)}  # 1-based

# Cor de fundo por status (ARGB sem alpha)
_CORES_STATUS = {
    "NA FILA":    "FFF9E6B3",  # amarelo claro
    "RODANDO":    "FFBBDEFB",  # azul claro
    "CONCLUÍDA":  "FFC8E6C9",  # verde claro
    "ERRO":       "FFFFCDD2",  # vermelho claro
    "CANCELADA":  "FFE0E0E0",  # cinza
}

LARGURAS = {
    "Caso": 42, "Status": 12, "Progresso": 22, "Tempo restante": 14,
    "Usuário": 18, "Empresa": 18, "PC": 16, "Início": 17, "Fim": 17,
    "Duração": 12, "Erro": 50, "Atualizado em": 17,
}


def _agora_txt() -> str:
    return datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def _fmt_duracao(segundos: float) -> str:
    segundos = max(0, int(segundos))
    h, m, s = segundos // 3600, (segundos % 3600) // 60, segundos % 60
    return f"{h:02d}h {m:02d}m {s:02d}s"


def obter_cfg_monitor(cfg: dict | None) -> dict:
    if not isinstance(cfg, dict):
        return {}
    return cfg.get("monitoramento", {}) or {}


@contextmanager
def _bloqueio_planilha(caminho: str, timeout_seg: float = 5.0):
    """Lock cooperativo entre processos para pasta local ou compartilhada."""
    lock_path = caminho + ".lock"
    inicio = time.monotonic()
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        except FileExistsError:
            try:
                # Recupera lock abandonado por queda do processo.
                if time.time() - os.path.getmtime(lock_path) > 120:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            if time.monotonic() - inicio >= timeout_seg:
                raise PermissionError("planilha ocupada por outra atualizacao")
            time.sleep(0.2)

    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(lock_path)
        except OSError:
            pass


class MonitorPlanilha:
    """
    Uma instancia por fila de simulacoes. Todos os metodos sao
    best-effort: nunca levantam excecao para o chamador.
    """

    def __init__(self, cfg: dict):
        mon = obter_cfg_monitor(cfg)
        self.caminho = str(mon.get("arquivo_xlsx", "") or "").strip()
        self.ativo = bool(mon.get("ativo", False)) and bool(self.caminho)

        if self.ativo and not _OPENPYXL_OK:
            logger.warning(
                "Monitoramento em planilha desativado: biblioteca 'openpyxl' "
                "nao instalada (pip install openpyxl)."
            )
            self.ativo = False

        self.usuario = str(mon.get("usuario", "") or "")
        self.empresa = str(mon.get("empresa", "") or "")
        self.pc = str(mon.get("pc", "") or "") or os.environ.get(
            "COMPUTERNAME", ""
        ) or "PC"
        self.pasta_base = os.path.abspath(str(cfg.get("pasta_simulacoes", "") or ""))
        try:
            self.intervalo = max(10.0, float(mon.get("intervalo_seg", 60)))
        except (TypeError, ValueError):
            self.intervalo = 60.0

        self._ultimo_progresso: dict[str, float] = {}   # caso -> timestamp
        self._intervalo_tempo: dict[str, tuple[float, float] | None] = {}
        self._avisou_falha = False

        if self.ativo:
            logger.info(
                f"Acompanhamento em planilha ativo: {self.caminho} "
                f"(PC: {self.pc})",
                extra={"console_visible": True},
            )

    # -- API usada pelo main.py ------------------------------------------------
    def _nome_caso(self, arquivo_fds: str) -> str:
        caminho = os.path.abspath(arquivo_fds)
        if self.pasta_base:
            try:
                relativo = os.path.relpath(caminho, self.pasta_base)
                if relativo != ".." and not relativo.startswith(".." + os.sep):
                    return relativo.replace("\\", "/")
            except (OSError, ValueError):
                pass
        return os.path.basename(caminho)

    def iniciar_fila(self, arquivos_fds: list[str]) -> None:
        if not self.ativo:
            return
        campos_por_caso = {}
        for arq in arquivos_fds:
            campos_por_caso[self._nome_caso(arq)] = {
                "Status": "NA FILA", "Progresso": "", "Tempo restante": "",
                "Início": "", "Fim": "", "Duração": "", "Erro": "",
            }
        self._salvar(campos_por_caso)

    def iniciar_caso(
        self, arquivo_fds: str, intervalo_tempo: tuple[float, float] | None
    ) -> None:
        if not self.ativo:
            return
        caso = self._nome_caso(arquivo_fds)
        self._intervalo_tempo[caso] = intervalo_tempo
        self._ultimo_progresso[caso] = 0.0
        self._salvar({caso: {
            "Status": "RODANDO", "Início": _agora_txt(), "Fim": "",
            "Duração": "", "Erro": "", "Progresso": "iniciando...",
            "Tempo restante": "",
        }})

    def progresso(self, arquivo_fds: str, tempo_simulado: float, decorrido: float) -> None:
        """Atualiza % simulada e ETA. Limitado por intervalo_seg."""
        if not self.ativo:
            return
        caso = self._nome_caso(arquivo_fds)
        agora = time.time()
        if agora - self._ultimo_progresso.get(caso, 0.0) < self.intervalo:
            return
        self._ultimo_progresso[caso] = agora

        intervalo = self._intervalo_tempo.get(caso)
        if intervalo:
            t_begin, t_end = intervalo
            total = max(1e-9, t_end - t_begin)
            pct = max(0.0, min(100.0, (tempo_simulado - t_begin) / total * 100.0))
            progresso_txt = f"{pct:.0f}% ({tempo_simulado:g}s de {t_end:g}s)"
            avancado = tempo_simulado - t_begin
            restante_txt = (
                _fmt_duracao(decorrido * max(0.0, t_end - tempo_simulado) / avancado)
                if avancado > 0 and decorrido > 0 else ""
            )
        else:
            progresso_txt = f"{tempo_simulado:g}s simulados"
            restante_txt = ""

        self._salvar({caso: {
            "Status": "RODANDO",
            "Progresso": progresso_txt,
            "Tempo restante": restante_txt,
        }})

    def finalizar_caso(
        self, arquivo_fds: str, sucesso: bool, duracao_s: float, erro: str = ""
    ) -> None:
        if not self.ativo:
            return
        caso = self._nome_caso(arquivo_fds)
        if sucesso:
            campos = {
                "Status": "CONCLUÍDA", "Progresso": "100%",
                "Tempo restante": "", "Fim": _agora_txt(),
                "Duração": _fmt_duracao(duracao_s), "Erro": "",
            }
        else:
            campos = {
                "Status": "ERRO", "Progresso": "", "Tempo restante": "",
                "Fim": _agora_txt(),
                "Duração": _fmt_duracao(duracao_s) if duracao_s > 0 else "",
                "Erro": (erro or "ver fds_run.log")[:250],
            }
        self._salvar({caso: campos})

    def cancelar_caso(self, arquivo_fds: str) -> None:
        if not self.ativo:
            return
        caso = self._nome_caso(arquivo_fds)
        self._salvar({caso: {
            "Status": "CANCELADA", "Tempo restante": "",
            "Fim": _agora_txt(), "Erro": "interrompida pelo usuario",
        }})

    def cancelar_fila(self, arquivos_fds: list[str]) -> None:
        """Marca como cancelados apenas os casos pendentes desta fila/PC."""
        if not self.ativo:
            return
        alvos = {self._nome_caso(arquivo) for arquivo in arquivos_fds}
        if not alvos:
            return
        registros = ler_planilha(self.caminho)
        campos = {}
        for registro in registros:
            caso = registro.get("Caso", "")
            if (
                registro.get("PC", "") == self.pc
                and caso in alvos
                and registro.get("Status", "") in {"RODANDO", "NA FILA"}
            ):
                campos[caso] = {
                    "Status": "CANCELADA",
                    "Progresso": "",
                    "Tempo restante": "",
                    "Fim": _agora_txt(),
                    "Erro": "interrompida pelo usuario",
                }
        if campos:
            self._salvar(campos)

    # -- gravacao ---------------------------------------------------------------
    def _salvar(self, campos_por_caso: dict[str, dict]) -> None:
        """Aplica atualizacoes (com retry) sem nunca interromper a fila."""
        for tentativa in range(3):
            try:
                self._salvar_impl(campos_por_caso)
                return
            except PermissionError:
                # Planilha aberta no Excel ou bloqueada pela sincronizacao
                time.sleep(2.0)
            except Exception:
                logger.debug("Falha ao atualizar planilha.", exc_info=True)
                break

        if not self._avisou_falha:
            self._avisou_falha = True
            logger.warning(
                "Nao consegui atualizar a planilha de acompanhamento "
                "(ela esta aberta no Excel? sem acesso a pasta?). "
                "A fila continua normalmente.",
            )

    def _salvar_impl(self, campos_por_caso: dict[str, dict]) -> None:
        pasta = os.path.dirname(os.path.abspath(self.caminho))
        os.makedirs(pasta, exist_ok=True)

        with _bloqueio_planilha(self.caminho):
            wb, ws = _abrir_ou_criar(self.caminho)

            # Indexa as linhas deste PC: (caso) -> numero da linha
            linhas_pc: dict[str, int] = {}
            for row in range(2, ws.max_row + 1):
                pc = str(ws.cell(row=row, column=COL["PC"]).value or "")
                caso = str(ws.cell(row=row, column=COL["Caso"]).value or "")
                if pc == self.pc and caso:
                    linhas_pc[caso] = row

            for caso, campos in campos_por_caso.items():
                row = linhas_pc.get(caso)
                if row is None:
                    row = ws.max_row + 1
                    ws.cell(row=row, column=COL["Caso"], value=caso)
                    linhas_pc[caso] = row

                # identidade sempre atualizada (usuario pode ter corrigido o nome)
                ws.cell(row=row, column=COL["Usuário"], value=self.usuario)
                ws.cell(row=row, column=COL["Empresa"], value=self.empresa)
                ws.cell(row=row, column=COL["PC"], value=self.pc)
                ws.cell(row=row, column=COL["Atualizado em"], value=_agora_txt())

                for nome_col, valor in campos.items():
                    ws.cell(row=row, column=COL[nome_col], value=valor)

                status = str(ws.cell(row=row, column=COL["Status"]).value or "")
                cor = _CORES_STATUS.get(status)
                fill = (
                    PatternFill(start_color=cor, end_color=cor, fill_type="solid")
                    if cor else PatternFill(fill_type=None)
                )
                for col in range(1, len(CABECALHO) + 1):
                    ws.cell(row=row, column=col).fill = fill

            temporario = None
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".xlsx", prefix="fds_monitor_", dir=pasta, delete=False
                ) as tmp:
                    temporario = tmp.name
                wb.save(temporario)
                wb.close()
                os.replace(temporario, self.caminho)
                temporario = None
            finally:
                try:
                    wb.close()
                except Exception:
                    pass
                if temporario:
                    try:
                        os.remove(temporario)
                    except OSError:
                        pass


def _abrir_ou_criar(caminho: str):
    """Abre a planilha; cria com cabecalho formatado se nao existir."""
    if os.path.exists(caminho):
        wb = openpyxl.load_workbook(caminho)
        if NOME_ABA in wb.sheetnames:
            return wb, wb[NOME_ABA]
        ws = wb.create_sheet(NOME_ABA)
    else:
        pasta = os.path.dirname(caminho)
        if pasta:
            os.makedirs(pasta, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = NOME_ABA

    # Cabecalho novo
    negrito = Font(bold=True, color="FFFFFFFF")
    fundo = PatternFill(start_color="FF37474F", end_color="FF37474F", fill_type="solid")
    for i, nome in enumerate(CABECALHO, start=1):
        cel = ws.cell(row=1, column=i, value=nome)
        cel.font = negrito
        cel.fill = fundo
        cel.alignment = Alignment(horizontal="center")
        ws.column_dimensions[cel.column_letter].width = LARGURAS.get(nome, 15)
    ws.freeze_panes = "A2"
    return wb, ws


def ler_planilha(caminho: str, propagar_erros: bool = False) -> list[dict]:
    """
    Le a planilha inteira (todas as linhas, de todos os PCs) para exibir
    na aba de acompanhamento da interface. Retorna lista de dicts pelas
    colunas do cabecalho. Best-effort: erro -> lista vazia.
    """
    if not _OPENPYXL_OK:
        if propagar_erros:
            raise RuntimeError("biblioteca openpyxl nao instalada")
        return []
    if not caminho or not os.path.exists(caminho):
        return []
    wb = None
    try:
        wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
        if NOME_ABA not in wb.sheetnames:
            return []
        ws = wb[NOME_ABA]
        linhas = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or not row[0]:
                continue
            registro = {}
            for i, nome in enumerate(CABECALHO):
                registro[nome] = str(row[i]) if i < len(row) and row[i] is not None else ""
            linhas.append(registro)
        return linhas
    except Exception:
        if propagar_erros:
            raise
        return []
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass
