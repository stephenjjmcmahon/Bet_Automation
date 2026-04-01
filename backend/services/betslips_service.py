def create_betslip(market_id, selection_id, side, price, stake):

    betslip = {
        "marketId": market_id,
        "instructions": [
            {
                "selectionId": selection_id,
                "side": side,
                "orderType": "LIMIT",
                "limitOrder": {
                    "size": stake,
                    "price": price,
                    "persistenceType": "LAPSE"
                }
            }
        ]
    }

    return betslip