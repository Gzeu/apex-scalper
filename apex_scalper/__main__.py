"""Entry point pentru `python -m apex_scalper`.

Permite lansarea corecta a pachetului ca modul:
  python -m apex_scalper

Fara acest fisier, `python apex_scalper/main.py` esueaza cu:
  ImportError: attempted relative import with no known parent package
Deoarece Python nu recunoaste main.py ca parte dintr-un pachet cand
e rulat direct ca script.
"""
import asyncio
from .main import main

if __name__ == "__main__":
    asyncio.run(main())
