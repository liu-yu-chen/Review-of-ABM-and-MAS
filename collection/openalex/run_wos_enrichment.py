"""Reuse the existing DOI-stage credential without exposing or duplicating it."""
import ast
import os
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
if not os.environ.get('OPENALEX_API_KEY'):
    tree = ast.parse((root / 'process/enrich.py').read_text(encoding='utf-8-sig'))
    setting = next(node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'DEFAULT_KEYS' for target in node.targets))
    os.environ['OPENALEX_API_KEY'] = ast.literal_eval(setting.value)['doi']
sys.argv = ['process.enrich_wos_openalex'] + sys.argv[1:]
runpy.run_module('process.enrich_wos_openalex', run_name='__main__')
