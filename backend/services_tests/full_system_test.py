import sys
import os

# allow imports from project
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.ai_interpreter import AIInterpreter
from services.search_service import search_market


print("=== SYSTEM TEST STARTED ===")

# Example user input
user_input = "Back Arsenal £10 at 2.2"

print("\nUser input:")
print(user_input)

# Step 1 — AI interpretation
parsed_bet = AIInterpreter.interpret(user_input)

print("\nParsed bet:")
#print(parsed_bet)

# Step 2 — Search Betfair and resolve selection
result = search_market(parsed_bet)

print("\nResolved Betfair IDs:")
print(result)

print("\n=== TEST COMPLETE ===")