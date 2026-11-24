# The test suite checks French messages (the source language of the code). Noxa now defaults to English
# for new servers, so the tests pin the old default; test_i18n covers the English default explicitly.
from noxa import i18n as _i18n

_i18n.DEFAULT = "fr"
_i18n.set_server_default("fr")
