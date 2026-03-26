import sys

import packages.registration_core.engine as engine_module


def test_registration_core_can_run_without_web_route_import():
    assert engine_module.RegistrationEngine is not None
    assert not any(name == "src.web" or name.startswith("src.web.") for name in sys.modules)
