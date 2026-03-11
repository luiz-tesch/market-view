import sys
import os

# Adiciona a raiz do projeto ao Python path
# Isso permite que os testes importem 'src.*' corretamente
sys.path.insert(0, os.path.dirname(__file__))