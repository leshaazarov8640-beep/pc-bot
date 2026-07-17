import sys
import os

# Добавляем папку с проектом в путь
sys.path.insert(0, os.path.dirname(__file__))

from bot import app as application
