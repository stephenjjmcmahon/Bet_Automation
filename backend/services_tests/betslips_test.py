import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.betslips_service import create_betslip

def test_betslip():

    market_id = "1.123456789"
    selection_id = 987654
    side = "BACK"
    price = 2.5
    stake = 10

    betslip = create_betslip(
        market_id,
        selection_id,
        side,
        price,
        stake
    )

    print("Betslip created:")
    print(betslip)

test_betslip()