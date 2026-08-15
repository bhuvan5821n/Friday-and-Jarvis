import urllib.parse

from actions.browser_control import browser_control
from actions.web_search import web_search as web_search_action


def news_update(parameters: dict = None, response=None, player=None, session_memory=None) -> str:
    params = parameters or {}
    query = params.get("query", "").strip()
    browser = params.get("browser", "").strip() if params.get("browser") else None
    max_items = params.get("max_items")

    if not query:
        query = "today news"

    search_query = query if query else "today news"
    search_url = (
        "https://www.google.com/search?q="
        + urllib.parse.quote(search_query)
        + "&tbm=nws"
    )

    browser_args = {"action": "go_to", "url": search_url}
    if browser:
        browser_args["browser"] = browser

    try:
        browser_control(parameters=browser_args, player=player)
        if player:
            player.write_log(f"[News] Opened browser news search: {search_query}")
    except Exception as exc:
        print(f"[News] ⚠️ Browser open failed: {exc}")

    summary_args = {"query": search_query}
    if max_items:
        summary_args["query"] = f"{search_query} top {max_items} headlines"

    summary = web_search_action(parameters=summary_args, player=player)
    return (
        "Opened the browser to the latest news search and summarized multiple recent headlines.\n\n"
        + summary
    )
