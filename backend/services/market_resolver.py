def resolve_selection(runners, selection_name):
    
    selection_name = selection_name.lower()

    for runner in runners:

        if runner["runnerName"].lower() == selection_name:
            return str(runner["selectionId"])

    return None