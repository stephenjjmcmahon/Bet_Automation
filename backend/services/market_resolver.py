def resolve_selection(runners: list, selection_name: str) -> tuple[int, str] | tuple[None, None]:
    name_lower = selection_name.lower()
    for runner in runners:
        runner_name = runner.get("runnerName", "")
        if name_lower in runner_name.lower() or runner_name.lower() in name_lower:
            return runner["selectionId"], runner_name
    return None, None
