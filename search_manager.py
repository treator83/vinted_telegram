import json

from search import Search


class SearchManager:

    def __init__(self, filename="searches.json"):

        self.filename = filename

    def load(self):

        with open(self.filename, "r", encoding="utf-8") as f:
            data = json.load(f)

        searches = []

        for item in data:

            searches.append(

                Search(

                    id=item["id"],

                    name=item["name"],

                    url=item["url"],

                    max_price=item.get("max_price", 999999),

                    keywords=item.get("keywords", []),

                    sizes=item.get("sizes", []),

                    conditions=item.get("conditions", [])
                )

            )

        return searches