"""Carrega a versão operacional do Radar para os testes."""

from __future__ import annotations

import importlib.util
import sqlite3
import socket
import sys
from pathlib import Path

import pytest
import requests

PASTA_V5 = Path(__file__).resolve().parents[1]
if str(PASTA_V5) not in sys.path:
    sys.path.insert(0, str(PASTA_V5))

# O nome do arquivo inclui a versão 5.1; os testes mantêm o alias de importação.
spec = importlib.util.spec_from_file_location("radar_pncp_v5", PASTA_V5 / "radar_pncp_v5.1.py")
radar = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = radar
spec.loader.exec_module(radar)

from radar import banco as banco_mod, coleta as coleta_mod, configuracao as configuracao_mod


@pytest.fixture(autouse=True)
def bloquear_rede(monkeypatch):
    def proibido(*args, **kwargs):
        raise AssertionError("Requisições reais são proibidas nos testes")

    monkeypatch.setattr(requests.sessions.Session, "request", proibido)
    monkeypatch.setattr(socket.socket, "connect", proibido)
    monkeypatch.setattr(socket.socket, "connect_ex", proibido)


@pytest.fixture
def coleta_memoria(monkeypatch):
    conn = banco_mod.iniciar_banco(":memory:")

    class Banco(sqlite3.Connection):
        fechamentos = 0

        def close(self):
            self.fechamentos += 1

    banco = sqlite3.connect(":memory:", factory=Banco)
    conn.backup(banco)
    conn.close()
    monkeypatch.setattr(banco_mod, "iniciar_banco", lambda *a, **k: banco)
    config = configuracao_mod.carregar_configuracao()
    config["ufs"], config["modalidades"] = ["DF"], [4]
    monkeypatch.setattr(configuracao_mod, "configuracao_padrao", lambda: config)
    monkeypatch.setattr(coleta_mod.time, "sleep", lambda segundos: None)
    yield banco
    sqlite3.Connection.close(banco)
