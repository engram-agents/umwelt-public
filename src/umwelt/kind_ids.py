"""Engine kind ids that more than one module needs (umwelt#1043, slice 2).

A LEAF module: stdlib-only, imports nothing from this package, so the world
package loader can name an engine kind without importing `server`. Each id
is written here ONCE; server.py keeps its historical private name pointing
at this constant.
"""

CHARGE_BOARD_KIND_ID = "kind_charge_board"
