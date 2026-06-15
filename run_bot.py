#!/usr/bin/env python3
"""Launcher script pentru apex_scalper.

Utilizare:
  python run_bot.py

Echivalent cu:
  python -m apex_scalper

Folosit de watchdog (subprocess fallback non-Docker) si pentru
lansare manuala fara a fi nevoie sa setezi PYTHONPATH.
"""
import sys
import os
import asyncio

# Garanteaza ca radacina proiectului e in sys.path
# indiferent de cwd la momentul lansarii
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from apex_scalper.main import main  # noqa: E402

if __name__ == "__main__":
    asyncio.run(main())
