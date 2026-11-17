from radar import banco as banco_mod

from unittest.mock import Mock, patch

import pytest



def test_21_inicializacao_fecha_banco_se_migracao_falhar():
    banco = Mock()
    banco.execute.side_effect = banco_mod.sqlite3.OperationalError("Falha de migração")
    with patch.object(banco_mod.sqlite3, "connect", return_value=banco):
        with pytest.raises(banco_mod.sqlite3.OperationalError):
            banco_mod.iniciar_banco(":memory:")
    banco.rollback.assert_called_once()
    banco.close.assert_called_once()
