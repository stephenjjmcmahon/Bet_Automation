import sys
import os

# allow imports from project
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.ai_interpreter import AIInterpreter
from services.search_service import search_market
from services.betslips_service import create_betslip
from services.betfair_client import place_orders



print("=== SYSTEM TEST STARTED ===")

# Example user input
user_input = "Bet Chelsea 100 at 5.2"

print("\nUser input:")
print(user_input)

# Step 1 — AI interpretation
parsed_bet = AIInterpreter.interpret(user_input)

print("\nParsed bet:")
print(parsed_bet)
#print(parsed_bet.sport)

# Step 2 — Search Betfair and resolve selection
result = search_market(parsed_bet)

#print("\nResolved Betfair IDs:")
#print(result)

# Step 3 — Create betslip (DRY RUN)

betslip = create_betslip(
    result["marketId"],
    result["selectionId"],
    "BACK",
    parsed_bet.price,
    parsed_bet.stake
)

print("\nGenerated Betslip:")
print(betslip)

# Step 4 — Place order 
# Uncomment the following lines to actually place the order
#response = place_orders(betslip["marketId"], betslip["instructions"])
#print("\nOrder response:")
#print(response)


print("\n=== TEST COMPLETE ===")