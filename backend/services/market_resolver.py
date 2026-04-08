def resolve_selection(runners: list, selection_name: str) -> int:
    name_lower = selection_name.lower()
    for runner in runners:
        runner_name = runner.get("runnerName", "").lower()
        if name_lower in runner_name or runner_name in name_lower:
            return runner["selectionId"]
    # Fallback: return first runner if no match found
    if runners:
        return runners[0]["selectionId"]
    raise ValueError(f"Could not resolve selection: {selection_name}")