from backend.services.betslips_service import create_betslip
from backend.services.betfair_client import place_orders


def place_bet(market_id, selection_id, price, stake):

    betslip = create_betslip(
        market_id,
        selection_id,
        "BACK",
        price,
        stake
    )

    response = place_orders(
        betslip["marketId"],
        betslip["instructions"]
    )

    return responsefrom services.betslips_service import create_betslip
from services.betfair_client import place_orders


def place_bet(market_id, selection_id, price, stake):

    betslip = create_betslip(
        market_id,
        selection_id,
        "BACK",
        price,
        stake
    )

    response = place_orders(
        betslip["marketId"],
        betslip["instructions"]
    )

    return response