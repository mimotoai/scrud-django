from django.apps import AppConfig


class TestsConfig(AppConfig):
    name = "tests"


class UILayerTestConfig(AppConfig):
    """The UI layer every test runs with (`tests.ui_layer`): see its
    package docstring for why it changes no other test's output."""

    name = "tests.ui_layer"
    label = "tests_ui_layer"
    scrud_ui_layer = {"prefix": "layertest:", "package": "tests.ui_layer"}


class UILayerMoreConfig(AppConfig):
    """A second UI layer, installed by the tests that stack layers."""

    name = "tests.ui_layer_more"
    label = "tests_ui_layer_more"
    scrud_ui_layer = {"prefix": "layermore:", "package": "tests.ui_layer_more"}


class UILayerBadConfig(AppConfig):
    """A UI layer with invalid files, installed by the test of
    ``scrud_django_ui.E002``."""

    name = "tests.ui_layer_bad"
    label = "tests_ui_layer_bad"
    scrud_ui_layer = {"prefix": "layerbad:", "package": "tests.ui_layer_bad"}


class UILayerInvalidConfig(AppConfig):
    """A UI layer whose files have content the viewsets refuse, installed
    by the test of ``scrud_django_ui.E002``'s content checks."""

    name = "tests.ui_layer_invalid"
    label = "tests_ui_layer_invalid"
    scrud_ui_layer = {"prefix": "layerinvalid:", "package": "tests.ui_layer_invalid"}
