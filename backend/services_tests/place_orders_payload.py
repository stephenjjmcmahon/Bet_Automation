import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.services.betslips_service import create_betslip

def test_place_orders_payload():

    betslip = create_betslip(
        "1.123456789",
        12345,
        "BACK",
        2.0,
        2
    )

    payload = {
        "marketId": betslip["marketId"],
        "instructions": betslip["instructions"]
    }

    print("Payload that would be sent to Betfair:")
    print(payload)

test_place_orders_payload()