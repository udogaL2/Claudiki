"""Делает bridge.py импортируемым из тестов (bridge/ в sys.path)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
