import sys
import os

# allow Python to find the services folder
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.market_resolver import resolve_selection

runners = [
 {"selectionId":1096,"runnerName":"Arsenal"},
 {"selectionId":47999,"runnerName":"Man City"},
 {"selectionId":58805,"runnerName":"The Draw"}
]

selection_id = resolve_selection(runners, "Arsenal")

print(selection_id)