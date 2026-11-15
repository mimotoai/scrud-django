"""The UI layer installed for the whole suite (``layertest:``). Inert for
every other test (ruling 2): it has one stylesheet, whose one rule selects
under the scope ``layertest``, which only `tests/test_ui_layers.py` uses, and
no components, CSS or JS items, which other tests list or every page would
link. The layer tests that need those install `tests.ui_layer_more` too."""
