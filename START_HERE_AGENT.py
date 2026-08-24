#!/usr/bin/env python3
"""Executable thin entrypoint for any compatible Agent host."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
from customer_flow import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
