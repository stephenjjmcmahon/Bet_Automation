# Sports where event names are meetings, races, or tournaments rather than
# head-to-head matchups. For these, the selection_name won't appear in the
# event name so we fetch all events for the sport and let the AI pick.
COMPETITION_SPORTS = {
    "golf", "horse racing", "motor sport", "motorsport",
    "greyhound racing", "cycling", "politics", "special bets",
}

# Subset of COMPETITION_SPORTS where an event is a MEETING and each race is a
# WIN/PLACE market beneath it (runner = horse/dog). These bypass the AI event
# pick entirely: the race is found deterministically by scanning every
# upcoming market's runners for the named horse/dog (see
# search_service.resolve_racing_markets).
RACING_SPORTS = {"horse racing", "greyhound racing"}

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
    "snooker": "6422",          # Playername v Playername, or tournament name
}


class UnsupportedSportError(ValueError):
    """The parsed sport has no Betfair event-type id mapping."""


def event_type_id_for(sport: str) -> str:
    """Betfair event-type id for a sport name (case-insensitive).

    Single lookup point so the normalization and the "unsupported sport" failure
    are consistent everywhere — callers used to inline this and disagreed on
    whether a miss should raise or silently return an empty result.
    """
    event_type_id = SPORT_EVENT_TYPE_MAP.get(sport.lower())
    if not event_type_id:
        raise UnsupportedSportError(f"Unsupported sport: {sport}")
    return event_type_id