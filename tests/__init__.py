"""Testpaket - unterdrueckt die Protokollausgabe der zu testenden Module."""

import logging

logging.getLogger().addHandler(logging.NullHandler())
logging.disable(logging.CRITICAL)
