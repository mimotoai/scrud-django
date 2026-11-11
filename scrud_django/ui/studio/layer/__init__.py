"""The studio's packaged UI layer (``studio:``), declared by
`scrud_django.ui.studio.apps.ScrudUIStudioConfig`: its components
(``component/``), its stylesheet (``stylesheet/studio.json``), CSS
(``css/studio.json``) and JS (``js/studio.json``), each file one item without
the branch fields (see ``docs/ui.md``, "Packaged UI layers").

Its rules select in the scope ``studio`` only (``studio scrud:page``), so a
page without ``?ui-scope=studio`` renders as before. Its CSS and JS are
effective on every branch, so every page links them: every CSS selector is
under a ``.studio`` class or a ``[data-studio…]`` attribute, and the JS does
nothing unless the page has a ``[data-studio-workbench]`` element, but on a
push's page (``[data-studio-push]``), where ``studio:promote`` stops the
``change`` of the push's state input and preview controls and previews the
target after the push. No component has an inline script or event handler
attribute."""
