"""kitlib.modulemap (docs/module-map.md): a repository's declared modules and the allowed edges
between them (load), the detectors that find every crossing in the tree (detectors), and the
edge check and baseline tools/module_map.py runs (check).
"""
from .load import ROOT, MapError, ModuleMap, load_ceilings, load_map

__all__ = ['ROOT', 'MapError', 'ModuleMap', 'load_ceilings', 'load_map']
