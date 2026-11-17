from radar import banco as banco_mod, cli as cli_mod

import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

import radar_pncp_v5 as radar

import pytest


def test_24_importar_nao_le_configuracao_nem_inicializa_http_ou_logging():
    spec = importlib.util.spec_from_file_location("radar_sem_efeitos", radar.__file__)
    modulo = importlib.util.module_from_spec(spec)
    with (
        patch.object(Path, "open", side_effect=AssertionError("Importação leu arquivo")),
        patch("requests.Session") as sessao,
        patch("logging.basicConfig") as configurar_log,
    ):
        spec.loader.exec_module(modulo)
        sessao.assert_not_called()
        configurar_log.assert_not_called()


def test_24_validacao_isolada_nao_abre_banco_nem_sessao(monkeypatch):
    monkeypatch.setattr("sys.argv", ["radar", "--validar-configuracao"])
    iniciar = Mock(side_effect=AssertionError("Banco não deve abrir"))
    monkeypatch.setattr(banco_mod, "iniciar_banco", iniciar)
    with patch("requests.Session") as sessao:
        assert cli_mod.main() == 0
        sessao.assert_not_called()


@pytest.mark.parametrize("valor", ["nan", "inf", "-1"])
def test_14_cli_recusa_prazo_nao_finito_ou_negativo(monkeypatch, valor):
    monkeypatch.setattr("sys.argv", ["radar", "--validar-configuracao", "--prazo-total", valor])
    with patch("requests.Session") as sessao:
        assert cli_mod.main() == 2
        sessao.assert_not_called()
