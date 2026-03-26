import importlib
import sys


def test_registration_core_can_run_without_web_route_import():
    for name in list(sys.modules):
        if name == "packages.registration_core.engine" or name.startswith("src.web."):
            sys.modules.pop(name, None)
        if name == "src.web":
            sys.modules.pop(name, None)

    engine_module = importlib.import_module("packages.registration_core.engine")

    assert engine_module.RegistrationEngine is not None
    assert not any(name == "src.web" or name.startswith("src.web.") for name in sys.modules)
