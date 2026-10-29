"""kitlib.modulemap (docs/module-map.md): a repository's declared modules and the allowed edges
between them (load), the detectors that find every crossing in the tree (detectors), and the
edge check and baseline tools/module_map.py runs (check), and the isolation run that tests each
module in a sparse clone holding only what its map entry reaches (isolate).
"""
from .load import ROOT, MapError, ModuleMap, load_ceilings, load_map

__all__ = ['ROOT', 'MapError', 'ModuleMap', 'load_ceilings', 'load_map']
