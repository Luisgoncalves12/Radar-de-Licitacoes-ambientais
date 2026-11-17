"""Verifica a importação isolada e a entrada pública do projeto modular."""

import subprocess
import sys
from pathlib import Path


RAIZ = Path(__file__).resolve().parents[1]


def test_importacao_dos_modulos_nao_inicia_operacoes():
    # Processo novo evita que o cache do conftest esconda efeitos de importação.
    codigo = '''
import importlib
import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch
import requests
import pandas
import openpyxl

# As dependencias podem ler recursos internos (como fusos no Windows).
# O bloqueio abaixo verifica apenas efeitos da importacao dos modulos do Radar.
with (
    patch.object(Path, "open", side_effect=AssertionError("Arquivo aberto")),
    patch.object(sqlite3, "connect", side_effect=AssertionError("Banco aberto")),
    patch.object(requests, "Session", side_effect=AssertionError("HTTP iniciado")),
    patch.object(logging, "basicConfig", side_effect=AssertionError("Log configurado")),
):
    for nome in ("classificacao", "dados", "banco", "coleta", "planilhas", "auditoria"):
        importlib.import_module("radar." + nome)
        assert "radar.cli" not in sys.modules
    importlib.import_module("radar.cli")
'''
    resultado = subprocess.run(
        [sys.executable, "-c", codigo], cwd=RAIZ,
        capture_output=True, text=True, timeout=60,
    )
    assert resultado.returncode == 0, resultado.stderr


def test_entrada_versionada_funciona_fora_da_raiz(tmp_path):
    resultado = subprocess.run(
        [sys.executable, str(RAIZ / "radar_pncp_v5.1.py"), "--validar-configuracao"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert resultado.returncode == 0, resultado.stderr
    assert not (tmp_path / "radar.db").exists()
    assert not (tmp_path / "planilhas").exists()
