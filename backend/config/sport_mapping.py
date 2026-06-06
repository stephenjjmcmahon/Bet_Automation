# Sports where event names are meetings, races, or tournaments rather than
# head-to-head matchups. For these, the selection_name won't appear in the
# event name so we fetch all events for the sport and let the AI pick.
COMPETITION_SPORTS = {
    "golf", "horse racing", "motor sport", "motorsport",
    "greyhound racing", "cycling", "politics", "special bets",
}

# Some sports use different market type codes than the AI's generic names.
# Applied in resolve_market before calling Betfair.
MARKET_TYPE_OVERRIDES = {
    "golf": {
        "OUTRIGHT_WINNER": "WINNER",
        "MATCH_ODDS":      "WINNER",
    },
    "horse racing": {
        "MATCH_ODDS":      "WIN",
        "OUTRIGHT_WINNER": "WIN",
    },
    "motor sport": {
        "MATCH_ODDS": "OUTRIGHT_WINNER",
    },
    "motorsport": {
        "MATCH_ODDS": "OUTRIGHT_WINNER",
    },
    "cycling": {
        "MATCH_ODDS":      "OUTRIGHT_WINNER",
        "OUTRIGHT_WINNER": "OUTRIGHT_WINNER",
    },
}

SPORT_EVENT_TYPE_MAP = {
    "soccer": "1",          # Teamname v Teamname
    "football": "1",
    "tennis": "2",      # Playername v Playername, Set 1, Set 2, etc.
    "golf": "3",        # Tournament name (e.g. "US Open 2026"); sub-markets use "3rd Round 3 Balls", "Player Round Scores", "Tournament Match Bets"
    "cricket": "4",     # Teamname v Teamname; also competition outrights (e.g. "Caribbean Premier League")
    "rugby union": "5", # Teamname v Teamname; also competition outrights (e.g. "Super Rugby Pacific", "United Rugby Championship")
    "rugby": "5",
    "boxing": "6",      # Fightername v Fightername
    "horse racing": "7",# Meeting name and date (e.g. "Ascot 15th June")
    "motor sport": "8", # Race name (e.g. "F1 Monaco Grand Prix"); also outrights (e.g. "F1 Outrights 2026")
    "motorsport": "8",
    "greyhound racing": "4339", # Meeting name and date (e.g. "Romford 6th Jun"); also "ANTEPOST" for future markets
    "politics": "2378961",      # Topic description (e.g. "UK - Next General Election")
    "basketball": "7522",       # Teamname v Teamname (EU) or Teamname @ Teamname (NBA)
    "australian rules": "61420",# Teamname v Teamname, or competition name (e.g. "AFL")
    "mixed martial arts": "26420387", # Fightername v Fightername
    "mma": "26420387",
    "rugby league": "1477",     # Teamname v Teamname, or competition name (e.g. "NRL")
    "baseball": "7511",         # Teamname @ Teamname
    "esports": "27454571",      # Teamname v Teamname
    "darts": "3503",            # Playername v Playername, or tournament name
    "special bets": "10",       # Varies — TV shows, awards, entertainment
    "american football": "6423",# Teamname @ Teamname, or competition name (e.g. "NFL")
    "gaelic games": "2152880",  # Teamname v Teamname, or competition name (e.g. "All Ireland Football")
    "ice hockey": "7524",       # Teamname @ Teamname, or competition name (e.g. "NHL")
    "volleyball": "998917",     # Teamname v Teamname (country or club)
    "cycling": "11",            # Race/tour name (e.g. "Tour de France")
}